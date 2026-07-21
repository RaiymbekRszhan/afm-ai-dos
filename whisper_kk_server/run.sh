#!/usr/bin/env bash
# Запуск Whisper-kk STT-сервера. Запускать ИЗ КОРНЯ проекта (cd STT).
set -euo pipefail
source .venv-whisper-kk/bin/activate

# Грузим модель из локальной папки (скачана setup.sh) — чтобы работало ОФЛАЙН,
# без обращения к HuggingFace (в сети АФМ интернета нет).
export WHISPER_KK_MODEL="${WHISPER_KK_MODEL:-$(pwd)/models/whisper-kazakh}"
# GPU АФМ: cuda. На CPU-проверке — cpu (медленно). auto сам выберет.
export WHISPER_DEVICE="${WHISPER_DEVICE:-cuda}"
export WHISPER_KK_PORT="${WHISPER_KK_PORT:-8813}"
# Никаких обращений в интернет за моделью/токенайзером.
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

python whisper_kk_server/server.py
