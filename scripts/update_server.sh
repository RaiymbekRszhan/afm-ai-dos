#!/usr/bin/env bash
# Обновление кода на сервере АФМ ЧЕРЕЗ GIT — основной способ с 2026-07-29,
# когда на сервере наконец появился git (`apt install git`, Debian 13 trixie).
#
#   bash scripts/update_server.sh                  # подтянуть origin/main, перезапустить что нужно
#   bash scripts/update_server.sh --dry-run        # показать, что приедет, и выйти
#   bash scripts/update_server.sh --branch feat/x  # выкатить ветку
#   bash scripts/update_server.sh --no-restart     # только обновить файлы
#   bash scripts/update_server.sh --host 10.10.42.44 --user root --dir /root/afm-ai-dos
#
# ЧЕМ ОТЛИЧАЕТСЯ ОТ deploy_snapshot.sh. Снимок вёз ЛОКАЛЬНОЕ дерево одним
# архивом; здесь сервер берёт код С GITHUB сам. Следствия:
#   * НЕЗАПУШЕННЫЕ коммиты на сервер не попадут — скрипт про это предупреждает
#     ДО выкатки (у снимка такой проблемы не было, и это единственный минус);
#   * приезжают удаления и переименования, а не только изменённые файлы. Ровно
#     на этом 29.07 обожглись: шесть добавленных файлов доехали, но остались
#     НЕОТСЛЕЖИВАЕМЫМИ, и `git diff` показывал их как «удалённые целиком»;
#   * состояние сервера — всегда именованный коммит, а не дерево неизвестного
#     возраста: `git log -1` на сервере теперь говорит правду.
# deploy_snapshot.sh остаётся ЗАПАСНЫМ путём — на случай, если GitHub из сети
# АФМ закроют.
#
# ЧТО НЕ ТРОГАЕТСЯ: .env и .env.* (в .gitignore), rag/rag_storage/, .venv*/,
# logs/, vendor/ — reset --hard не удаляет неотслеживаемое, `git clean` не
# зовём сознательно. Юниты в /etc/systemd/system тоже не трогаются: там
# ОТДЕЛЬНЫЕ копии, подогнанные под этот сервер (пути /root/afm-ai-dos вместо
# /opt/ai-dos и User=root) — про их расхождение скрипт предупреждает отдельно.
set -uo pipefail
cd "$(dirname "$0")/.."

HOST="10.10.42.44"; USER="root"; DIR="/root/afm-ai-dos"; BRANCH="main"
DRY=0; RESTART=1
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2;;
    --user) USER="$2"; shift 2;;
    --dir)  DIR="$2";  shift 2;;
    --branch) BRANCH="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    --no-restart) RESTART=0; shift;;
    *) echo "неизвестный аргумент: $1"; exit 1;;
  esac
done

echo "=== Обновление $USER@$HOST:$DIR до origin/$BRANCH ==="

# Сервер тянет с GitHub, а не с этой машины: всё, что не запушено, до него не
# доедет. Молча выкатить «почти то, что я вижу» — худший исход, поэтому шумим.
if ! git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
  echo "⚠️  нет ветки origin/$BRANCH — сначала: git fetch, либо git push -u origin $BRANCH"; exit 1
fi
AHEAD="$(git rev-list --count "origin/$BRANCH..$BRANCH" 2>/dev/null || echo 0)"
if [ "$AHEAD" != "0" ]; then
  echo "⚠️  локально на $AHEAD коммит(ов) ВПЕРЕДИ origin/$BRANCH — они на сервер НЕ поедут:"
  git log --oneline "origin/$BRANCH..$BRANCH" | sed 's/^/    /'
  echo "    сначала: git push origin $BRANCH"
  echo
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠️  в рабочем дереве есть незакоммиченное — на сервер оно тоже НЕ поедет:"
  git status --short | sed 's/^/    /'
  echo
fi

if [ "$DRY" = 1 ]; then
  echo "  (dry-run: спрашиваю сервер, что у него сейчас, и выхожу)"
fi

ssh "$USER@$HOST" DIR="$DIR" BRANCH="$BRANCH" DRY="$DRY" RESTART="$RESTART" bash -s <<'REMOTE'
set -uo pipefail
cd "$DIR" || { echo "нет каталога $DIR"; exit 1; }
command -v git >/dev/null || { echo "на сервере нет git — используй scripts/deploy_snapshot.sh"; exit 1; }

OLD="$(git rev-parse HEAD)"
echo "  сейчас:  $(git log --oneline -1 HEAD)"

