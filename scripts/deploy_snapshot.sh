#!/usr/bin/env bash
# ⚠️ ЗАПАСНОЙ способ обновления. С 2026-07-29 на сервере АФМ есть git, и
# основной путь — `bash scripts/update_server.sh` (сервер сам тянет с GitHub:
# доезжают удаления и переименования, состояние сервера — именованный коммит).
# Снимок нужен, только если GitHub из сети АФМ закроют или git снова пропадёт.
#
# Обновление кода на сервере БЕЗ git: снимок ветки одним архивом -> scp ->
# распаковка поверх, с бэкапом и без потери данных.
#
# ⚠️ ЧЕГО СНИМОК НЕ УМЕЕТ (обожглись 29.07): tar кладёт файлы поверх, но
# НЕ УДАЛЯЕТ исчезнувшие и не обновляет .git — индекс на сервере остаётся на
# старом коммите, новые файлы лежат неотслеживаемыми, и `git diff` потом
# показывает их как «удалённые целиком». После снимка индекс надо чинить:
#   ssh <сервер> 'cd <проект> && git fetch origin && git reset --hard origin/main'
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
# Скрипт НЕ перезапускает сервисы сам: решение «гасим приём граждан» должно
# быть осознанным. В конце печатает, что сделать.
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

  # 1) починить индекс git (tar его не обновляет — см. шапку скрипта):
  ssh $USER@$HOST 'cd $DIR && git fetch origin && git reset --hard origin/main'

  # 2) зависимости, если правился requirements.txt:
  ssh $USER@$HOST 'cd $DIR && .venv/bin/pip install -r requirements.txt'

  # 3) перезапуск (машинно-зависимые ключи запуска — в /etc/default/ai-dos,
  #    в командную строку их подставлять НЕ надо):
  ssh $USER@$HOST 'systemctl restart ai-dos-rag ai-dos-api ai-dos-video-ui'
  ssh $USER@$HOST 'systemctl status ai-dos-rag ai-dos-api ai-dos-video-ui --no-pager'

Приёмка (API и RAG слушают 127.0.0.1, поэтому с самого сервера):
  ssh $USER@$HOST 'cd $DIR && UI=http://localhost bash scripts/healthcheck.sh'
  curl -s -o /dev/null -w '%{http_code}\\n' http://$HOST/

Откат, если что-то сломалось (на сервере):
  tar xzf ~/ai-dos-backup-$STAMP.tar.gz -C $(dirname "$DIR")
  systemctl restart ai-dos-rag ai-dos-api ai-dos-video-ui
EOF
