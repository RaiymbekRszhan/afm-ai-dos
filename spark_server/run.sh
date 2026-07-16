#!/usr/bin/env bash
# Запуск Spark-TTS-сервера. Запускать ИЗ КОРНЯ проекта (cd STT).
set -euo pipefail
source .venv-spark/bin/activate

export SPARK_REPO="$(pwd)/spark_tts_repo"
export SPARK_MODEL_DIR="$(pwd)/models/spark-kazakh"
export SPARK_DEVICE="${SPARK_DEVICE:-cpu}"  # GPU: задай SPARK_DEVICE=cuda (env/systemd) или поставь здесь
export SPARK_GENDER="male"          # male | female (голос по умолчанию)
# Темп речи: very_low|low|moderate|high|very_high.
# На moderate казахский голос заметно тянет слова -> ставим high (замер на одном
# тексте: moderate 8.00 c -> high 7.22 c, т.е. шкала даёт лишь ~10% на ступень).
# ⚠️ very_high НЕ СТАВИТЬ: модель уходит в вырожденную генерацию (0 токенов),
# срабатывают ретраи и запрос висит до таймаута, звука нет вообще. high — потолок.
# Работает ТОЛЬКО в режиме gender (cloning=false): если задать SPARK_SPEAKER_WAV
# (клонирование), темп берётся из образца, а speed молча игнорируется.
# Нужно ещё быстрее — ускоряй воспроизведение на стороне плеера (SPEECH_RATE
# в video_ui/static/index.html): это точнее и не стоит времени генерации.
export SPARK_SPEED="${SPARK_SPEED:-high}"
# Клонирование ВЫКЛЮЧЕНО (голос по умолчанию — стабильнее и чище).
# Чтобы вернуть клон — раскомментируй обе строки ниже:
# export SPARK_SPEAKER_WAV="$(pwd)/refs/ref_kk.wav"
# export SPARK_SPEAKER_TEXT="$(cat refs/ref_kk.txt 2>/dev/null)"
export SPARK_PORT="8809"

python spark_server/server.py
