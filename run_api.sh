#!/usr/bin/env bash
# Запуск ВСЕГО пилотного стека ОДНОЙ командой:
#   bash run_api.sh
# Поднимает: rag (8077) + основной API (8000) + video_ui (8100, вариант 2
# «видео-аватар» — выбранный путь пилота).
# TTS по умолчанию: РУССКИЙ — F5 (GPU-сервер АФМ :8991), КАЗАХСКИЙ — OmniVoice
# (GPU-сервер АФМ :8993, офлайн, бесплатно) с фолбэком на ElevenLabs (облако).
# Локальные f5_server/omni_server/spark_server НЕ поднимаются.
# Ctrl-C гасит все дочерние сервисы.
#
# Варианты (по умолчанию поднимается всё нужное пилоту):
#   WITH_VIDEO_UI=0       bash run_api.sh   # без страницы видео-аватара (:8100)
#   USE_LOCAL_F5=1        bash run_api.sh   # русский на ЛОКАЛЬНОМ f5_server (GPU АФМ недоступен)
#   USE_LOCAL_OMNI=1      bash run_api.sh   # казахский на ЛОКАЛЬНОМ OmniVoice (GPU АФМ недоступен)
#   TTS_KK_PROVIDER=eleven bash run_api.sh  # казахский в облаке (eleven_v3)
#   TTS_KK_PROVIDER=spark bash run_api.sh   # казахский на ПРЕЖНЕМ Spark (GPU АФМ :8992)
#   USE_LOCAL_SPARK=1     bash run_api.sh   # казахский на ЛОКАЛЬНОМ Spark (GPU АФМ недоступен)
#   USE_ELEVEN=1          bash run_api.sh   # ВЕСЬ TTS (ru+kk) на ElevenLabs
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
# один голос на оба языка). Не зовёт F5/Spark GPU — они не нужны.
# Ставим ДО экспортов TTS_PROVIDER/TTS_KK_PROVIDER ниже, чтобы их ${VAR:-...} увидели.
USE_ELEVEN="${USE_ELEVEN:-0}"
if [ "$USE_ELEVEN" = "1" ]; then
  export TTS_PROVIDER="${TTS_PROVIDER:-eleven}"
  export TTS_KK_PROVIDER="${TTS_KK_PROVIDER:-eleven}"
  # Оба языка в облаке -> оба остаются без звука, если интернет пропадёт. Даём
  # каждому офлайн-страховку: русский досинтезирует F5, казахский — OmniVoice
  # (адреса ниже экспортируются в любом режиме). Фолбэк срабатывает автоматом,
  # провайдер в ответе /voice будет ФАКТИЧЕСКИЙ — киоск подберёт темп.
  export TTS_FALLBACK="${TTS_FALLBACK:-f5}"
  export TTS_KK_FALLBACK="${TTS_KK_FALLBACK:-omni}"
fi

# Русский TTS по умолчанию — с GPU-сервера АФМ (F5_URL :8991), казахский — OmniVoice
# (TTS_KK_PROVIDER ниже). Локальные серверы НЕ поднимаем (модели грузятся долго и едят
# память). Вернуть локальный — USE_LOCAL_F5=1 / USE_LOCAL_OMNI=1 bash run_api.sh:
# адреса и провайдер переключаются автоматически (блок TTS ниже).
USE_LOCAL_F5="${USE_LOCAL_F5:-0}"
if [ "$USE_LOCAL_F5" = "1" ]; then bash f5_server/run.sh & fi
USE_LOCAL_SPARK="${USE_LOCAL_SPARK:-0}"
if [ "$USE_LOCAL_SPARK" = "1" ]; then bash spark_server/run.sh & fi
USE_LOCAL_OMNI="${USE_LOCAL_OMNI:-0}"
if [ "$USE_LOCAL_OMNI" = "1" ]; then bash omni_server/run.sh & fi

# RAG (8077) — ОТДЕЛЬНЫЙ venv (lightrag/torch конфликтуют с голосовым слоем).
# Подоболочка, чтобы активация его venv не протекла в основной shell.
# TIKTOKEN_CACHE_DIR: словарь токенизатора завендорен локально (rag/vendor/tiktoken)
# — иначе tiktoken лезет в интернет, которого в сети АФМ нет (см. rag/README).
# RAG слушает только localhost: единственный клиент — оркестратор на этой же машине.
# TIKTOKEN_CACHE_DIR переопределяем снаружи: на сервере АФМ рабочий кэш пришлось
# пересоздать в другом каталоге (версия из репозитория не подошла), и раньше это
# держалось правкой ЭТОГО файла — правка терялась при каждом обновлении, а RAG
# после неё лез за словарём в интернет и падал на TLS-прокси.
( cd rag && source .venv/bin/activate \
  && export TIKTOKEN_CACHE_DIR="${TIKTOKEN_CACHE_DIR:-$PWD/vendor/tiktoken}" \
  && uvicorn ragsvc.server:app --host "${RAG_HOST:-127.0.0.1}" --port 8077 ) &

