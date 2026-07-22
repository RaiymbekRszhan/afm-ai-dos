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
SPARK_URL=http://localhost:8809/tts
# Локальный русский F5 вместо GPU: F5_URL=http://localhost:8810/tts, убери F5_REF_*, старт с USE_LOCAL_F5=1
```
И в `spark_server/run.sh` поставь `SPARK_DEVICE="cuda"` (для GPU).
LLM/эмбеддинги RAG-сервиса настраиваются отдельно в `rag/.env` (шаг 3).

> **Как оркестратор выбирает контракт F5:** если задан `F5_REF_AUDIO` — шлёт
> multipart `{ref_audio, ref_text, gen_text}` на GPU-сервер (референс в каждом
> запросе); если пусто — прежний JSON `{text, language}` на локальный `f5_server`.
> `run_api.sh` уже настроен на GPU-сервер по умолчанию (`USE_LOCAL_F5=0`) — отдельные
> ключи запуска не нужны. Вернуть локальный русский TTS: `USE_LOCAL_F5=1
> F5_URL=http://localhost:8810/tts bash run_api.sh`.

**Через systemd:** в `deploy/ai-dos-api.service` поставь
`Environment=F5_URL=http://192.168.165.2:8991/tts`,
`Environment=F5_REF_AUDIO=refs/ref_ru_f5.wav`,
`Environment=F5_REF_TEXT=@refs/ref_ru.txt` и отключи юнит `ai-dos-f5`
(`systemctl disable --now ai-dos-f5`).

---

## ШАГ 7. Запустить всё
**Одной командой** (поднимает spark + rag + основной API; русский TTS — с GPU-сервера
АФМ, локальный f5 не стартует; Ctrl-C гасит всё):
```bash
HF_HUB_OFFLINE=1 bash run_api.sh
```
Или вручную по сервисам (tmux). `f5_server` — только если нужен локальный русский TTS
(`USE_LOCAL_F5=1` и `F5_URL=http://localhost:8810/tts`); по умолчанию он не нужен:
```bash
bash spark_server/run.sh                                             # 8809 (казахский TTS)
(cd rag && source .venv/bin/activate && uvicorn ragsvc.server:app --host 127.0.0.1 --port 8077)
source .venv/bin/activate && HF_HUB_OFFLINE=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
# локальный русский TTS (опционально): bash f5_server/run.sh            # 8810
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

## Работа 24/7

`run_api.sh` — для отладки; для боевого режима — systemd-юниты + сторож `/health`:
[deploy/README.md](deploy/README.md) (автозапуск при загрузке, рестарт при падении
и при зависании).

---

## Если упрётся
- ошибка установки F5/Spark → пришли текст, это обычно версии (пины в их `requirements.txt`)
- RAG: `Embedding dimension mismatch` → сверь `EMBEDDING_DIM` в `rag/.env` с выводом `python -m scripts.check`
- нет интернета на сервере → офлайн-бандл, напиши мне
- внутренние сервисы АФМ не видны с GPU-сервера → вопрос их сетевикам
