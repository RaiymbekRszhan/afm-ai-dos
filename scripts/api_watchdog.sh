#!/usr/bin/env bash
# Сторож оркестратора для работы 24/7: Restart=always в юните ловит только
# ПАДЕНИЕ процесса; этот скрипт ловит «висит, но не отвечает» (застрявший
# GPU-вызов, дедлок): /health не отвечает MAX_FAILS раз подряд ->
# systemctl restart ai-dos-api. Запускается из ai-dos-watchdog.timer (раз в
# 2 мин); счётчик неудач живёт в /run и сбрасывается при ребуте.
set -u

URL="${AIDOS_HEALTH_URL:-http://127.0.0.1:8000/health}"
UNIT="${AIDOS_UNIT:-ai-dos-api}"
MAX_FAILS="${AIDOS_MAX_FAILS:-3}"
STATE="/run/ai-dos-watchdog.fails"

# -m 30: health опрашивает RAG/TTS, при их деградации отвечает небыстро
if curl -fsS -m 30 "$URL" >/dev/null 2>&1; then
    echo 0 > "$STATE"
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
echo "health FAIL ${fails}/${MAX_FAILS} ($URL)"

if [ "$fails" -ge "$MAX_FAILS" ]; then
    echo 0 > "$STATE"
    echo "перезапускаю $UNIT"
    systemctl restart "$UNIT"
fi
