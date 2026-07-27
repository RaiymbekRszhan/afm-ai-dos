# Развёртывание на сервере АФМ

Проект = **4 сервиса**, которые крутятся вместе (лучше на GPU-сервере):

| Сервис | Порт | venv | Что делает |
|--------|------|------|-----------|
| Основной API | 8000 | `.venv` | оркестратор + STT-роутинг + вызов RAG/TTS |
| RAG-сервис | 8077 | `rag/.venv` | ответ строго по базе РК (LightRAG + LLM/эмбеддинги АФМ) |
| F5-сервер | 8810 | `.venv-f5` | русский TTS (F5-TTS + ударения; референс refs/ref_ru_f5.wav) |
| Spark-сервер | 8809 | `.venv-spark` | казахский TTS (голос по умолчанию) |

⚠️ **venv `rag/` нельзя сливать с основным** — `lightrag`/`torch` конфликтуют с голосовым слоем.

---

## (Рекомендуется) Установка через uv — быстрее и воспроизводимее

`uv` — быстрый менеджер пакетов (замена pip). Ставит в разы быстрее (а у нас 4 venv) и
фиксирует версии через lock — это снимает риск «на Mac работает, на сервере нет».
Зависимости каждого сервиса объявлены в его `pyproject.toml`; `requirements.txt` —
закреплённый список для установки/лока.

**Поставить uv один раз** (нужен интернет; офлайн — взять бинарь заранее):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # или: pip install uv
```

- В шагах ниже команды даны на `uv`. Без `uv` — те же команды с обычным `pip` (рабочий фолбэк).
- `setup.sh` у F5/Spark **сами используют `uv`, если он установлен** (иначе pip) — менять ничего не нужно.
- **Зафиксировать точные версии (lock)** на машине с интернетом — по разу на сервис:
  ```bash
  uv pip compile pyproject.toml -o requirements.txt          # корень и rag/ (из их папок)
  ```
  Полученные `requirements.txt` коммитишь и везёшь на сервер — установка станет
  полностью воспроизводимой (включая транзитивные зависимости).

---

## ⚠️ ШАГ 0. Проверь интернет на GPU-сервере (это решает всё)
```bash
ssh ПОЛЬЗОВАТЕЛЬ@СЕРВЕР
curl -I https://pypi.org        # и https://huggingface.co
```
- **Работает** → иди по инструкции ниже (обычный путь).
- **Не работает (HTTP 000)** → офлайн-деплой сложнее (нужны бандлы зависимостей/моделей под Linux). **Напиши мне** — соберём отдельно.

Также проверь, что с сервера видны внутренние сервисы АФМ:
```bash
curl -m 5 http://192.168.165.2:8901/v1/models   # LLM (должен ответить)
curl -m 5 http://192.168.165.2:8806/v1/models   # эмбеддинги (нужны RAG-сервису)
```

---

## ШАГ 1. Скопировать проект с Mac (scp)
⚠️ **venv НЕ копируем** — они под macOS, на Linux-сервере не заработают (пересоздаём на месте).

С Mac, из папки проекта:
```bash
# код + база знаний + образцы голоса + RAG-сервис + запуск/деплой
scp -r app scripts f5_server spark_server rag refs \
       requirements.txt pyproject.toml run_api.sh deploy \
       .env.example README.md \
       ПОЛЬЗОВАТЕЛЬ@СЕРВЕР:~/STT/
```
`refs/` (твои образцы голоса) **обязательно** — их нельзя скачать.
`run_api.sh` и `deploy/` нужны на шаге 7 (запуск) — без них деплой по инструкции не соберётся.

⚠️ `scp -r rag` копирует **всё** содержимое, включая `rag/.venv` (сборка под macOS,
на Linux не работает) и `rag/rag_storage/`. Сразу после копирования удали их на сервере —
venv пересоздаётся, индекс строится заново через `ingest` (шаг 3):
```bash
ssh ПОЛЬЗОВАТЕЛЬ@СЕРВЕР 'rm -rf ~/STT/rag/.venv ~/STT/rag/rag_storage ~/STT/rag/_convert'
```
(Либо копируй через `rsync -a --exclude='.venv' --exclude='rag_storage' rag/ …` — scp исключать не умеет.)

**НЕ нужны на сервере** (пересоздаются/скачиваются): `.venv*`, `rag/.venv`, `rag/rag_storage/`,
`rag/_convert/`, `spark_tts_repo/`, `models/`.

---

## ШАГ 2. Основной API (venv .venv)
```bash
cd ~/STT
uv venv .venv && source .venv/bin/activate     # без uv: python3 -m venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt             # без uv: pip install -r requirements.txt
deactivate
```
Whisper-kk (~1.6 ГБ) скачается сам при первом казахском запросе.

## ШАГ 3. RAG-сервис (venv rag/.venv)
```bash
cd ~/STT/rag
uv venv .venv && source .venv/bin/activate     # без uv: python3 -m venv .venv && source .venv/bin/activate
uv pip install -r requirements.txt             # без uv: pip install -r requirements.txt (LightRAG==1.4.9.10)
cp .env.example .env                   # проверь LLM_BASE_URL / LLM_MODEL
python -m scripts.check                # покажет реальную размерность эмбеддинга
                                       #   (qwen3-embedding-8b = 4096) → EMBEDDING_DIM в .env
