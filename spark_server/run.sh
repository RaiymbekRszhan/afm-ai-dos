#!/usr/bin/env bash
# Запуск Spark-TTS-сервера. Запускать ИЗ КОРНЯ проекта (cd STT).
set -euo pipefail
source .venv-spark/bin/activate

export SPARK_REPO="$(pwd)/spark_tts_repo"
export SPARK_MODEL_DIR="$(pwd)/models/spark-kazakh"
export SPARK_DEVICE="${SPARK_DEVICE:-cpu}"  # GPU: задай SPARK_DEVICE=cuda (env/systemd) или поставь здесь
export SPARK_GENDER="male"          # male | female (голос по умолчанию)
# Клонирование ВЫКЛЮЧЕНО (голос по умолчанию — стабильнее и чище).
# Чтобы вернуть клон — раскомментируй обе строки ниже:
# export SPARK_SPEAKER_WAV="$(pwd)/refs/ref_kk.wav"
# export SPARK_SPEAKER_TEXT="$(cat refs/ref_kk.txt 2>/dev/null)"
export SPARK_PORT="8809"

python spark_server/server.py
