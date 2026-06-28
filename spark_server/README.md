# Spark-TTS-Kazakh — сервер казахского TTS

Отдельный сервис: качественный казахский синтез (Spark-TTS, LLM-based, с клонированием
голоса). Основной API зовёт его по HTTP. Вынесен отдельно, т.к. версии (torch 2.8,
transformers 4.46) несовместимы с основным API и другими TTS-серверами.

```
Основной API  ──HTTP──▶  Spark-сервер (этот)  ──▶  Spark-TTS-Kazakh
   POST /tts {"text","language"}                возвращает WAV
```

## Установка и запуск (из корня проекта)
```bash
bash spark_server/setup.sh    # venv + фреймворк + модель (нужен интернет)
bash spark_server/run.sh      # слушает порт 8809
```
Проверка:
```bash
curl -X POST http://localhost:8809/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"Сәлеметсіз бе!","language":"kazakh"}' --output test.wav
```

## Подключение к основному API
В `.env` основного проекта:
```
TTS_KK_PROVIDER=spark
SPARK_URL=http://localhost:8809/tts
```

## Голос
- Без референса — режим по полу/тону (`SPARK_GENDER=male|female`, `SPARK_PITCH`, `SPARK_SPEED`).
- **Клонирование:** дай образец 3–10 сек (`SPARK_SPEAKER_WAV=ref.wav`) — заговорит этим голосом.

## Где запускать
- CPU работает, но LLM-генерация медленная (~несколько сек/фразу).
- На GPU (`SPARK_DEVICE=cuda`) — заметно быстрее. Для боя — GPU.

## ⚠️ Зависит от папок в корне проекта
`spark_tts_repo/` (код фреймворка) и `models/spark-kazakh/` (модель) — НЕ удалять.
torch 2.8 вместо пинованного 2.5.1 (его нет под Python 3.13).
