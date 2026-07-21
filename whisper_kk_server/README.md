# Whisper-kk STT-сервер (казахское распознавание речи)

Отдельный HTTP-сервис казахского STT для GPU-сервера АФМ. Выносит модель Whisper-kk
из оркестратора, чтобы киоск/оркестратор оставались лёгкими (без torch). По контракту
он **совместим со STT-сервером АФМ** — оркестратору достаточно указать `STT_KK_URL`.

## Что это за модель
- **`shyngys879/kazakh-whisper-large-v3-turbo`** (HuggingFace) — Whisper-large-v3-turbo,
  дообученный под казахский. ~1.6 ГБ, самодостаточна (без отдельного вокодера/акцента).
- «Движок» — библиотека `transformers` (обычный pip). GPU желателен (на CPU медленно).

## Развёртывание (как с F5/Spark)
Запускать **из корня проекта** (`cd STT`). Отдельный venv `.venv-whisper-kk`.

1. **Установка** (на машине с интернетом — качает модель и зависимости):
   ```bash
   bash whisper_kk_server/setup.sh
   ```
   Для GPU подставьте колёса torch под вашу CUDA (как для F5/Spark), напр.
   `pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cu121`.
   Модель ляжет в `models/whisper-kazakh/`.

2. **Перенос на сервер АФМ** (офлайн): каталог проекта + `.venv-whisper-kk` +
   `models/whisper-kazakh/` — тем же способом, что и F5/Spark.

3. **Запуск**:
   ```bash
   bash whisper_kk_server/run.sh
   ```
   Или через systemd: `deploy/ai-dos-whisper-kk.service` (по образцу
   `ai-dos-spark.service`; `WHISPER_DEVICE=cuda`).

## Контракт (что дергает оркестратор)
```
POST /transcribe   multipart:  data=<аудиофайл>,  language=<строка>
   -> 200  {"status": "success", "data": "<распознанный текст>"}
GET  /health
   -> 200  {"status": "ok", "model": "...", "device": "cuda", "loaded": true}
```
Тот же формат ответа (`{"status":"success","data":...}`), что у STT-сервера АФМ, —
поэтому на стороне оркестратора код не меняется, только адрес.

## Переменные окружения
| Переменная | По умолчанию | Назначение |
|---|---|---|
| `WHISPER_KK_MODEL` | путь `models/whisper-kazakh` (в run.sh) | папка/HF-id модели |
| `WHISPER_DEVICE` | `cuda` (в run.sh) | `auto` \| `cpu` \| `cuda` \| `mps` |
| `WHISPER_KK_PORT` | `8813` | порт сервиса |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` (в run.sh) | без обращений в интернет |

## Проверка после запуска
```bash
curl http://localhost:8813/health
# распознать файл:
curl -s -F "data=@sample_kk.wav" -F "language=kazakh" http://localhost:8813/transcribe
# ждём {"status":"success","data":"<текст>"}
```

## Подключение к оркестратору
На машине оркестратора в `.env` (или окружении) указать адрес сервиса на GPU АФМ:
```
STT_KK_URL=http://192.168.165.2:8813/transcribe
```
Тогда казахское аудио уходит сюда, а НЕ в локальный Whisper (torch на киоске не нужен).
Русский STT продолжает идти на сервер АФМ (`STT_URL`).