python -m ragsvc.ingest                # построить индекс базы в rag_storage/
cd .. && deactivate
```
> ⚠️ Индексация — **только векторный поиск** (граф знаний не используется): зовёт лишь
> эмбеддинги (минуты), без нагрузки на LLM. Качество подтверждено eval (см. `rag/eval/`).
> `ingest` возобновляемый: при обрыве просто запусти снова.

> ⚠️ **Словарь tiktoken — завендорен, копировать обязательно.** LightRAG при старте
> берёт словарь токенизатора; без кэша tiktoken лезет в интернет (`openaipublic.blob...`)
> и на офлайн-сервере RAG **не стартует** (симптом: `NameResolutionError ... o200k_base.tiktoken`).
> Лекарство: папка `rag/vendor/tiktoken/` должна приехать вместе с проектом, а переменная
> `TIKTOKEN_CACHE_DIR` указывать на неё (в `run_api.sh` и `deploy/ai-dos-rag.service` уже
> прописано). Пересоздать папку (на машине с интернетом):
> `TIKTOKEN_CACHE_DIR=$PWD/vendor/tiktoken python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"`.
> Кэш во временной папке ОС ненадёжен — macOS/Linux чистят её при перезагрузке.

## ШАГ 4. F5-сервер (русский TTS) — ОПЦИОНАЛЬНО

> ⏭️ **Пропусти этот шаг, если русский TTS берётся с GPU-сервера АФМ** (`192.168.165.2:8991`
> — вариант по умолчанию, `run_api.sh` уже на него настроен, см. ниже). Локальный
> `f5_server` нужен только для автономного/оффлайн русского TTS на этой же машине.

```bash
bash f5_server/setup.sh                # .venv-f5 + f5-tts + модель + Vocos + RUAccent
```
> ⚠️ f5-tts тянет bleeding-edge torch/torchaudio/torchcodec — `setup.sh` откатывает на
> `torch==2.8.0`/`torchaudio==2.8.0` и убирает torchcodec (иначе ломается аудио без ffmpeg
> shared-libs). Референс `refs/ref_ru_f5.wav`+`refs/ref_ru.txt` уже в репозитории.

## ШАГ 5. Spark-сервер (казахский TTS) — ОФЛАЙН-ФОЛБЭК
> Нужен только для казахского TTS БЕЗ интернета. Основной путь — ElevenLabs (шаг 6,
> ключ в `.env`), ему установки не требуется — это облако.
```bash
bash spark_server/setup.sh             # .venv-spark + фреймворк + модель
```
> Опц.: вынести казахский **STT** на GPU АФМ отдельным сервисом (чтобы на этой машине
> не было torch) — `bash whisper_kk_server/setup.sh` (см. `whisper_kk_server/README.md`),
> затем `STT_KK_URL=http://192.168.165.2:8813/transcribe`.

---

