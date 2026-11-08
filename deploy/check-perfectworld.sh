#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ $# -ne 1 ]]; then
  echo "用法: $0 <SteamID64>" >&2
  echo "示例: $0 76561198929215155" >&2
  exit 2
fi

TARGET_STEAM_ID="$1"
if [[ ! "$TARGET_STEAM_ID" =~ ^7656119[0-9]{10}$ ]]; then
  echo "SteamID64 格式不正确，应类似 7656119xxxxxxxxxx" >&2
  exit 2
fi

test -f .env || { echo "缺少 .env" >&2; exit 1; }

if ! grep -Eq '^PERFECTWORLD_TOKEN=.+$' .env || grep -Eq '^PERFECTWORLD_TOKEN=CHANGE_ME' .env; then
  echo "PERFECTWORLD_TOKEN 未配置。请先编辑 .env，但不要把 token 打印或提交到 Git。" >&2
  exit 1
fi

if ! docker compose ps -q bot >/dev/null; then
  echo "bot 容器不存在，请先运行 docker compose up -d bot" >&2
  exit 1
fi

docker compose exec -T bot python - "$TARGET_STEAM_ID" <<'PY'
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from pwcs2_bot.perfectworld import PerfectWorldApiError, PerfectWorldClient


def value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        item = data.get(key)
        if item not in (None, ""):
            return item
    return "-"


async def main() -> int:
    target_steam_id = sys.argv[1]
    token = os.environ.get("PERFECTWORLD_TOKEN", "").strip()
    my_steam_id = os.environ.get("PERFECTWORLD_MY_STEAM_ID", "").strip()

    if not token or token.startswith("CHANGE_ME"):
        print("FAIL PERFECTWORLD_TOKEN 未配置")
        return 1

    client = PerfectWorldClient(token, my_steam_id=my_steam_id)
    try:
        data = await client.detail_stats(target_steam_id)
    except PerfectWorldApiError as exc:
        print(f"FAIL {exc}")
        return 1
    finally:
        await client.close()

    print("OK PerfectWorld detailStats 查询成功")
    print(f"name={value(data, 'name')}")
    print(f"steamId={value(data, 'steamId')}")
    print(f"seasonId={value(data, 'seasonId')}")
    print(f"cnt={value(data, 'cnt')}")
    print(f"winRate={value(data, 'winRate')}")
    print(f"rating={value(data, 'pwRating', 'rating')}")
    return 0


raise SystemExit(asyncio.run(main()))
PY
