#!/usr/bin/env bash
# Обновление кода на сервере АФМ БЕЗ git (его там нет): снимок ветки одним
# архивом -> scp -> распаковка поверх, с бэкапом и без потери данных.
#
#   bash scripts/deploy_snapshot.sh                      # main на 10.10.42.44
#   bash scripts/deploy_snapshot.sh --ref HEAD           # то, что в рабочем дереве закоммичено
#   bash scripts/deploy_snapshot.sh --host 10.10.42.44 --user root --dir /root/afm-ai-dos
#   bash scripts/deploy_snapshot.sh --dry-run            # показать план, ничего не делать
#
# ЧТО НЕ ТРОГАЕТСЯ (их нет в архиве, распаковка их не удаляет):
#   .env (секреты и топология), rag/rag_storage/ (индекс), .venv*/ (окружения),
#   logs/ (аналитика с ПДн).
# ЧТО ЗАТИРАЕТСЯ: все файлы под контролем git — включая run_api.sh и
# video_ui/run.sh. Если их правили на сервере (порты!), правки исчезнут:
# держи такие настройки в переменных окружения/юните systemd, а не в файлах.
#
# Скрипт НЕ перезапускает сервисы сам: стек живёт в tmux-сессии, и решение
# «гасим приём граждан» должно быть осознанным. В конце печатает, что сделать.
set -uo pipefail
cd "$(dirname "$0")/.."

HOST="10.10.42.44"; USER="root"; DIR="/root/afm-ai-dos"; REF="main"; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --user) USER="$2"; shift 2;;
    --dir)  DIR="$2";  shift 2;;
    --ref)  REF="$2";  shift 2;;
    --dry-run) DRY=1; shift;;
    *) echo "неизвестный аргумент: $1"; exit 1;;
  esac
done

STAMP="$(date +%F-%H%M)"
ARCHIVE="/tmp/ai-dos-${STAMP}.tar.gz"
REMOTE_TMP="/tmp/ai-dos-${STAMP}.tar.gz"

# Незакоммиченное в снимок НЕ попадёт (git archive берёт из коммита) — про это
# лучше узнать здесь, а не после «почему на сервере нет моей правки».
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠️  В рабочем дереве есть незакоммиченные изменения — в снимок они НЕ войдут:"
  git status --short | sed 's/^/    /'
  echo
fi

echo "=== Снимок $REF -> $USER@$HOST:$DIR ==="
git rev-parse --short "$REF" >/dev/null 2>&1 || { echo "нет такой ревизии: $REF"; exit 1; }
echo "  коммит:  $(git log -1 --format='%h %s' "$REF")"
if [ "$DRY" = 1 ]; then
  echo "  (dry-run: архив не собирается, ничего не отправляется)"
  exit 0
fi

git archive --format=tar.gz -o "$ARCHIVE" "$REF" || exit 1
echo "  архив:   $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

scp -q "$ARCHIVE" "$USER@$HOST:$REMOTE_TMP" || { echo "scp не прошёл"; exit 1; }
echo "  доставлен на сервер"

# Бэкап кода (без venv/индекса/логов — они не меняются и весят) и распаковка.
ssh "$USER@$HOST" DIR="$DIR" REMOTE_TMP="$REMOTE_TMP" STAMP="$STAMP" bash -s <<'REMOTE'
set -e
BACKUP="$HOME/ai-dos-backup-$STAMP.tar.gz"
tar czf "$BACKUP" -C "$(dirname "$DIR")" \
    --exclude='*/.venv' --exclude='*/.venv-*' --exclude='*/rag/.venv' \
    --exclude='*/rag/rag_storage' --exclude='*/logs' \
    "$(basename "$DIR")"
echo "  бэкап:   $BACKUP ($(du -h "$BACKUP" | cut -f1))"
tar xzf "$REMOTE_TMP" -C "$DIR"
rm -f "$REMOTE_TMP"
echo "  распаковано в $DIR"
REMOTE
[ $? -eq 0 ] || { echo "распаковка на сервере не прошла"; exit 1; }

cat <<EOF

Код обновлён. Дальше — руками, чтобы приём граждан прервался осознанно:

  ssh $USER@$HOST
  cd $DIR
  # зависимости могли поменяться (проверь, если правился requirements.txt):
  #   .venv/bin/pip install -r requirements.txt
  tmux attach -t mainpy
  #   Ctrl-C  -> дождаться приглашения -> строка запуска ЦЕЛИКОМ:
  #
  #   VIDEO_UI_PORT=80 TIKTOKEN_CACHE_DIR=/root/afm-ai-dos/vendor/tiktoken bash run_api.sh
  #
  #   Обе переменные обязательны на сервере АФМ и держатся ЗДЕСЬ, а не правкой
  #   файлов (иначе теряются при каждом обновлении):
  #     VIDEO_UI_PORT=80         иначе киоск-страница уедет на 8100 и все точки
  #                              увидят «недоступно»;
  #     TIKTOKEN_CACHE_DIR=...   рабочий словарь лежит НЕ там, где дефолт; без
  #                              него RAG полезет за ним в интернет и упадёт на
  #                              TLS-прокси, то есть не стартует.
  #   Ctrl-B, затем D                        <- выйти, оставив стек работать

Приёмка (с этой машины):
  HOST=$HOST bash scripts/healthcheck.sh
  curl -s http://$HOST/health

Откат, если что-то сломалось (на сервере):
  tar xzf ~/ai-dos-backup-$STAMP.tar.gz -C $(dirname "$DIR")
EOF
