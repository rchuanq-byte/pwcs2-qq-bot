#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

command -v git >/dev/null || { echo "缺少 git" >&2; exit 1; }
command -v docker >/dev/null || { echo "缺少 docker" >&2; exit 1; }
test -d .git || { echo "当前目录不是 Git 仓库" >&2; exit 1; }
test -f .env || { echo "缺少 .env，请先复制 .env.example 并填写密钥" >&2; exit 1; }
if ! grep -q '^PERFECTWORLD_MY_STEAM_ID=' .env; then
  printf '\nPERFECTWORLD_MY_STEAM_ID=\n' >> .env
fi
if ! grep -q '^STEAM_WEB_API_KEY=' .env; then
  printf 'STEAM_WEB_API_KEY=\n' >> .env
fi
if ! grep -q '^STEAM_PRESENCE_PROXY_URL=' .env; then
  printf 'STEAM_PRESENCE_PROXY_URL=\n' >> .env
fi
if ! grep -q '^COMPOSE_PROFILES=' .env; then
  printf 'COMPOSE_PROFILES=\n' >> .env
fi
if ! grep -q '^HIGHLIGHT_ENABLED=' .env; then
  printf 'HIGHLIGHT_ENABLED=0\n' >> .env
fi
if ! grep -q '^PWCS2_PWA_DEMO_ENABLED=' .env; then
  printf 'PWCS2_PWA_DEMO_ENABLED=0\n' >> .env
fi
if ! grep -q '^PERFECTWORLD_PWA_ACCESS_TOKEN=' .env; then
  printf 'PERFECTWORLD_PWA_ACCESS_TOKEN=\n' >> .env
fi
if ! grep -q '^PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON=' .env; then
  printf 'PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON={}\n' >> .env
fi
if ! grep -q '^PWCS2_HIGHLIGHT_DATA_DIR=' .env; then
  printf 'PWCS2_HIGHLIGHT_DATA_DIR=/data/highlights\n' >> .env
fi
if ! grep -q '^PWCS2_HIGHLIGHT_INTENT_DATABASE_PATH=' .env; then
  printf 'PWCS2_HIGHLIGHT_INTENT_DATABASE_PATH=/data/highlights/intents.db\n' >> .env
fi
if ! grep -q '^PWCS2_HIGHLIGHT_MAX_VIDEO_BYTES=' .env; then
  printf 'PWCS2_HIGHLIGHT_MAX_VIDEO_BYTES=25165824\n' >> .env
fi
if ! grep -q '^PWCS2_HIGHLIGHT_MAX_BACKLOG_DEMOS=' .env; then
  printf 'PWCS2_HIGHLIGHT_MAX_BACKLOG_DEMOS=2\n' >> .env
fi
if ! grep -q '^PWCS2_HIGHLIGHT_MAX_BACKLOG_DEMO_BYTES=' .env; then
  printf 'PWCS2_HIGHLIGHT_MAX_BACKLOG_DEMO_BYTES=2147483648\n' >> .env
fi
if ! grep -q '^HIGHLIGHT_CONTROLLER_MEMORY_LIMIT=' .env; then
  printf 'HIGHLIGHT_CONTROLLER_MEMORY_LIMIT=768m\n' >> .env
fi
if ! grep -q '^HIGHLIGHT_CONTROLLER_MEMORY_SWAP_LIMIT=' .env; then
  printf 'HIGHLIGHT_CONTROLLER_MEMORY_SWAP_LIMIT=1280m\n' >> .env
fi
if ! grep -q '^PWCS2_HIGHLIGHT_IMAGE=' .env; then
  printf 'PWCS2_HIGHLIGHT_IMAGE=pwcs2-qq-bot-highlight:local\n' >> .env
fi
if ! grep -q '^STEAM_MIHOMO_IMAGE=' .env; then
  printf 'STEAM_MIHOMO_IMAGE=pwcs2-steam-mihomo:1.19.29\n' >> .env
elif grep -q '^STEAM_MIHOMO_IMAGE=metacubex/mihomo:v1\.19\.28$' .env; then
  sed -i \
    's|^STEAM_MIHOMO_IMAGE=metacubex/mihomo:v1\.19\.28$|STEAM_MIHOMO_IMAGE=pwcs2-steam-mihomo:1.19.29|' \
    .env
