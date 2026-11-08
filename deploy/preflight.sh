#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

command -v git >/dev/null || { echo "缺少 git" >&2; exit 1; }
command -v docker >/dev/null || { echo "缺少 docker" >&2; exit 1; }
docker compose version >/dev/null

test -f .env || { echo "缺少 .env：请复制 .env.example 后填写" >&2; exit 1; }
test -f config/config.json || { echo "缺少 config/config.json" >&2; exit 1; }
if ! grep -q '^PERFECTWORLD_MY_STEAM_ID=' .env; then
  printf '\nPERFECTWORLD_MY_STEAM_ID=\n' >> .env
fi

if grep -q "CHANGE_ME" .env; then
  echo ".env 仍包含 CHANGE_ME，占位密钥不能用于部署" >&2
  exit 1
fi
if ! grep -Eq '^PERFECTWORLD_TOKEN=.+$' .env; then
  echo ".env 缺少 PERFECTWORLD_TOKEN" >&2
  exit 1
fi
if ! grep -Eq '^NAPCAT_ONEBOT_TOKEN=.+$' .env; then
  echo ".env 缺少 NAPCAT_ONEBOT_TOKEN" >&2
  exit 1
fi

mkdir -p data/bot data/napcat/qq data/napcat/config data/napcat/plugins

echo "[1/3] 校验 Compose 合并配置"
docker compose config --quiet

echo "[2/3] 构建机器人镜像"
docker compose build bot

echo "[3/3] 验证 AstrBot 业务适配器"
docker compose run --rm --no-deps --entrypoint python bot -c \
  "from pwcs2_bot.astrbot_transport import AstrBotCSApplication; import sys; assert not any(m.startswith('nanobot') for m in sys.modules); print('AstrBot CS adapter: ok')"

if grep -Eq '"allowedGroups"[[:space:]]*:[[:space:]]*\[[[:space:]]*\]' config/config.json; then
  echo "警告：allowedGroups 为空，机器人命令将对所在的所有 QQ 群开放。"
fi

echo "预检通过。下一步：docker compose up -d napcat"
