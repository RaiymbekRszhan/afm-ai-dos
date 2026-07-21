#!/usr/bin/env bash
# Установка Whisper-kk STT-сервера (казахский). Запускать ИЗ КОРНЯ проекта (cd STT),
# один раз, на машине С ИНТЕРНЕТОМ (скачивает модель + зависимости), затем
# перенести на GPU-сервер АФМ. Отдельный venv (несовместим с основным API).
#   bash whisper_kk_server/setup.sh
set -euo pipefail

python3 -m venv .venv-whisper-kk
source .venv-whisper-kk/bin/activate
pip install --upgrade pip

# torch: по умолчанию 2.8.0 (CPU/универсально). Для GPU АФМ подставь колёса под
# вашу CUDA — ТОЧНО так же, как делали для F5/Spark, напр.:
#   pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cu121
pip install torch==2.8.0

# transformers — «движок» Whisper; librosa/soundfile — чтение/ресемпл аудио.
pip install transformers librosa soundfile fastapi "uvicorn[standard]" "huggingface_hub[cli]"

# Модель Whisper-kk (~1.6 ГБ, самодостаточна — без вокодера/акцента).
hf download shyngys879/kazakh-whisper-large-v3-turbo --local-dir models/whisper-kazakh

echo ""
echo "✅ Готово. Запуск: bash whisper_kk_server/run.sh"