# Правки руками — единственное, что этот способ может стереть. Показываем их
# ДО reset, чтобы «а куда делось» не всплыло потом. .env/vendor/logs сюда не
# попадают: они неотслеживаемые, reset --hard их не касается.
DIRTY="$(git status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
  echo "  ⚠️  на сервере есть ПРАВКИ РУКАМИ, reset --hard их сотрёт:"
  echo "$DIRTY" | sed 's/^/      /'
fi

git fetch --quiet origin "$BRANCH" || { echo "  fetch не прошёл (сеть/TLS?)"; exit 1; }
NEW="$(git rev-parse "origin/$BRANCH")"

if [ "$OLD" = "$NEW" ] && [ -z "$DIRTY" ]; then
  echo "  уже актуально, делать нечего"; exit 0
fi

if [ "$OLD" != "$NEW" ]; then
  echo "  приедет:"
  git log --oneline "$OLD..$NEW" | sed 's/^/      /'
fi
CHANGED="$(git diff --name-only "$OLD" "$NEW")"

if [ "$DRY" = 1 ]; then
  echo "  (dry-run: ничего не менял)"; exit 0
fi

git reset --hard "origin/$BRANCH" >/dev/null || exit 1
echo "  теперь:  $(git log --oneline -1 HEAD)"
LEFT="$(git status --porcelain --untracked-files=no)"
[ -n "$LEFT" ] && { echo "  ⚠️  дерево осталось грязным:"; echo "$LEFT" | sed 's/^/      /'; }

# Зависимости и юниты сами себя не обновят — про них только предупреждаем.
echo "$CHANGED" | grep -qx 'requirements.txt' \
  && echo "  ⚠️  requirements.txt изменился:  .venv/bin/pip install -r requirements.txt"
echo "$CHANGED" | grep -qx 'rag/requirements.txt' \
  && echo "  ⚠️  rag/requirements.txt изменился:  rag/.venv/bin/pip install -r rag/requirements.txt"
if echo "$CHANGED" | grep -q '^deploy/ai-dos-.*\.service$'; then
  echo "  ⚠️  юниты в репозитории изменились, а живые копии в /etc/systemd/system"
  echo "      НЕ обновляются автоматически — в них правки под этот сервер. Сверить:"
  echo "        diff deploy/ai-dos-api.service /etc/systemd/system/ai-dos-api.service"
fi

# Перезапускаем только то, чей код действительно поменялся: у RAG долгий старт
# (поднимает индекс), дёргать его из-за правки в static/ — лишний простой.
SVC=""
echo "$CHANGED" | grep -qE '^rag/(ragsvc|scripts)/'   && SVC="$SVC ai-dos-rag"
echo "$CHANGED" | grep -qE '^(app/|run_api\.sh$)'     && SVC="$SVC ai-dos-api"
echo "$CHANGED" | grep -qE '^video_ui/.*\.py$'        && SVC="$SVC ai-dos-video-ui"
# video_ui/static/** намеренно НЕ в списке: uvicorn читает статику с диска на
# каждый запрос, страница обновится сама (проверено 29.07 на vad.js).

if [ -z "$SVC" ]; then
  echo "  перезапуск не нужен (код сервисов не менялся)"
elif [ "$RESTART" != "1" ]; then
  echo "  --no-restart: перезапустить вручную -> systemctl restart$SVC"
else
  echo "  перезапускаю:$SVC"
  systemctl restart $SVC || { echo "  ⚠️  перезапуск не прошёл"; exit 1; }
  sleep 3
  for s in $SVC; do printf "      %-20s %s\n" "$s" "$(systemctl is-active "$s")"; done
fi

printf "  киоск-страница: %s\n" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost/)"
REMOTE
RC=$?

echo
if [ "$RC" != "0" ]; then
  echo "Обновление НЕ прошло (код $RC). Откат: на сервере git reset --hard <прошлый коммит>,"
  echo "он виден выше в строке «сейчас:». Бэкапы снимка — ~/ai-dos-*.tar.gz."
  exit "$RC"
fi
cat <<EOF
Готово. Приёмка:
  ssh $USER@$HOST 'cd $DIR && UI=http://localhost bash scripts/healthcheck.sh'
  ssh $USER@$HOST 'journalctl -u ai-dos-api -n 30 --no-pager'
Откат на прошлый коммит (он напечатан выше как «сейчас:»):
  ssh $USER@$HOST 'cd $DIR && git reset --hard <коммит> && systemctl restart ai-dos-api'
EOF
