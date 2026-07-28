#!/usr/bin/env bash
# Приёмка деплоя Ai-dos ОДНОЙ командой. Проверяет все сервисы (вкл. фронтенд
# :8100) и прогоняет ru+kk: ответ RAG по базе и озвучку TTS — печатает PASS/FAIL
# по каждому шагу.
# Запускать ПОСЛЕ старта стека (run_api.sh или systemd) — на самом сервере АФМ.
#
#   bash scripts/healthcheck.sh                 # сервисы + RAG-ответ + TTS (ru/kk)
#   bash scripts/healthcheck.sh --full          # + сквозной голос STT→RAG→TTS (по refs/*.wav)
#   HOST=192.168.1.50 bash scripts/healthcheck.sh   # проверить сервер по сети
#   SKIP_UI=1 bash scripts/healthcheck.sh       # без киоск-страницы (стек поднят с WITH_VIDEO_UI=0)
#
# Код выхода: 0 — все проверки прошли; 1 — есть провал (годится для systemd/CI).
set -uo pipefail
cd "$(dirname "$0")/.."          # корень проекта (нужен для refs/*.wav)

# --- Адреса (можно переопределить через переменные окружения) ----------------
HOST="${HOST:-localhost}"
API="${API:-http://$HOST:8000}"   # оркестратор
RAG="${RAG:-http://$HOST:8077}"   # RAG-сервис
UI="${UI:-http://$HOST:8100}"     # киоск-страница «видео-аватар» = фронтенд пилота
SKIP_UI="${SKIP_UI:-0}"           # 1 — стек поднят без :8100 (WITH_VIDEO_UI=0)
# Адреса TTS-серверов здесь НЕ нужны: доступность берём из tts.servers.* в
# $API/health (оркестратор пингует их по фактическим адресам сам, см. check_tts_srv).
FULL=0; [ "${1:-}" = "--full" ] && FULL=1
OUT="$(mktemp -d)"                # сюда складываем синтезированные WAV для прослушки

# --- Оформление (без цвета, если вывод не в терминал) ------------------------
if [ -t 1 ]; then G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else G=; R=; Y=; B=; N=; fi
PASS=0; FAIL=0
ok(){ PASS=$((PASS+1)); printf "  ${G}✔ PASS${N}  %s\n" "$1"; [ -n "${2:-}" ] && printf "          ${Y}%s${N}\n" "$2"; }
no(){ FAIL=$((FAIL+1)); printf "  ${R}✘ FAIL${N}  %s\n" "$1"; [ -n "${2:-}" ] && printf "          ${Y}%s${N}\n" "$2"; }
section(){ printf "\n${B}%s${N}\n" "$1"; }

# --- Помощники: разбор JSON и сборка payload (python3 гарантирован в проекте) -
jget(){ # jget <путь.через.точку>  — читает JSON из stdin, печатает значение или ""
  python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    for k in sys.argv[1].split("."):
        d = d[k]
    print(d if not isinstance(d, (dict, list)) else json.dumps(d, ensure_ascii=False))
except Exception:
    print("")
' "$1"
}
payload(){ # payload key1 val1 ...  -> JSON (значения из argv; true/false/null → JSON-типы)
  python3 -c '
import sys, json
def conv(v):
    return {"true": True, "false": False, "null": None}.get(v, v)
a = sys.argv[1:]
print(json.dumps({a[i]: conv(a[i+1]) for i in range(0, len(a), 2)}, ensure_ascii=False))
' "$@"
}
is_wav(){ [ -s "$1" ] && [ "$(head -c 4 "$1" 2>/dev/null)" = "RIFF" ]; }
# /voice отдаёт JSON {answer, audio_b64, ...} (N5). «ok», если ответ непустой и
# аудио (если есть) декодируется в WAV; audio_b64=null (TTS выкл) — тоже ок.
voice_ok(){ python3 -c '
import sys, json, base64
try:
    d = json.load(sys.stdin)
    ans = d.get("answer") or ""
    ab = d.get("audio_b64")
    audio_ok = ab is None or base64.b64decode(ab)[:4] == b"RIFF"
    print("ok" if ans and audio_ok else "")
except Exception:
    print("")
'; }

# --- Проверки ----------------------------------------------------------------
check_health(){ # check_health "Имя" URL  — ждёт {"status":"ok"} на /health
  local name="$1" url="$2" body
  body="$(curl -s --max-time 10 "$url/health" 2>/dev/null)" || true
  if [ -z "$body" ]; then no "$name — $url не отвечает" "сервис не запущен или порт закрыт"; return; fi
  if [ "$(printf '%s' "$body" | jget status)" = "ok" ]; then ok "$name — $url/health → ok"
  else no "$name — $url/health" "$(printf '%s' "$body" | head -c 200)"; fi
}

check_ui(){ # фронтенд (:8100): жив, видит бэкенд, ролики аватара на месте
  local body videos
  body="$(curl -s --max-time 10 "$UI/health" 2>/dev/null)" || true
  if [ -z "$body" ] || [ "$(printf '%s' "$body" | jget status)" != "ok" ]; then
    no "Киоск-страница (фронтенд) — $UI не отвечает" \
       "video_ui не запущен (WITH_VIDEO_UI=0 / юнит ai-dos-video-ui выключен?); если так и задумано — SKIP_UI=1"
    return
  fi
  ok "Киоск-страница (фронтенд) — $UI/health → ok"
  # Без этого гражданин увидит страницу, но на вопрос получит ошибку прокси.
  case "$(printf '%s' "$body" | jget backend_reachable)" in
    True|true) ok "Киоск видит бэкенд — $(printf '%s' "$body" | jget backend)";;
    *) no "Киоск НЕ видит бэкенд — $(printf '%s' "$body" | jget backend)" \
          "поправь AIDOS_BACKEND у video_ui (юнит/run.sh) или подними API";;
  esac
  # Ролики тяжёлые (~21 МБ) — при копировании через scp их легко забыть, и на
  # киоске будет чёрный экран при живом стеке (регистр имён проверяет сам сервис).
  videos="$(printf '%s' "$body" | jget videos)"
  case "$videos" in
    *false*|"") no "Ролики аватара не на месте" \
                   "$videos — чёрный экран; скопируй video_ui/static/video/*.mp4";;
    *) ok "Ролики аватара на месте (idle.mp4 + talk.mp4)";;
  esac
}