fi
if ! grep -q '^USTC_LLM_API_KEY=' .env; then
  printf '\nUSTC_LLM_API_KEY=\n' >> .env
fi
if ! grep -q '^USTC_LLM_MODEL=' .env; then
  printf 'USTC_LLM_MODEL=deepseek-flash\n' >> .env
fi
if ! grep -q '^USTC_LLM_VISION_MODEL=' .env; then
  printf 'USTC_LLM_VISION_MODEL=deepseek-flash\n' >> .env
fi

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "工作区有未提交修改，拒绝覆盖。请先提交、暂存或删除这些修改。" >&2
  exit 1
fi

BRANCH="${DEPLOY_BRANCH:-$(git branch --show-current)}"
test -n "$BRANCH" || { echo "当前为 detached HEAD；请设置 DEPLOY_BRANCH" >&2; exit 1; }

echo "更新分支: $BRANCH"
OLD_HEAD="$(git rev-parse HEAD)"
git fetch --prune origin "$BRANCH"
git merge --ff-only "origin/$BRANCH"
NEW_HEAD="$(git rev-parse HEAD)"

if [[ "$OLD_HEAD" != "$NEW_HEAD" ]] && git diff --name-only "$OLD_HEAD" "$NEW_HEAD" -- deploy/update.sh | grep -q .; then
  echo "更新脚本已变更，重新执行当前版本..."
  exec "$0"
fi

bash "$ROOT_DIR/deploy/install-training-game.sh"

if [[ "${PULL_NAPCAT_IMAGE:-0}" == "1" ]]; then
  docker compose pull napcat
else
  echo "跳过 NapCat 镜像拉取；如需拉取远端镜像，请使用 PULL_NAPCAT_IMAGE=1"
fi

BUILD_SERVICES=(bot)
HIGHLIGHTS_PROFILE_ENABLED=0
file_compose_profiles="$(
  awk -F= '$1 == "COMPOSE_PROFILES" {sub(/^[^=]*=/, ""); print; exit}' .env
)"
compose_profiles="${COMPOSE_PROFILES:-$file_compose_profiles}"
IFS=', ' read -r -a configured_profiles <<< "$compose_profiles"
for compose_profile in "${configured_profiles[@]}"; do
  if [[ "$compose_profile" == "highlights" || "$compose_profile" == "*" ]]; then
    BUILD_SERVICES+=(highlight-controller)
    HIGHLIGHTS_PROFILE_ENABLED=1
    break
  fi
done
if [[ "${PULL_BOT_BASE_IMAGE:-0}" == "1" ]]; then
  docker compose build --pull "${BUILD_SERVICES[@]}"
else
  docker compose build "${BUILD_SERVICES[@]}"
fi
docker compose up -d --remove-orphans

CONTAINER_ID="$(docker compose ps -q bot)"
for _ in $(seq 1 30); do
  STATUS="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER_ID")"
  if [[ "$STATUS" == "healthy" ]]; then
    file_highlight_enabled="$(
      awk -F= '$1 == "HIGHLIGHT_ENABLED" {sub(/^[^=]*=/, ""); print; exit}' .env
    )"
    highlight_enabled="${HIGHLIGHT_ENABLED:-$file_highlight_enabled}"
    if [[ "$HIGHLIGHTS_PROFILE_ENABLED" == "1" ]]; then
      case "${highlight_enabled,,}" in
        1|true|yes|on)
          if ! timeout --signal=TERM --kill-after=10s 120s \
            bash "$ROOT_DIR/deploy/check-highlights.sh"; then
            docker compose logs --tail=100 highlight-controller highlight-api
            echo "更新后高光服务验收失败" >&2
            exit 1
          fi
          ;;
      esac
    fi
    docker compose ps
    echo "更新完成: $(git rev-parse --short HEAD)"
    exit 0
  fi
  if [[ "$STATUS" == "unhealthy" ]]; then
    break
  fi
  sleep 2
done

docker compose logs --tail=100 bot
echo "更新后健康检查失败" >&2
exit 1
