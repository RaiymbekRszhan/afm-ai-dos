#!/usr/bin/env bash
# Запуск OmniVoice-сервера (казахский TTS). Запускать ИЗ КОРНЯ проекта (cd STT).
set -euo pipefail
source .venv-omni/bin/activate

export OMNI_MODEL="${OMNI_MODEL:-$(pwd)/models/omnivoice-kazakh}"
# auto = cuda -> mps -> cpu. На GPU-ноде АФМ ставь cuda явно (юнит это и делает).
export OMNI_DEVICE="${OMNI_DEVICE:-auto}"
export OMNI_LANGUAGE="${OMNI_LANGUAGE:-Kazakh}"

# Голос: КЛОНИРОВАНИЕ с образца сотрудника АФМ (refs/ref_kk_omni.wav — тот же
# голос, что снят для казахского). В отличие от Spark, где клон пришлось выключить
# ради стабильности, здесь он не мешает: промпт считается ОДИН раз на старте
# (create_voice_clone_prompt) и дальше ничего не стоит.
#
# ⚠️ OMNI_SPEAKER_TEXT обязан ТОЧНО совпадать с тем, что произносится в WAV —
# иначе клон плывёт. При первом старте рядом с образцом появится .omni.pt; чтобы
# следующий старт не пересчитывал промпт, укажи его в OMNI_PROMPT.
export OMNI_SPEAKER_WAV="${OMNI_SPEAKER_WAV:-$(pwd)/refs/ref_kk_omni.wav}"
export OMNI_SPEAKER_TEXT="${OMNI_SPEAKER_TEXT:-@$(pwd)/refs/ref_kk_omni.txt}"
# Голос без образца (voice design) — если клон почему-то не нужен:
#   unset OMNI_SPEAKER_WAV; export OMNI_INSTRUCT="A calm middle-aged male voice"

# Темп: 1.0 = как оценит модель. В отличие от SPARK_SPEED эту ручку крутить МОЖНО
# (модель не авторегрессивная, вырожденной генерации от неё не бывает), но общий
# разгон речи всё равно держим на плеере (SPEECH_RATE в video_ui/static/index.html):
# он ничего не стоит и не может испортить синтез.
export OMNI_SPEED="${OMNI_SPEED:-0}"        # 0 = не передавать speed вовсе
export OMNI_NUM_STEP="${OMNI_NUM_STEP:-32}" # меньше шагов = быстрее и грязнее
export OMNI_GUIDANCE="${OMNI_GUIDANCE:-2.0}"

export OMNI_PORT="${OMNI_PORT:-8811}"
# Локально слушаем только 127.0.0.1; на GPU-ноде АФМ юнит ставит OMNI_HOST=0.0.0.0.
export OMNI_HOST="${OMNI_HOST:-127.0.0.1}"

python omni_server/server.py
