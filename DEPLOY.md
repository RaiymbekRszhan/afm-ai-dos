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
# код + база знаний + образцы голоса + RAG-сервис
scp -r app scripts f5_server spark_server rag refs \
       requirements.txt .env.example README.md \
       ПОЛЬЗОВАТЕЛЬ@СЕРВЕР:~/STT/
```
`refs/` (твои образцы голоса) **обязательно** — их нельзя скачать.
В `rag/` копируется код и база `rag/data/`, но **не** `rag/.venv` и `rag/rag_storage/`
(venv пересоздаётся, индекс заново строится через `ingest` на шаге 3).

**НЕ копируй:** `.venv*`, `rag/.venv`, `rag/rag_storage/`, `rag/_convert/`, `spark_tts_repo/`,
`models/` (пересоздаются/скачиваются на сервере).

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

## ШАГ 4. F5-сервер (русский TTS)
```bash
bash f5_server/setup.sh                # .venv-f5 + f5-tts + модель + Vocos + RUAccent
```
> ⚠️ f5-tts тянет bleeding-edge torch/torchaudio/torchcodec — `setup.sh` откатывает на
> `torch==2.8.0`/`torchaudio==2.8.0` и убирает torchcodec (иначе ломается аудио без ffmpeg
> shared-libs). Референс `refs/ref_ru_f5.wav`+`refs/ref_ru.txt` уже в репозитории.

## ШАГ 5. Spark-сервер (казахский TTS)
```bash
bash spark_server/setup.sh             # .venv-spark + фреймворк + модель
```

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

# казахский STT — локальный Whisper на GPU
STT_KK_USE_WHISPER=true
WHISPER_DEVICE=cuda

# источник ответа — RAG-сервис
RAG_URL=http://localhost:8077/ask

# TTS — наши сервисы
TTS_PROVIDER=f5
F5_URL=http://localhost:8810/tts
TTS_KK_PROVIDER=spark
SPARK_URL=http://localhost:8809/tts
```
И в `f5_server/run.sh` / `spark_server/run.sh` поставь `*_DEVICE="cuda"` (для GPU).
LLM/эмбеддинги RAG-сервиса настраиваются отдельно в `rag/.env` (шаг 3).

---

## ШАГ 7. Запустить всё
**Одной командой** (поднимает f5 + spark + rag + основной API, Ctrl-C гасит всё):
```bash
HF_HUB_OFFLINE=1 bash run_api.sh
```
Или вручную по сервисам (4 окна / tmux):
```bash
bash f5_server/run.sh                                                # 8810
bash spark_server/run.sh                                             # 8809
(cd rag && source .venv/bin/activate && uvicorn ragsvc.server:app --host 0.0.0.0 --port 8077)
source .venv/bin/activate && HF_HUB_OFFLINE=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
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

## Если упрётся
- ошибка установки F5/Spark → пришли текст, это обычно версии (пины в их `requirements.txt`)
- RAG: `Embedding dimension mismatch` → сверь `EMBEDDING_DIM` в `rag/.env` с выводом `python -m scripts.check`
- нет интернета на сервере → офлайн-бандл, напиши мне
- внутренние сервисы АФМ не видны с GPU-сервера → вопрос их сетевикам
