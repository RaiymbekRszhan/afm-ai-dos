# F5-TTS_RUSSIAN — сервер русского TTS

Качественный русский синтез с **правильными ударениями** (RUAccent). Отдельный
сервис: основной API зовёт его по HTTP. Контракт **тот же**, что у Spark
(`POST /tts {text, language} -> WAV`).

```
Основной API  ──HTTP──▶  F5-сервер (этот)  ──▶  F5-TTS_RUSSIAN + RUAccent
   POST /tts {"text","language"}                возвращает WAV
```

Модель: [Misha24-10/F5-TTS_RUSSIAN](https://huggingface.co/Misha24-10/F5-TTS_RUSSIAN),
арх `F5TTS_v1_Base`, по умолчанию версия `v2` (рекомендована в карточке модели).
Ударения ставит [RUAccent](https://github.com/Den4ikAI/ruaccent) — модель обучена
под разметку «+» после ударной гласной, это и даёт естественное произношение
(правильные ударения — ключевое для русского синтеза).

## Установка и запуск
```bash
cd STT
bash f5_server/setup.sh   # отдельный venv + f5-tts + модель + вокодер + RUAccent (нужен интернет)
bash f5_server/run.sh     # слушает порт 8810
```
Проверка:
```bash
curl -X POST http://localhost:8810/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"Здравствуйте! Это проверка синтеза речи.","language":"russian"}' --output test.wav
```

## Подключение к основному API
В `run_api.sh` / `.env` основного проекта:
```
TTS_PROVIDER=f5
F5_URL=http://localhost:8810/tts
```

## Голос (референс)
F5 синтезирует «в тембр» референс-аудио (zero-shot клон). Используем готовую пару:
- `refs/ref_ru_f5.wav` — образец голоса (≤12 c, иначе F5 режет сам);
- `refs/ref_ru.txt` — его транскрипт (ОБЯЗАТЕЛЕН: без него F5 авто-транскрибирует
  Whisper'ом, а это требует ffmpeg и тянет 1.6 ГБ).

Сменить голос → положи свой `refs/ref_ru.wav` и пересними пару:
`python f5_server/transcribe_ref.py` (обрежет до ≤12 c и снимет транскрипт).

## ⚠️ Версии / окружение
- f5-tts официально **Python 3.10-3.12** (на 3.13 часто не встаёт) — `setup.sh`
  поэтому предпочитает `python3.11/3.12`.
- F5 — диффузионная модель: на CPU медленная (~25-28 c/фраза; управляется
  `F5_NFE_STEP`, для CPU попробуй 16). На GPU (`F5_DEVICE=cuda`) — быстро и качественно.
- Офлайн (АФМ): вокодер Vocos и модели RUAccent кэшируются в `setup.sh`; на бою
  запускать с `HF_HUB_OFFLINE=1` (как остальные сервисы).
