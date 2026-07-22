#!/usr/bin/env bash
# Запуск ВСЕГО пилотного стека ОДНОЙ командой:
#   bash run_api.sh
# Поднимает: spark (8809, казахский TTS) + rag (8077) + основной API (8000)
# + video_ui (8100, вариант 2 «видео-аватар» — выбранный путь пилота).
# Русский TTS — с GPU-сервера АФМ (F5_URL ниже), локальный f5_server НЕ поднимается.
# Останавливается по Ctrl-C — гасит все дочерние сервисы.
#
# Лишнее отключается флагами (по умолчанию поднимается всё нужное пилоту):
#   WITH_VIDEO_UI=0 bash run_api.sh   # без страницы видео-аватара (:8100)
#   WITH_SPARK=0    bash run_api.sh   # без казахского TTS (показ только на русском)
#   USE_LOCAL_F5=1  bash run_api.sh   # + локальный f5_server (когда GPU АФМ недоступен)
#   USE_ELEVEN=1    bash run_api.sh   # ВЕСЬ TTS (ru+kk) на ElevenLabs — нужен интернет
set -uo pipefail
cd "$(dirname "$0")"

# Проверяем venv ДО старта: иначе `source .venv/bin/activate` тихо провалится
# (нет set -e) и uvicorn запустится из чужого окружения PATH.
need_venv(){ [ -f "$1/bin/activate" ] || { echo "ОШИБКА: venv '$1' не создан — см. DEPLOY.md (ШАГ 2-5)"; exit 1; }; }
need_venv .venv
need_venv rag/.venv

# По выходу из скрипта убиваем всю группу процессов (дочерние сервисы).
trap 'kill 0' EXIT INT TERM

# Офлайн-флаги — ОБЯЗАТЕЛЬНО до любых фоновых `&` (Spark/F5) ниже: bash
# фиксирует окружение фонового процесса в момент его запуска (fork), а не в
# момент выполнения его тела, так что export, сделанный ПОСЛЕ `&`, до дочернего
# процесса не долетает. Раньше это стояло ниже, у активации основного venv, и
# Spark/локальный F5+RUAccent стартовали без гарантии офлайн-режима.
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
# STT: и русский, И казахский — на STT-сервер АФМ (:8804). Казахский Whisper
# больше не грузим: torch/модель на этой машине не нужны. Вернуть локальный
# Whisper для казахского: STT_KK_USE_WHISPER=true bash run_api.sh
export STT_KK_USE_WHISPER="${STT_KK_USE_WHISPER:-false}"

# USE_ELEVEN=1 — весь TTS (русский И казахский) на ElevenLabs (облако, НУЖЕН
# ИНТЕРНЕТ). Ключ и голос — в .env (ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID,
# один голос на оба языка). Гасит Spark и не зовёт F5 GPU — они не нужны.
# Ставим ДО экспортов TTS_PROVIDER/WITH_SPARK ниже, чтобы их ${VAR:-...} увидели.
USE_ELEVEN="${USE_ELEVEN:-0}"
if [ "$USE_ELEVEN" = "1" ]; then
  export TTS_PROVIDER="${TTS_PROVIDER:-eleven}"
  export TTS_KK_PROVIDER="${TTS_KK_PROVIDER:-eleven}"
  WITH_SPARK="${WITH_SPARK:-0}"
fi

# Казахский TTS (Spark, 8809) — свой venv, запускаем в фоне.
# Русский TTS по умолчанию берём с GPU-сервера АФМ (F5_URL ниже), локальный
# f5_server НЕ поднимаем. Чтобы вернуть локальный: USE_LOCAL_F5=1 F5_URL=http://localhost:8810/tts bash run_api.sh
USE_LOCAL_F5="${USE_LOCAL_F5:-0}"
if [ "$USE_LOCAL_F5" = "1" ]; then bash f5_server/run.sh & fi
# Казахский TTS. WITH_SPARK=0 — не поднимать (демо только на русском): модель
# Spark грузится долго и ест память, вхолостую её держать незачем.
WITH_SPARK="${WITH_SPARK:-1}"
if [ "$WITH_SPARK" = "1" ]; then bash spark_server/run.sh & fi

# RAG (8077) — ОТДЕЛЬНЫЙ venv (lightrag/torch конфликтуют с голосовым слоем).
# Подоболочка, чтобы активация его venv не протекла в основной shell.
# TIKTOKEN_CACHE_DIR: словарь токенизатора завендорен локально (rag/vendor/tiktoken)
# — иначе tiktoken лезет в интернет, которого в сети АФМ нет (см. rag/README).
# RAG слушает только localhost: единственный клиент — оркестратор на этой же машине.
( cd rag && source .venv/bin/activate \
  && export TIKTOKEN_CACHE_DIR="$PWD/vendor/tiktoken" \
  && uvicorn ragsvc.server:app --host 127.0.0.1 --port 8077 ) &

# Основной оркестратор (8000) — на venv голосового слоя.
source .venv/bin/activate

# TTS: русский -> F5 на GPU-сервере АФМ (multipart с клиентским референсом),
# казахский -> Spark. ${VAR:-...} — любой адрес/референс можно переопределить
# снаружи (напр. свой F5_URL или F5_REF_AUDIO). Референс шлётся в каждый запрос.
export TTS_PROVIDER="${TTS_PROVIDER:-f5}"
export F5_URL="${F5_URL:-http://192.168.165.2:8991/tts}"
# _padded: тот же голос + 250/300 мс тишины по краям. Оригинал начинался речью
# с нулевого сэмпла — F5 клонировал эту манеру и СТАРТОВАЛ РЕЗКО, съедая атаку
# первого слова («начало куска не с начала», отзыв 2026-07-17). С тишиной в
# референсе у генерации появился разгон и хвост (замерено по сэмплам).
export F5_REF_AUDIO="${F5_REF_AUDIO:-refs/ref_ru_f5_padded.wav}"
export F5_REF_TEXT="${F5_REF_TEXT:-@refs/ref_ru.txt}"
export TTS_KK_PROVIDER="${TTS_KK_PROVIDER:-spark}"
export SPARK_URL="${SPARK_URL:-http://localhost:8809/tts}"

# Источник ответа — внешний RAG-сервис
export RAG_URL="${RAG_URL:-http://localhost:8077/ask}"

# Вариант 2 «видео-аватар» (:8100) — ВЫБРАННЫЙ путь пилота, поэтому поднимаем
# вместе со стеком (страница проксирует вопросы на :8000, отдельный терминал
# больше не нужен). WITH_VIDEO_UI=0 — не поднимать.
WITH_VIDEO_UI="${WITH_VIDEO_UI:-1}"
if [ "$WITH_VIDEO_UI" = "1" ]; then bash video_ui/run.sh & fi

uvicorn app.main:app --host 0.0.0.0 --port 8000
