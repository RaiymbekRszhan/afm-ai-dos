#!/usr/bin/env bash
# Установка Spark-TTS-Kazakh. Запускать ИЗ КОРНЯ проекта (cd STT), один раз, на Wi-Fi.
#   bash spark_server/setup.sh
set -euo pipefail

# 1. Изолированный venv (несовместим с основным API)
python3 -m venv .venv-spark
source .venv-spark/bin/activate
# uv ставит в разы быстрее; если его нет — обычный pip (ничего не меняется)
PIP="pip"; command -v uv >/dev/null 2>&1 && PIP="uv pip"
pip install --upgrade pip

# 2. Фреймворк Spark-TTS (код инференса)
git clone --depth 1 https://github.com/SparkAudio/Spark-TTS.git spark_tts_repo

# 3. Зависимости. ВАЖНО: torch 2.5.1 из их requirements НЕ ставится на Python 3.13,
#    подменяем на 2.8.0; gradio (webui) не нужен.
grep -ivE '^torch==|^torchaudio==|^gradio==' spark_tts_repo/requirements.txt > /tmp/spark_reqs.txt
$PIP install torch==2.8.0 torchaudio==2.8.0
$PIP install -r /tmp/spark_reqs.txt "huggingface_hub[cli]" fastapi "uvicorn[standard]" pydantic

# 4. Сама модель (LLM + BiCodec + wav2vec2, ~самодостаточна)
hf download ErnarBahat/Spark-TTS-Kazakh --local-dir models/spark-kazakh

echo ""
echo "✅ Готово. Запуск: bash spark_server/run.sh"