## ШАГ 6. Настроить .env (в корне ~/STT)
```bash
cp .env.example .env
```
Проверь/поставь:
```
# внутренние сервисы АФМ (адреса те же)
LLM_BASE_URL=http://192.168.165.2:8901/v1
STT_URL=http://192.168.165.2:8804/transcribe   # 2026-07-03: АФМ перенёс STT с 8004 на 8804
# (эмбеддинги нужны только RAG-сервису — настраиваются в rag/.env, шаг 3, не здесь)

# казахский STT — на сервер АФМ (:8804 тянет и казахский). Whisper на этой машине
# не нужен (torch не тянем). Опц. вынести на отдельный Whisper-сервер (GPU АФМ):
#   STT_KK_URL=http://192.168.165.2:8813/transcribe   (см. whisper_kk_server/README.md)
STT_KK_USE_WHISPER=false

# источник ответа — RAG-сервис
RAG_URL=http://localhost:8077/ask

# TTS — ОСНОВНОЙ: ElevenLabs (облако, рус+каз одним голосом; ⚠️ НУЖЕН ИНТЕРНЕТ).
# Ключ/голос — только в .env (в git не идут). Включить весь TTS на eleven: старт
# с USE_ELEVEN=1 (он выставит TTS_PROVIDER/TTS_KK_PROVIDER=eleven и погасит Spark).
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...

# TTS — ОФЛАЙН-ФОЛБЭК (без интернета): русский F5 (GPU АФМ) + казахский Spark.
# Это дефолт run_api.sh БЕЗ USE_ELEVEN.
TTS_PROVIDER=f5
F5_URL=http://192.168.165.2:8991/tts
F5_REF_AUDIO=refs/ref_ru_f5.wav      # референс тембра (шлётся в каждый запрос)
F5_REF_TEXT=@refs/ref_ru.txt         # "@путь" = прочитать транскрипт из файла
TTS_KK_PROVIDER=spark
SPARK_URL=http://192.168.165.2:8992/tts
# Локальный TTS вместо GPU (когда сервер АФМ недоступен): старт с USE_LOCAL_F5=1
# (русский) и/или USE_LOCAL_SPARK=1 (казахский) — адреса/контракт переключатся сами.
```
И в `spark_server/run.sh` поставь `SPARK_DEVICE="cuda"` (для GPU).
LLM/эмбеддинги RAG-сервиса настраиваются отдельно в `rag/.env` (шаг 3).

> **Как оркестратор выбирает контракт F5:** если задан `F5_REF_AUDIO` — шлёт
> multipart `{ref_audio, ref_text, gen_text}` на GPU-сервер (референс в каждом
> запросе); если пусто — прежний JSON `{text, language}` на локальный `f5_server`.
> `run_api.sh` уже настроен на GPU-сервер по умолчанию (`USE_LOCAL_F5=0`) — отдельные
> ключи запуска не нужны. Вернуть локальный русский TTS: `USE_LOCAL_F5=1 bash run_api.sh`
> (адрес и контракт переключатся сами). Казахский Spark на GPU АФМ (`:8992`)
> использует ТОТ ЖЕ JSON-контракт `{text, language}`, что и локальный, — вернуть
> локальный: `USE_LOCAL_SPARK=1 bash run_api.sh` (`SPARK_URL` переключится сам).

**Через systemd:** в `deploy/ai-dos-api.service` поставь
`Environment=F5_URL=http://192.168.165.2:8991/tts`,
`Environment=F5_REF_AUDIO=refs/ref_ru_f5.wav`,
`Environment=F5_REF_TEXT=@refs/ref_ru.txt`,
`Environment=SPARK_URL=http://192.168.165.2:8992/tts` и отключи локальные TTS-юниты
`ai-dos-f5`/`ai-dos-spark` (`systemctl disable --now ai-dos-f5 ai-dos-spark`).

---

## ШАГ 7. Запустить всё
**Одной командой** (поднимает rag + основной API + video_ui; русский И казахский TTS —
с GPU-сервера АФМ, локальные f5/spark не стартуют; Ctrl-C гасит всё):
```bash
HF_HUB_OFFLINE=1 bash run_api.sh
```
Или вручную по сервисам (tmux). `f5_server`/`spark_server` — только если нужен локальный
TTS (`USE_LOCAL_F5=1` / `USE_LOCAL_SPARK=1`); по умолчанию TTS берётся с GPU АФМ:
```bash
(cd rag && source .venv/bin/activate && uvicorn ragsvc.server:app --host 127.0.0.1 --port 8077)
source .venv/bin/activate && HF_HUB_OFFLINE=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
# локальный TTS (опционально): bash f5_server/run.sh  # 8810 (ru) ; bash spark_server/run.sh  # 8809 (kk)
```

---

## ШАГ 8. Проверить — приёмка ОДНОЙ командой
```bash
bash scripts/healthcheck.sh          # все 4 сервиса + RAG-ответ + TTS (ru/kk), PASS/FAIL
bash scripts/healthcheck.sh --full   # + сквозной голос STT→RAG→TTS (по образцам refs/)
```
Печатает по строке на каждый шаг (✔/✘), в конце — итог и папку с синтезированными
WAV для прослушки. Код выхода 0 = всё прошло (можно использовать как критерий
«деплой принят»). Проверить сервер по сети: `HOST=АДРЕС bash scripts/healthcheck.sh`.

