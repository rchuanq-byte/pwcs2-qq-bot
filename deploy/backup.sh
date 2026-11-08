#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p -m 700 backups
chmod 700 backups
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="backups/pwcs2-bot-$STAMP.tar.gz"

restart_bot() {
  docker compose start bot >/dev/null
}

trap restart_bot EXIT
docker compose stop bot
if command -v sudo >/dev/null && sudo -n true 2>/dev/null; then
  sudo -n tar -czf "$ARCHIVE" data/bot data/napcat/config .env config/config.json
  sudo -n chown "$(id -u):$(id -g)" "$ARCHIVE"
else
  tar -czf "$ARCHIVE" data/bot data/napcat/config .env config/config.json
fi
chmod 600 "$ARCHIVE"
tar -tzf "$ARCHIVE" >/dev/null

restart_bot
CONTAINER_ID="$(docker compose ps -q bot)"
STATUS=starting
for _ in $(seq 1 30); do
  STATUS="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER_ID")"
  [[ "$STATUS" == "healthy" ]] && break
  sleep 2
done
[[ "$STATUS" == "healthy" ]]
trap - EXIT
echo "备份完成: $ARCHIVE"