check_ask(){ # check_ask lang "вопрос"  — ждёт СОДЕРЖАТЕЛЬНЫЙ ответ RAG по базе
  local lang="$1" q="$2" body ans
  # По умолчанию > LLM_TIMEOUT в rag/ragsvc/config.py (240 с) — иначе curl обрывает
  # раньше, чем RAG-сервис успевает сам честно дождаться LLM (ложный FAIL).
  body="$(curl -s --max-time "${RAG_TIMEOUT:-260}" -X POST "$RAG/ask" \
        -H 'Content-Type: application/json' \
        --data "$(payload question "$q" lang "$lang" with_sources false)")" || true
  ans="$(printf '%s' "$body" | jget answer)"
  # Фраза-отказ (нет в базе) на приёмочный вопрос, который В базе ЕСТЬ, — это провал
  # поиска, а не «зелёный». Иначе healthcheck зелёный при неработающем RAG.
  case "$ans" in
    *"нет точной информации"*|*"нақты ақпарат жоқ"*)
      no "RAG ($lang) — ответ-отказ на вопрос из базы (поиск не работает?)" \
         "$(printf '%s' "$ans" | tr '\n' ' ' | head -c 90)…"; return;;
  esac
  if [ "${#ans}" -ge 20 ]; then ok "RAG ответил по базе ($lang) — ${#ans} симв." "$(printf '%s' "$ans" | tr '\n' ' ' | head -c 90)…"
  else no "RAG ($lang) — пустой/короткий ответ" "$(printf '%s' "$body" | head -c 200)"; fi
}

check_speak(){ # check_speak russian|kazakh "текст" имя_файла.wav
  local lang="$1" text="$2" file="$OUT/$3" code
  code="$(curl -s --max-time "${TTS_TIMEOUT:-180}" -o "$file" -w '%{http_code}' \
        -X POST "$API/speak" -H 'Content-Type: application/json' \
        --data "$(payload text "$text" language "$lang")")" || true
  if [ "$code" != "200" ]; then no "TTS $lang — HTTP $code" "$(head -c 200 "$file" 2>/dev/null)"; return; fi
  if is_wav "$file"; then ok "TTS $lang — WAV $(( $(wc -c < "$file") / 1024 )) КБ → $file"
  else no "TTS $lang — ответ не WAV" "$(head -c 120 "$file")"; fi
}

check_voice(){ # check_voice russian|kazakh refs/файл.wav имя_выхода.json
  local lang="$1" ref="$2" out="$OUT/$3" code
  if [ ! -f "$ref" ]; then no "Сквозной голос $lang — нет файла $ref"; return; fi
  code="$(curl -s --max-time "${VOICE_TIMEOUT:-300}" -o "$out" -w '%{http_code}' \
        -X POST "$API/voice" -F "data=@$ref" -F "language=$lang")" || true
  if [ "$code" != "200" ]; then no "Сквозной голос $lang — HTTP $code" "$(head -c 200 "$out")"; return; fi
  if [ "$(voice_ok < "$out")" = "ok" ]; then ok "Сквозной голос $lang (STT→RAG→TTS) — текст+WAV в JSON"
  else no "Сквозной голос $lang — в ответе нет текста/аудио" "$(head -c 200 "$out")"; fi
}

