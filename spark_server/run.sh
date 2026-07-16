#!/usr/bin/env bash
# Запуск Spark-TTS-сервера. Запускать ИЗ КОРНЯ проекта (cd STT).
set -euo pipefail
source .venv-spark/bin/activate

export SPARK_REPO="$(pwd)/spark_tts_repo"
export SPARK_MODEL_DIR="$(pwd)/models/spark-kazakh"
export SPARK_DEVICE="${SPARK_DEVICE:-cpu}"  # GPU: задай SPARK_DEVICE=cuda (env/systemd) или поставь здесь
export SPARK_GENDER="male"          # male | female (голос по умолчанию)
# Темп речи: very_low|low|moderate|high|very_high. ДЕРЖИМ moderate — не трогать.
#
# Соблазн разогнать речь тут выглядит логичным, но это плохой размен:
#   very_high — ломает ВСЕГДА: вырожденная генерация (0 токенов), звука нет вовсе;
#   high      — даёт всего ~10% (8.00 c -> 7.10 c), но выдаёт вырожденную генерацию
#               СЛУЧАЙНО: один и тот же текст то озвучивается, то падает с 422 —
#               а 422 на одном куске рушит весь ответ;
#   moderate  — единственная настройка, на которой сквозной тест прошёл без отказов.
# Разгонять надо ПЛЕЕРОМ: SPEECH_RATE в video_ui/static/index.html ускоряет ровно
# во столько, сколько задано, ничего не стоит (звук уже готов) и не может уронить
# синтез. moderate/1.4 = 5.71 c против high/1.25 = 5.68 c — на слух то же самое,
# но без риска. Скорость берём оттуда, надёжность — отсюда.
#
# Работает ТОЛЬКО в режиме gender (cloning=false): если задать SPARK_SPEAKER_WAV
# (клонирование), темп берётся из образца, а speed молча игнорируется.
export SPARK_SPEED="${SPARK_SPEED:-moderate}"
# Spark стохастически выдаёт вырожденную генерацию (0 токенов вместо звука) —
# один и тот же текст то синтезируется, то нет. Дефолтных 3 попыток мало: если все
# три подряд пустые, кусок падает с 422 и рушит ВЕСЬ ответ. Ретрай стоит секунды,
# отказ — всего ответа, поэтому берём с запасом (6 попыток).
export SPARK_RETRIES="${SPARK_RETRIES:-5}"
# Клонирование ВЫКЛЮЧЕНО (голос по умолчанию — стабильнее и чище).
# Чтобы вернуть клон — раскомментируй обе строки ниже:
# export SPARK_SPEAKER_WAV="$(pwd)/refs/ref_kk.wav"
# export SPARK_SPEAKER_TEXT="$(cat refs/ref_kk.txt 2>/dev/null)"
export SPARK_PORT="8809"

python spark_server/server.py
