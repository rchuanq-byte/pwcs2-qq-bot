#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

docker compose exec -T bot python - <<'PY'
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pwcs2_bot.stream_live import LivePlatformClient, StreamLiveError, StreamerTarget


async def main() -> int:
    root = json.loads(Path("/config/config.json").read_text(encoding="utf-8"))
    channel = root.get("channels", {}).get("pwcs2", {})
    targets = [
        StreamerTarget.model_validate(item)
        for item in channel.get("streamLiveTargets", [])
        if isinstance(item, dict)
    ]
    client = LivePlatformClient(
        timeout_seconds=float(channel.get("streamLiveTimeoutSeconds", 10))
    )
    failures = 0
    try:
        for target in targets:
            if not target.enabled:
                continue
            try:
                snapshot = await client.fetch(target)
            except StreamLiveError as exc:
                failures += 1
                print(f"FAIL {target.platform} {target.name}: {exc}")
                continue
            status = "LIVE" if snapshot.is_live else "OFFLINE"
            print(
                f"OK {target.platform} {target.name}: {status} "
                f"room={snapshot.room_id} title={snapshot.title[:60]}"
            )
    finally:
        await client.close()
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))
PY
