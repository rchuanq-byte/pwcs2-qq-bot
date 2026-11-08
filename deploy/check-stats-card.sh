#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先运行 docker compose up -d bot" >&2
  exit 1
fi

docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import asyncio
import struct

from pwcs2_bot.formatting import build_stats_snapshot
from pwcs2_bot.stats_card import StatsCardRenderer


async def main() -> None:
    stats = build_stats_snapshot(
        {
            "name": "战绩卡片自检",
            "steamId": "76561197960266101",
            "seasonId": "S24",
            "pvpScore": 1950,
            "pwRating": 1.07,
            "cnt": 21,
            "winRate": 0.48,
            "kills": 315,
            "assists": 89,
            "deaths": 282,
            "adr": 77.29,
            "headShotRatio": 0.5,
            "rws": 9.29,
            "mvpCount": 44,
            "summary": "神枪不朽",
            "hotMaps": [
                {"mapName": "炙热沙城Ⅱ", "totalMatch": 13, "winCount": 8},
                {"mapName": "荒漠迷城", "totalMatch": 8, "winCount": 2},
            ],
            "hotWeapons": [
                {"weaponName": "AK47", "weaponKill": 138},
                {"weaponName": "M4A1消音型", "weaponKill": 59},
                {"weaponName": "AWP", "weaponKill": 40},
            ],
        }
    )
    renderer = StatsCardRenderer(timeout_seconds=30)
    path = None
    try:
        path = await renderer.render(stats)
        payload = path.read_bytes()
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        assert payload[12:16] == b"IHDR"
        width, height = struct.unpack(">II", payload[16:24])
        assert width >= 960
        assert height >= 600
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
        await renderer.close()
    print("OK HTML/CSS stats card rendered to PNG")


asyncio.run(main())
PY
