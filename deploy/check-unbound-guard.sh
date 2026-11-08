#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先启动 bot" >&2
  exit 1
fi

docker compose exec -T bot python - "${1:-}" <<'PY'
from __future__ import annotations

import json
import os
import sys
import uuid

from websockets.sync.client import connect

from pwcs2_bot.database import BindingRepository


def call(ws, action: str, params: dict) -> dict:
    echo = uuid.uuid4().hex
    ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
    for _ in range(50):
        payload = json.loads(ws.recv(timeout=10))
        if not isinstance(payload, dict) or payload.get("echo") != echo:
            continue
        if payload.get("status") == "ok" or payload.get("retcode") == 0:
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        raise RuntimeError(
            f"OneBot action failed: action={action} retcode={payload.get('retcode')}"
        )
    raise RuntimeError(f"OneBot action timed out: {action}")


def main() -> None:
    repository = BindingRepository(os.environ.get("PWCS2_DATABASE_PATH", "/data/bindings.db"))
    bindings = repository.list_bindings()
    requested_group = sys.argv[1].strip()
    if not requested_group and not bindings:
        raise RuntimeError("请提供群号，数据库中也没有可推断群号的绑定")
    group_id = requested_group or bindings[0].group_id
    token = os.environ.get("NAPCAT_ONEBOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("NAPCAT_ONEBOT_TOKEN 未配置")
    with connect(
        "ws://napcat:3001",
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=10,
        close_timeout=3,
    ) as ws:
        login = call(ws, "get_login_info", {})
        bot_id = int(login["user_id"])
        member = call(
            ws,
            "get_group_member_info",
            {"group_id": int(group_id), "user_id": bot_id, "no_cache": True},
        )
    role = str(member.get("role") or "member")
    if role not in {"admin", "owner"}:
        raise RuntimeError(
            f"机器人在群 {group_id} 的角色是 {role}，必须设为管理员后才能自动禁言"
        )
    print("OK bot has permission to mute unbound group members")
    print(f"group={group_id}")
    print(f"role={role}")


main()
PY
