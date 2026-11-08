#!/usr/bin/env python3
"""Configure NapCat's OneBot 11 WebSocket server without printing secrets."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
CONFIG_DIR = PROJECT_ROOT / "data" / "napcat" / "config"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main() -> None:
    env = read_env(ENV_PATH)
    token = env.get("NAPCAT_ONEBOT_TOKEN", "")
    if not token or token.startswith("CHANGE_ME"):
        raise SystemExit("NAPCAT_ONEBOT_TOKEN is not configured in .env")

    configs = sorted(CONFIG_DIR.glob("onebot11_*.json"))
    if len(configs) != 1:
        raise SystemExit(
            f"expected exactly one onebot11_*.json after QQ login, found {len(configs)}"
        )

    path = configs[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    network = payload.setdefault("network", {})
    servers = network.setdefault("websocketServers", [])
    if not isinstance(servers, list):
        raise SystemExit("network.websocketServers must be a list")

    servers[:] = [
        server
        for server in servers
        if not (
            isinstance(server, dict)
            and (server.get("name") == "pwcs2" or server.get("port") == 3001)
        )
    ]
    servers.append(
        {
            "enable": True,
            "name": "pwcs2",
            "host": "0.0.0.0",
            "port": 3001,
            "reportSelfMessage": False,
            "enableForcePushEvent": True,
            "messagePostFormat": "array",
            "token": token,
            "debug": False,
            "heartInterval": 30000,
        }
    )

    original_mode = stat.S_IMODE(path.stat().st_mode)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, original_mode)
    os.replace(temporary, path)
    print("NapCat OneBot WebSocket server configured on port 3001.")


if __name__ == "__main__":
    main()
