#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ $# -ne 1 || "$1" != *"#"* ]]; then
  echo "用法：$0 '昵称#数字ID'" >&2
  exit 2
fi
if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先运行 docker compose up -d bot" >&2
  exit 1
fi

docker compose exec -T -e VALORANT_TEST_ID="$1" bot python - <<'PY'
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pwcs2_bot.stats_card import StatsCardRenderer
from pwcs2_bot.valorant import ValorantClient


async def main() -> int:
    riot_id = os.environ["VALORANT_TEST_ID"]
    client = ValorantClient(timeout_seconds=30)
    renderer = StatsCardRenderer(timeout_seconds=60)
    image_path: Path | None = None
    try:
        snapshot = await client.query(riot_id)
        image_path = await renderer.render_valorant(snapshot)
        if not image_path.is_file() or image_path.stat().st_size < 1024:
            raise RuntimeError("rendered image is empty")
        print(
            "OK VALORANT public stats queried and card rendered "
            f"player={snapshot.riot_id} matches={len(snapshot.matches)}"
        )
        return 0
    finally:
        if image_path is not None:
            image_path.unlink(missing_ok=True)
        await renderer.close()
        await client.close()


raise SystemExit(asyncio.run(main()))
PY
