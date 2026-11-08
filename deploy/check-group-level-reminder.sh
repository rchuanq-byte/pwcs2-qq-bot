#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "$(docker compose ps -q bot)" ]]; then
  echo "bot 容器不存在，请先启动 bot" >&2
  exit 1
fi

docker compose exec -T bot python - "${1:-}" "${2:-1}" <<'PY'
from __future__ import annotations

import json
import os
import sys
import uuid

from websockets.sync.client import connect

from pwcs2_bot.database import BindingRepository


def call(ws, action: str, params: dict):
    echo = uuid.uuid4().hex
    ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
    for _ in range(50):
        payload = json.loads(ws.recv(timeout=10))
        if not isinstance(payload, dict) or payload.get("echo") != echo:
            continue
        if payload.get("status") == "ok" or payload.get("retcode") == 0:
            return payload.get("data")
        raise RuntimeError(
            f"OneBot action failed: action={action} retcode={payload.get('retcode')}"
        )
    raise RuntimeError(f"OneBot action timed out: {action}")


repository = BindingRepository(os.environ.get("PWCS2_DATABASE_PATH", "/data/bindings.db"))
bindings = repository.list_bindings()
requested_group = sys.argv[1].strip()
if not requested_group and not bindings:
    raise RuntimeError("请提供群号，数据库中也没有可推断的群号")
group_id = requested_group or bindings[0].group_id
target_level = int(sys.argv[2])
token = os.environ.get("NAPCAT_ONEBOT_TOKEN", "").strip()
if not token:
    raise RuntimeError("NAPCAT_ONEBOT_TOKEN 未配置")

with connect(
    "ws://napcat:3001",
    additional_headers={"Authorization": f"Bearer {token}"},
    open_timeout=10,
    close_timeout=3,
) as ws:
    members = call(
        ws,
        "get_group_member_list",
        {"group_id": int(group_id), "no_cache": True},
    )

if not isinstance(members, list) or not members:
    raise RuntimeError("NapCat 返回的群成员列表为空或格式错误")


def is_truthy(value):
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return bool(value)


ordinary_members = [
    member
    for member in members
    if isinstance(member, dict)
    and str(member.get("role") or "member").strip().casefold() == "member"
    and not is_truthy(member.get("is_robot"))
    and str(member.get("user_id") or "").isdecimal()
]
bound_ids = {
    binding.qq_id for binding in repository.list_group_bindings(str(group_id))
}
level_ids = {
    str(member["user_id"])
    for member in ordinary_members
    if str(member.get("level", "")).strip() == str(target_level)
}
unbound_ids = {
    str(member["user_id"])
    for member in ordinary_members
    if str(member["user_id"]) not in bound_ids
}
print("OK group compliance data is available (no message sent or member removed)")
print(f"group={group_id}")
print(f"member_count={len(members)}")
print(f"target_level={target_level}")
print(f"level_target_count={len(level_ids)}")
print(f"unbound_target_count={len(unbound_ids)}")
print(f"policy_target_count={len(level_ids | unbound_ids)}")
PY
