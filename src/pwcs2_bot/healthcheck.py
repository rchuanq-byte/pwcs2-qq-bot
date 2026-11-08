"""Docker readiness: gateway responsiveness AND recently verified QQ online."""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def snapshot_problem(data: Any, *, now: float, max_age: float = 95) -> str | None:
    if not isinstance(data, dict) or data.get("version") != 1:
        return "invalid_snapshot"
    checked = data.get("checked_at")
    if (
        isinstance(checked, bool)
        or not isinstance(checked, int | float)
        or not math.isfinite(checked)
        or checked > now + 5
        or now - checked > max_age
    ):
        return "stale_snapshot"
    if (
        data.get("state") != "online"
        or data.get("online") is not True
        or data.get("connected") is not True
    ):
        return "qq_not_ready"
    return None


def read_problem(path: Path, *, now: float | None = None) -> str | None:
    try:
        # The file is small. Bound the read even if it was replaced accidentally.
        with path.open("rb") as stream:
            content = stream.read(8193)
        if len(content) > 8192:
            return "invalid_snapshot"
        data = json.loads(content)
    except (OSError, ValueError):
        return "missing_or_invalid_snapshot"
    return snapshot_problem(data, now=time.time() if now is None else now)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("/data/runtime/napcat-health.json"))
    args = parser.parse_args()
    try:
        # Explicitly avoid proxy environment variables for this loopback probe.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:18790/health", timeout=3) as response:
            if response.status != 200:
                raise OSError("gateway unavailable")
    except (OSError, urllib.error.URLError):
        print("FAIL gateway_unavailable")
        return 1
    problem = read_problem(args.state)
    if problem:
        print("FAIL " + problem)
        return 1
    print("OK gateway_and_qq_online")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
