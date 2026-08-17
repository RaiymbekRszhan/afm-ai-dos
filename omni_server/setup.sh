#!/usr/bin/env bash
# Установка KazakhTTS-OmniVoice. Запускать ИЗ КОРНЯ проекта (cd STT), один раз,
# ТАМ, ГДЕ ЕСТЬ ИНТЕРНЕТ (pypi + huggingface.co):
#   bash omni_server/setup.sh
# Для офлайн-ноды сначала собери бандл: bash omni_server/bundle.sh (на машине с
# интернетом), потом ставь по omni_server/README.md.
set -euo pipefail

# 1. Изолированный venv: omnivoice тянет transformers >= 5.3, а Spark сидит на
#    4.46 — в один venv они не сойдутся.
python3 -m venv .venv-omni
source .venv-omni/bin/activate
PIP="pip"; command -v uv >/dev/null 2>&1 && PIP="uv pip"
pip install --upgrade pip

# 2. Библиотека инференса + обёртка-сервер.
$PIP install omnivoice==0.2.1 soundfile fastapi "uvicorn[standard]" pydantic "huggingface_hub[cli]"

# 3. Модель (2,3 ГБ) + аудио-токенизатор (768 МБ).
#    ⚠️ Токенизатор — ОТДЕЛЬНАЯ модель: from_pretrained тянет
#    eustlb/higgs-audio-v2-tokenizer, если рядом с чекпоинтом нет папки
#    audio_tokenizer/. Кладём его туда сразу — тогда сервис стартует и с
#    HF_HUB_OFFLINE=1, и без домашнего HF-кэша.
hf download shyngys879/KazakhTTS-OmniVoice --local-dir models/omnivoice-kazakh
hf download eustlb/higgs-audio-v2-tokenizer --local-dir models/omnivoice-kazakh/audio_tokenizer

echo ""
echo "✅ Готово. Запуск: bash omni_server/run.sh"
