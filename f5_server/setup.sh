#!/usr/bin/env bash
# Установка F5-TTS-сервера (русский TTS). Запускать ИЗ КОРНЯ проекта (cd STT),
# один раз, на Wi-Fi (нужен интернет: pip + загрузка модели/вокодера/ruaccent).
#   bash f5_server/setup.sh
set -euo pipefail

# f5-tts официально работает на Python 3.10-3.12 (на 3.13 часто не встаёт).
# Берём первый доступный из 3.11/3.12, иначе системный python3 (на свой риск).
PY=""
for cand in python3.11 python3.12 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
echo "[setup] использую интерпретатор: $PY ($($PY --version 2>&1))"

# 1. Изолированный venv (несовместим с основным API)
"$PY" -m venv .venv-f5
source .venv-f5/bin/activate
PIP="pip"; command -v uv >/dev/null 2>&1 && PIP="uv pip"
pip install --upgrade pip

# 2. Зависимости (f5-tts сам тянет torch/torchaudio/vocos/transformers)
$PIP install -r f5_server/requirements.txt

# 2a. КРИТИЧНО: f5-tts тянет bleeding-edge torch 2.12 / torchaudio 2.11 / torchcodec.
#     torchcodec для I/O требует ffmpeg SHARED-LIBS (не бинарник) — на машине без них
#     падает ВСЁ аудио (torchaudio.load). Откатываемся на проверенный в проекте стек
#     torch 2.8.0 / torchaudio 2.8.0 (там есть soundfile-бэкенд, torchcodec не нужен).
$PIP install "torch==2.8.0" "torchaudio==2.8.0"
pip uninstall -y torchcodec 2>/dev/null || true

# 2b. ffmpeg-бинарник (через pip, без brew) — НЕ нужен для синтеза (референс задан
#     транскриптом refs/ref_ru.txt), но убирает варнинг pydub и нужен, если будешь
#     ПЕРЕснимать транскрипт для другого референса (f5_server/transcribe_ref.py).
$PIP install imageio-ffmpeg
FFEXE="$(python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
ln -sf "$FFEXE" .venv-f5/bin/ffmpeg

# 3. Модель Misha24-10/F5-TTS_RUSSIAN — тянем только нужное:
#    vocab.txt (один на все версии) + чекпойнт v2 (рекомендован в README модели).
#    Чтобы попробовать другую версию — скачай её .safetensors и укажи в run.sh.
hf download Misha24-10/F5-TTS_RUSSIAN \
  F5TTS_v1_Base/vocab.txt \
  F5TTS_v1_Base_v2/model_last_inference.safetensors \
  --local-dir models/f5-russian

# 4. Прогрев кэшей для офлайн-работы (сеть АФМ без интернета):
#    - вокодер Vocos (его f5-tts качает с HF при первом инференсе)
#    - модели RUAccent
echo "[setup] кэширую вокодер Vocos..."
python -c "from f5_tts.infer.utils_infer import load_vocoder; load_vocoder()"
echo "[setup] кэширую RUAccent..."
python -c "from ruaccent import RUAccent; a=RUAccent(); a.load(omograph_model_size='turbo3', use_dictionary=True)"

echo ""
echo "✅ Готово. Запуск: bash f5_server/run.sh"
echo "   (Опц.) впиши транскрипт refs/ref_ru.wav в refs/ref_ru.txt — иначе F5"
echo "   авто-транскрибирует референс Whisper'ом (нужен интернет на первом старте)."