# Основной оркестратор (8000) — на venv голосового слоя.
source .venv/bin/activate

# TTS: русский -> F5, казахский -> OmniVoice. ${VAR:-...} — адрес/референс можно
# переопределить снаружи. Контракт F5 (JSON vs multipart) выбирает _f5 по факту
# заданности F5_REF_AUDIO, поэтому разводим его ПО РЕЖИМУ (иначе локальный
# JSON-сервер получал бы multipart и падал в 422 — весь /voice -> 502).
export TTS_PROVIDER="${TTS_PROVIDER:-f5}"
if [ "$USE_LOCAL_F5" = "1" ]; then
  # Локальный f5_server принимает ТОЛЬКО JSON {text, language}; референс задан
  # НА СЕРВЕРЕ (f5_server/run.sh). Оркестратор идёт JSON-контрактом, а он
  # выбирается ПУСТЫМ F5_REF_AUDIO — непустой заставил бы _f5 слать multipart.
  export F5_URL="${F5_URL:-http://localhost:8810/tts}"
  export F5_REF_AUDIO=""
  export F5_REF_TEXT=""
else
  # Русский TTS с GPU-сервера АФМ: multipart {ref_audio, ref_text, gen_text},
  # клиентский референс шлётся в каждый запрос.
  export F5_URL="${F5_URL:-http://192.168.165.2:8991/tts}"
  # _padded: тот же голос + 250/300 мс тишины по краям. Оригинал начинался речью
  # с нулевого сэмпла — F5 клонировал эту манеру и СТАРТОВАЛ РЕЗКО, съедая атаку
  # первого слова («начало куска не с начала», отзыв 2026-07-17). С тишиной в
  # референсе у генерации появился разгон и хвост (замерено по сэмплам).
  export F5_REF_AUDIO="${F5_REF_AUDIO:-refs/ref_ru_f5_padded.wav}"
  export F5_REF_TEXT="${F5_REF_TEXT:-@refs/ref_ru.txt}"
fi
# Казахский TTS по умолчанию — OmniVoice (KazakhTTS-OmniVoice на GPU АФМ :8992):
# офлайн, бесплатно, интернет не нужен. Облако осталось переключателем:
# TTS_KK_PROVIDER=eleven.
# ⚠️ Порт 8992 — ТОТ ЖЕ, на котором раньше отвечал Spark: на ноде его заменили
# OmniVoice (контракт совпадает). Поэтому адреса Spark по умолчанию БОЛЬШЕ НЕТ —
# иначе фолбэк «в Spark» молча уходил бы в тот же OmniVoice.
# Провайдер: заданный снаружи побеждает; иначе — тот движок, который подняли
# локально; иначе — omni на GPU АФМ.
if [ -n "${TTS_KK_PROVIDER:-}" ]; then
  export TTS_KK_PROVIDER
elif [ "$USE_LOCAL_SPARK" = "1" ]; then
  export TTS_KK_PROVIDER="spark"
else
  export TTS_KK_PROVIDER="omni"
fi
# Адреса обоих казахских движков экспортируем всегда: используется тот, чей
# провайдер выбран (второй просто не дёргается) — так фолбэк не остаётся без URL.
if [ "$USE_LOCAL_OMNI" = "1" ]; then
  export OMNI_URL="${OMNI_URL:-http://localhost:8811/tts}"
else
  export OMNI_URL="${OMNI_URL:-http://192.168.165.2:8992/tts}"
fi
if [ "$USE_LOCAL_SPARK" = "1" ]; then
  export SPARK_URL="${SPARK_URL:-http://localhost:8809/tts}"
fi
# Страховка на случай, когда GPU-нода недоступна: досинтезируем в облаке. Раньше
# было наоборот (облако основное, офлайн-Spark страховкой) — теперь основной путь
# офлайновый, а платное облако осталось запасным. Пустая строка отключает фолбэк.
export TTS_KK_FALLBACK="${TTS_KK_FALLBACK:-eleven}"

# Источник ответа — внешний RAG-сервис
export RAG_URL="${RAG_URL:-http://localhost:8077/ask}"

# Вариант 2 «видео-аватар» (:8100) — ВЫБРАННЫЙ путь пилота, поэтому поднимаем
# вместе со стеком (страница проксирует вопросы на :8000, отдельный терминал
# больше не нужен). WITH_VIDEO_UI=0 — не поднимать.
WITH_VIDEO_UI="${WITH_VIDEO_UI:-1}"
if [ "$WITH_VIDEO_UI" = "1" ]; then bash video_ui/run.sh & fi

# По умолчанию оркестратор слушает 127.0.0.1: снаружи к нему ходит только
# video_ui (:8100) с той же машины (см. N6). Открыть на всю сеть (за TLS-прокси):
# API_HOST=0.0.0.0 bash run_api.sh (в systemd — Environment=API_HOST=0.0.0.0).
uvicorn app.main:app --host "${API_HOST:-127.0.0.1}" --port 8000