> Если TTS возьмёт на себя АФМ (внешний endpoint) — скрипт это видит по `/health`
> и не пытается пинговать локальные F5/Spark, а помечает TTS как внешний.

<details><summary>…или вручную, по отдельным curl</summary>

```bash
curl http://localhost:8000/health        # status:ok, rag.reachable:true, tts.enabled:true
curl -m 5 http://localhost:8077/health    # RAG-сервис жив

# ответ строго по базе
curl -s -X POST http://localhost:8077/ask -H 'Content-Type: application/json' \
  -d '{"question":"Какой порог по операциям с ювелирными изделиями?","lang":"ru"}'

# русский TTS (F5, с ударениями)
curl -X POST http://localhost:8000/speak -H 'Content-Type: application/json' \
  -d '{"text":"Здравствуйте!","language":"russian"}' --output ru.wav
# казахский TTS (Spark, голос по умолчанию)
curl -X POST http://localhost:8000/speak -H 'Content-Type: application/json' \
  -d '{"text":"Сәлеметсіз бе!","language":"kazakh"}' --output kk.wav
```
Или открой `http://СЕРВЕР:8000/` в браузере — тест-страница с микрофоном.

</details>

---

## Два TTS-пайплайна: локальный и ElevenLabs

Обе ветки ставятся **один раз** (шаги выше), в `.env` держатся **оба блока сразу**
(ШАГ 6). Наличие ключа ElevenLabs в `.env` само по себе облако НЕ включает — ветку
выбирает **команда запуска**. Автопереключения между ними нет и не задумано: провайдер
фиксируется при старте процесса (`USE_ELEVEN=1` переводит оба языка на `eleven` и не
поднимает Spark). Если ElevenLabs упадёт на запросе — вернётся ошибка, тихого отката на
F5/Spark не будет. Это осознанный выбор: **два пайплайна — две команды**.

Обе ветки слушают `:8000` и `:8100`, поэтому работает **одна за раз**: Ctrl-C гасит
стек, запускаешь другой командой.

```bash
HF_HUB_OFFLINE=1 bash run_api.sh     # ЛОКАЛЬНАЯ ветка: русский F5 (GPU АФМ) + казахский Spark
USE_ELEVEN=1     bash run_api.sh     # ОБЛАЧНАЯ ветка: рус+каз одним голосом на eleven_v3
```
Полностью автономный русский на этой же машине (без GPU АФМ):
`USE_LOCAL_F5=1 F5_URL=http://localhost:8810/tts bash run_api.sh`.

### Сеть — главное отличие на контуре АФМ

| Ветка | Куда ходит |
|-------|-----------|
| **Локальная** | только LAN АФМ (LLM `:8901`, STT `:8804`, F5 `:8991`, RAG/Spark локально). **Интернет не нужен.** |
| **ElevenLabs** | то же самое **+ наружу** `https://api.elevenlabs.io:443`. |

Контур АФМ (`192.168.165.x`) интернета не даёт, поэтому для облачной ветки киоск-машине
нужен **split routing**: default-маршрут — через интерфейс с интернетом, а на подсеть АФМ
— явный статический маршрут (иначе LLM/STT уходят в default и не достучатся):
```bash
sudo ip route add 192.168.165.0/24 via <шлюз_кабеля_АФМ> dev <интерфейс_АФМ>
```
Проверка перед стартом облачной ветки (оба должны отвечать):
```bash
curl -m5 -I https://api.elevenlabs.io            # наружу жив
curl -m5 http://192.168.165.2:8901/v1/models     # контур АФМ жив
```
Если в АФМ есть прокси — процессу API выставить `HTTPS_PROXY=...` и согласовать с сетевиками
исходящий 443 на `api.elevenlabs.io`. Для локальной ветки ничего этого не требуется.

### Характеристики

