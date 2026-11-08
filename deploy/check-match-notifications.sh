#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ $# -ne 1 ]] || [[ ! "$1" =~ ^7656119[0-9]{10}$ ]]; then
  echo "用法: $0 <SteamID64>" >&2
  exit 2
fi

test -f .env || { echo "缺少 .env" >&2; exit 1; }
if ! grep -Eq '^PERFECTWORLD_TOKEN=.+$' .env; then
  echo "PERFECTWORLD_TOKEN 未配置" >&2
  exit 1
fi
if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先运行 docker compose up -d bot" >&2
  exit 1
fi

docker compose exec -T bot python - "$1" <<'PY'
from __future__ import annotations

import asyncio
import os
import struct
import sys

from pwcs2_bot.match_formatting import build_match_snapshot
from pwcs2_bot.perfectworld import PerfectWorldClient
from pwcs2_bot.stats_card import StatsCardRenderer


async def main() -> None:
    steam_id = sys.argv[1]
    client = PerfectWorldClient(
        os.environ.get("PERFECTWORLD_TOKEN", ""),
        my_steam_id=os.environ.get("PERFECTWORLD_MY_STEAM_ID", ""),
    )
    renderer = StatsCardRenderer(timeout_seconds=30)
    path = None
    try:
        matches = await client.match_list(steam_id, page_size=1)
        if not matches:
            raise RuntimeError("该账号没有可用于验收的比赛记录")
        profile = await client.detail_stats(steam_id)
        detail = await client.match_detail(
            str(matches[0]["matchId"]),
            data_source=int(matches[0].get("dataSource") or 3),
        )
        snapshot = build_match_snapshot(
            matches[0],
            steam_id=steam_id,
            profile=profile,
            detail=detail,
            group_member_steam_ids={steam_id},
        )
        assert len(snapshot.players) == 10
        path = await renderer.render_match(snapshot)
        payload = path.read_bytes()
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        assert payload[12:16] == b"IHDR"
        width, height = struct.unpack(">II", payload[16:24])
        assert width >= 960 and height >= 500
        print("OK 10-player match detail queried and notification card rendered")
        print(f"matchId={snapshot.match_id}")
        print(f"map={snapshot.map_name}")
        print(f"result={snapshot.result_label} {snapshot.own_score}:{snapshot.opponent_score}")
        print(f"players={len(snapshot.players)}")
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
        await renderer.close()
        await client.close()


asyncio.run(main())
PY
