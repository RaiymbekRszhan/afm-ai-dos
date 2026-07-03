#!/usr/bin/env bash
# Запуск ВСЕГО голосового стека одной командой:
#   f5 (8810) + spark (8809) + rag (8077) + основной API (8000).
#   bash run_api.sh
# Останавливается по Ctrl-C — гасит все дочерние сервисы.
set -uo pipefail
cd "$(dirname "$0")"

# По выходу из скрипта убиваем всю группу процессов (дочерние сервисы).
trap 'kill 0' EXIT INT TERM

# Русский TTS (F5, 8810) и казахский TTS (Spark, 8809) — у каждого свой venv
# и свой env внутри run.sh, поэтому просто запускаем их в фоне.
bash f5_server/run.sh &
bash spark_server/run.sh &

# RAG (8077) — ОТДЕЛЬНЫЙ venv (lightrag/torch конфликтуют с голосовым слоем).
# Подоболочка, чтобы активация его venv не протекла в основной shell.
# TIKTOKEN_CACHE_DIR: словарь токенизатора завендорен локально (rag/vendor/tiktoken)
# — иначе tiktoken лезет в интернет, которого в сети АФМ нет (см. rag/README).
( cd rag && source .venv/bin/activate \
  && export TIKTOKEN_CACHE_DIR="$PWD/vendor/tiktoken" \
  && uvicorn ragsvc.server:app --host 0.0.0.0 --port 8077 ) &

# Основной оркестратор (8000) — на venv голосового слоя.
source .venv/bin/activate

# STT: казахский -> локальный Whisper (из кэша, без интернета)
export STT_KK_USE_WHISPER=true
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

# TTS: русский -> F5-сервер, казахский -> Spark-сервер
export TTS_PROVIDER=f5
export F5_URL=http://localhost:8810/tts
export TTS_KK_PROVIDER=spark
export SPARK_URL=http://localhost:8809/tts

# Источник ответа — внешний RAG-сервис
export RAG_URL=http://localhost:8077/ask

uvicorn app.main:app --host 0.0.0.0 --port 8000
