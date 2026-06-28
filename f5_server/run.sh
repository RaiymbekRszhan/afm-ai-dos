#!/usr/bin/env bash
# Запуск F5-TTS-сервера русского TTS. Запускать ИЗ КОРНЯ проекта (cd STT).
set -euo pipefail
source .venv-f5/bin/activate

export F5_MODEL="F5TTS_v1_Base"   # арх модели (одна на все версии чекпойнтов)
export F5_CKPT_FILE="$(pwd)/models/f5-russian/F5TTS_v1_Base_v2/model_last_inference.safetensors"
export F5_VOCAB_FILE="$(pwd)/models/f5-russian/F5TTS_v1_Base/vocab.txt"

# Референс голоса (тембр). ref_ru_f5.wav = первые 11 c от ref_ru.wav (<12 c, F5 не
# обрезает), под него снят транскрипт refs/ref_ru.txt (см. f5_server/transcribe_ref.py).
export F5_REF_AUDIO="$(pwd)/refs/ref_ru_f5.wav"
# Транскрипт референса ОБЯЗАТЕЛЕН: иначе F5 авто-транскрибирует Whisper'ом, а это
# требует ffmpeg (на этой машине его нет) и тянет 1.6 ГБ. Файл уже подготовлен.
export F5_REF_TEXT="$(cat refs/ref_ru.txt 2>/dev/null || true)"

export F5_RUACCENT="1"             # 1 — ставить ударения (рекомендуется)
export F5_DEVICE="${F5_DEVICE:-cpu}"  # GPU: задай F5_DEVICE=cuda (env/systemd)
export F5_NFE_STEP="${F5_NFE_STEP:-16}"  # шагов диффузии: 16 на CPU, 32 на GPU (качественнее)
export F5_PORT="8810"

python f5_server/server.py