| | Локальная (F5 + Spark) | ElevenLabs (`eleven_v3`) |
|---|---|---|
| **Интернет** | не нужен | нужен (443 наружу) |
| **Русский** | F5-TTS + RUAccent — хорошо (глотает концы слов → паддинг) | чисто, естественно |
| **Казахский** | Spark — приемлемо | чисто (главная причина брать v3) |
| **Голос** | разные движки/тембры на язык | один голос на оба языка |
| **Задержка** | F5 GPU ~1.5 c/кусок; Spark локально + прогрев модели | сеть round-trip (нестабильно за прокси) |
| **Стоимость** | ~0 (своё железо) | ~$0.08 за озвученный вопрос |
| **Железо** | нужен GPU + память (Spark грузится долго, ест RAM) | почти ничего локально |
| **Приватность** | всё в контуре АФМ | текст ответа уходит в облако (США) — для госоргана взвесить |
| **Риск** | автономно, но качество ниже | зависит от интернета/аптайма API и биллинга |
| **Команда** | `HF_HUB_OFFLINE=1 bash run_api.sh` | `USE_ELEVEN=1 bash run_api.sh` |

Ориентир: локальная — основной/дежурный режим (автономна, бесплатна, приватна);
ElevenLabs — когда нужен максимально чистый голос (особенно казахский) и есть согласованный
выход в интернет. Единственное сетевое требование к «обеим веткам сразу» — split routing на
киоск-машине.

**Через systemd:** держи два юнита (копии `deploy/ai-dos-api.service`) — во втором добавь
`Environment=USE_ELEVEN=1`; активен всегда один (`systemctl enable --now` нужный,
`disable --now` другой). Приёмка любой ветки — `bash scripts/healthcheck.sh` (сам видит по
`/health`, внешний TTS или локальный).

---

## Работа 24/7

`run_api.sh` — для отладки; для боевого режима — systemd-юниты + сторож `/health`:
[deploy/README.md](deploy/README.md) (автозапуск при загрузке, рестарт при падении
и при зависании).

---

## Логи и аналитика

Два независимых потока (реализация — `app/logging_setup.py`):

- **Ops (ошибки, тайминги, request-id)** — в stderr → **journald**. Смотреть как обычно:
  ```bash
  journalctl -u ai-dos-api -f                    # живой поток
  journalctl -u ai-dos-api | grep interaction    # строки по каждому /voice
  ```
  Пример строки: `interaction lang=ru stt=320ms rag=1450ms tts=2100ms total=3.9s found=True suggest=0 print=fl provider=f5 error=-`. Ротацию journald держит сам (`SystemMaxUse` в `journald.conf`).
- **Аналитика — по строке JSONL на каждый `/voice`** в `logs/interactions.jsonl` (суточная ротация, `backupCount=LOG_RETENTION_DAYS` → старые файлы **сами удаляются = ретеншен**). Поля: `question`, `answer`, `lang`, `answer_found`, `suggested`, `print_ids`, `provider`, тайминги стадий, `error`.

⚠️ **`logs/` содержит ПДн граждан** — в git не коммитится (`.gitignore`), с сервера наружу не выносить без согласования.

**Отчёт для АФМ** (спрос, доля fallback = дыры в базе, p50/p95 задержек, ru/kk):
```bash
.venv/bin/python -m scripts.interactions_report --dir logs           # всё
.venv/bin/python -m scripts.interactions_report --dir logs --days 7  # за неделю
```

**Настройки** (env / `.env`, дефолты рабочие — менять не обязательно):

| Переменная | Дефолт | Смысл |
|---|---|---|
| `LOG_LEVEL` | `INFO` | уровень ops-логов |
| `LOG_DIR` | `logs` | куда писать JSONL (относит. WorkingDirectory) |
| `LOG_ANALYTICS` | `true` | писать JSONL по `/voice` |
| `LOG_QUESTIONS` | `full` | текст вопроса: `full` / `hash` (sha256) / `off` |
| `LOG_ANSWERS` | `true` | писать текст ответа |
| `LOG_RETENTION_DAYS` | `30` | сколько суток хранить (= число суточных файлов) |

**systemd:** journald ловит stdout сам; `logs/` под `/opt/ai-dos` при текущем `ProtectSystem=full` пишется без доп. настроек. Если ужесточишь до `ProtectSystem=strict` — добавь в юнит `ReadWritePaths=/opt/ai-dos/logs`.

---

## Если упрётся
- ошибка установки F5/Spark → пришли текст, это обычно версии (пины в их `requirements.txt`)
- RAG: `Embedding dimension mismatch` → сверь `EMBEDDING_DIM` в `rag/.env` с выводом `python -m scripts.check`
- нет интернета на сервере → офлайн-бандл, напиши мне
- внутренние сервисы АФМ не видны с GPU-сервера → вопрос их сетевикам
