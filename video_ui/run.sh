#!/usr/bin/env bash
# Вариант 2 («видео-аватар») — демо-страница на :8100 РЯДОМ с рабочим вариантом (:8000).
# Рабочий бэкенд не трогаем: вопросы проксируются на него, поэтому сначала подними
# основной стек (bash run_api.sh), а этот скрипт — вторым, в отдельном терминале.
#
#   bash video_ui/run.sh
#   AIDOS_BACKEND=http://192.168.23.120:8000 bash video_ui/run.sh   # бэкенд на другой машине
#   VIDEO_UI_PORT=8101 bash video_ui/run.sh                         # другой порт
set -euo pipefail
cd "$(dirname "$0")/.."

# Новых зависимостей нет — берём основной venv (fastapi/uvicorn/httpx/multipart уже там).
source .venv/bin/activate

export AIDOS_BACKEND="${AIDOS_BACKEND:-http://localhost:8000}"
PORT="${VIDEO_UI_PORT:-8100}"

echo "[video_ui] вариант 2 (видео-аватар) → http://localhost:${PORT}/"
echo "[video_ui] вопросы проксирую на ${AIDOS_BACKEND}"
exec uvicorn video_ui.server:app --host 0.0.0.0 --port "${PORT}"