# --- Запуск ------------------------------------------------------------------
printf "${B}Приёмка Ai-dos${N}  (HOST=%s, режим=%s)\n" "$HOST" "$([ "$FULL" = 1 ] && echo "полный +голос" || echo "базовый")"

section "1. Сервисы живы"
check_health "Оркестратор API" "$API"
check_health "RAG-сервис"      "$RAG"
if [ "$SKIP_UI" = "1" ]; then
  printf "  ${Y}ℹ INFO${N}  Киоск-страница (:8100) — проверка пропущена (SKIP_UI=1)\n"
else
  check_ui
fi

# Какие TTS-провайдеры реально настроены (учитывает вариант «TTS как endpoint АФМ»).
# Если API не ответил — пропускаем (его FAIL уже выше, гадать про TTS не нужно).
api_health="$(curl -s --max-time 10 "$API/health" 2>/dev/null)" || true
if [ -n "$api_health" ]; then
  ru_prov="$(printf '%s' "$api_health" | jget tts.ru)"
  kk_prov="$(printf '%s' "$api_health" | jget tts.kk)"
  # Оркестратор УЖЕ пингует свои TTS-серверы по фактическим адресам (локальный
  # f5_server ИЛИ GPU-сервер АФМ) и кладёт статус в tts.servers.* — берём его
  # оттуда, а не гадаем URL (иначе при удалённом F5 ложный FAIL на localhost:8810).
  check_tts_srv(){ # check_tts_srv f5|spark "Имя"
    local key="$1" name="$2" status
    status="$(printf '%s' "$api_health" | jget "tts.servers.$key.status")"
    case "$(printf '%s' "$api_health" | jget "tts.servers.$key.reachable")" in
      # reachable = сервер ОТВЕТИЛ (даже 404: у F5 на GPU нет /health, но он жив).
      True|true) ok "$name — отвечает${status:+ (HTTP $status)}";;
      *) no "$name — недоступен" "$(printf '%s' "$api_health" | jget "tts.servers.$key.error")";;
    esac
  }
  case " $ru_prov $kk_prov " in *" f5 "*)    check_tts_srv f5    "F5 (русский TTS)";;    esac
  case " $ru_prov $kk_prov " in *" spark "*) check_tts_srv spark "Spark (казахский TTS)";; esac
  case " $ru_prov $kk_prov " in
    *f5*|*spark*) ;;
    *) printf "  ${Y}ℹ INFO${N}  TTS внешний (ru=%s, kk=%s) — локальные TTS-серверы не проверяю\n" "${ru_prov:-?}" "${kk_prov:-?}";;
  esac
fi

section "2. RAG отвечает строго по базе (ru + kk)"
check_ask ru "Какой порог по операциям с ювелирными изделиями?"
check_ask kk "Қаржы мониторингі субъектілері кімдер?"

section "3. TTS озвучивает (ru + kk)"
check_speak russian "Здравствуйте! Это проверка озвучивания." ru.wav
check_speak kazakh  "Сәлеметсіз бе! Бұл тексеру."             kk.wav

if [ "$FULL" = 1 ]; then
  section "4. Сквозной голос: аудио → STT → RAG → TTS (по образцам refs/)"
  check_voice russian refs/ref_ru.wav voice_ru.json
  check_voice kazakh  refs/ref_kk.wav voice_kk.json
fi

# --- Итог --------------------------------------------------------------------
section "Итог"
printf "  Прошло: ${G}%d${N}   Провалов: %s%d${N}   Всего: %d\n" \
  "$PASS" "$([ "$FAIL" -gt 0 ] && echo "$R" || echo "$G")" "$FAIL" "$((PASS + FAIL))"
printf "  Аудио для прослушки: %s\n" "$OUT"
if command -v afplay >/dev/null 2>&1; then printf "  Проиграть (mac):  afplay %s/ru.wav\n" "$OUT"
elif command -v aplay  >/dev/null 2>&1; then printf "  Проиграть (linux): aplay %s/ru.wav\n" "$OUT"; fi
[ "$FAIL" -eq 0 ] && printf "\n  ${G}${B}ГОТОВО: деплой принят.${N}\n\n" || printf "\n  ${R}${B}ЕСТЬ ПРОВАЛЫ — см. подсказки выше.${N}\n\n"
exit $([ "$FAIL" -eq 0 ] && echo 0 || echo 1)
