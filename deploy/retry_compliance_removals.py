#!/usr/bin/env python3
"""Safely preview or retry overdue group-compliance removals through OneBot."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from websockets.sync.client import connect

from pwcs2_bot.database import BindingRepository

TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_DATABASE = "/data/bindings.db"
DEFAULT_WS_URL = "ws://napcat:3001"
EXEMPT_ROLES = {"owner", "admin"}


class RetryError(RuntimeError):
    """A safe-to-display retry failure."""


@dataclass(frozen=True, slots=True)
class DueRecord:
    qq_id: str
    first_noncompliant_date: date
    last_checked_date: date
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KickCandidate:
    qq_id: str
    reasons: tuple[str, ...]
    level: int
    is_bound: bool


@dataclass(frozen=True, slots=True)
class ReviewResult:
    candidates: tuple[KickCandidate, ...]
    clearable_ids: tuple[str, ...]
    skipped_ids: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="预览或重试已到期但尚未完成的群合规移出",
    )
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--grace-days", type=int, default=15)
    parser.add_argument("--target-level", type=int, default=1)
    parser.add_argument("--max-removals", type=int, default=10)
    return parser.parse_args()


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return bool(value)


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise RetryError(f"数据库中的 {field} 日期格式不正确") from exc


def load_due_records(
    database_path: Path,
    group_id: str,
    *,
    today: date,
    grace_days: int,
) -> tuple[DueRecord, ...]:
    cutoff = today - timedelta(days=grace_days)
    connection = sqlite3.connect(database_path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT qq_id, first_noncompliant_date, last_checked_date, reasons_json
            FROM group_member_compliance
            WHERE group_id = ?
            ORDER BY CAST(qq_id AS INTEGER)
            """,
            (group_id,),
        ).fetchall()
    finally:
        connection.close()

    due: list[DueRecord] = []
    for row in rows:
        first_date = _parse_date(row["first_noncompliant_date"], "首次不合规")
        if first_date > cutoff:
            continue
        try:
            parsed_reasons = json.loads(str(row["reasons_json"]))
        except json.JSONDecodeError as exc:
            raise RetryError("数据库中的不合规原因格式不正确") from exc
        if not isinstance(parsed_reasons, list):
            raise RetryError("数据库中的不合规原因格式不正确")
        reasons = tuple(
            sorted(
                {
                    str(reason)
                    for reason in parsed_reasons
                    if str(reason) in {"level", "unbound"}
                }
            )
        )
        due.append(
            DueRecord(
                qq_id=str(row["qq_id"]),
                first_noncompliant_date=first_date,
                last_checked_date=_parse_date(row["last_checked_date"], "最后检查"),
                reasons=reasons,
            )
        )
    return tuple(due)


def review_due_records(
    records: tuple[DueRecord, ...],
    members: list[dict[str, Any]],
    *,
    bound_ids: set[str],
    bot_id: str,
    today: date,
    target_level: int,
    include_unbound: bool = True,
) -> ReviewResult:
    live_members = {
        str(member.get("user_id")): member
        for member in members
        if isinstance(member, dict)
        and str(member.get("user_id") or "").isdecimal()
    }
    candidates: list[KickCandidate] = []
    clearable: list[str] = []
    skipped: list[str] = []

    for record in records:
        check_age_days = (today - record.last_checked_date).days
        if check_age_days < 0 or check_age_days > 1:
            skipped.append(record.qq_id)
            continue
        member = live_members.get(record.qq_id)
        if member is None:
            clearable.append(record.qq_id)
            continue
        role = str(member.get("role") or "").strip().casefold()
        if record.qq_id == bot_id or role in EXEMPT_ROLES:
            clearable.append(record.qq_id)
            continue
        if role != "member" or "is_robot" not in member:
            skipped.append(record.qq_id)
            continue
        if _truthy(member["is_robot"]):
            clearable.append(record.qq_id)
            continue
        if "level" not in member:
            skipped.append(record.qq_id)
            continue
        try:
            level = int(str(member["level"]).strip())
        except ValueError:
            skipped.append(record.qq_id)
            continue
        is_bound = record.qq_id in bound_ids
        still_low_level = level == target_level
        still_unbound = include_unbound and not is_bound
        if not still_low_level and not still_unbound:
            clearable.append(record.qq_id)
            continue
        candidates.append(
            KickCandidate(
                qq_id=record.qq_id,
                reasons=record.reasons,
                level=level,
                is_bound=is_bound,
            )
        )

    return ReviewResult(
        candidates=tuple(candidates),
        clearable_ids=tuple(clearable),
        skipped_ids=tuple(skipped),
    )


def onebot_call(ws: Any, action: str, params: dict[str, Any]) -> Any:
    echo = uuid.uuid4().hex
    ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
    for _ in range(100):
        payload = json.loads(ws.recv(timeout=10))
        if not isinstance(payload, dict) or payload.get("echo") != echo:
            continue
        if payload.get("status") == "ok" or payload.get("retcode") == 0:
            return payload.get("data")
        raise RetryError(
            f"OneBot 操作失败：action={action} retcode={payload.get('retcode')}"
        )
    raise RetryError(f"OneBot 操作超时：{action}")


def fetch_members(ws: Any, group_id: str) -> list[dict[str, Any]]:
    data = onebot_call(
        ws,
        "get_group_member_list",
        {"group_id": int(group_id), "no_cache": True},
    )
    if not isinstance(data, list) or not data:
        raise RetryError("NapCat 返回的实时群成员列表为空或格式错误")
    return [member for member in data if isinstance(member, dict)]


def fetch_member_detail(
    ws: Any,
    group_id: str,
    qq_id: str,
) -> dict[str, Any]:
    data = onebot_call(
        ws,
        "get_group_member_info",
        {
            "group_id": int(group_id),
            "user_id": int(qq_id),
            "no_cache": True,
        },
    )
    if not isinstance(data, dict) or str(data.get("user_id") or "") != qq_id:
        raise RetryError(f"无法核验成员 {qq_id} 的实时身份")
    return data


def verify_bot_role(ws: Any, group_id: str, bot_id: str) -> None:
    data = onebot_call(
        ws,
        "get_group_member_info",
        {
            "group_id": int(group_id),
            "user_id": int(bot_id),
            "no_cache": True,
        },
    )
    if not isinstance(data, dict) or str(data.get("user_id") or "") != bot_id:
        raise RetryError("无法核验机器人在目标群中的身份")
    role = str(data.get("role") or "").strip().casefold()
    if role not in EXEMPT_ROLES:
        raise RetryError(f"机器人当前角色为 {role or 'unknown'}，拒绝执行移出")


def print_review(
    group_id: str,
    records: tuple[DueRecord, ...],
    review: ReviewResult,
    *,
    execute: bool,
) -> None:
    print("OK 群合规到期记录已完成实时复核")
    print(f"group={group_id}")
    print(f"mode={'execute' if execute else 'preview'}")
    print(f"due_records={len(records)}")
    print(f"kick_candidates={len(review.candidates)}")
    print(f"already_resolved={len(review.clearable_ids)}")
    print(f"safety_skipped={len(review.skipped_ids)}")
    for item in review.candidates:
        reasons = ",".join(item.reasons) or "unknown"
        print(
            f"candidate qq={item.qq_id} reasons={reasons} "
            f"level={item.level} bound={str(item.is_bound).lower()}"
        )
    if review.skipped_ids:
        print(f"skipped_qq={','.join(review.skipped_ids)}")


def main() -> int:
    args = parse_args()
    group_id = str(args.group_id).strip()
    if not group_id.isdecimal():
        raise RetryError("群号格式不正确")
    if not 1 <= args.grace_days <= 90:
        raise RetryError("grace-days 必须在 1 到 90 之间")
    if not 1 <= args.max_removals <= 50:
        raise RetryError("max-removals 必须在 1 到 50 之间")

    token = os.environ.get("NAPCAT_ONEBOT_TOKEN", "").strip()
    if not token:
        raise RetryError("NAPCAT_ONEBOT_TOKEN 未配置")
    database_path = Path(
        os.environ.get("PWCS2_DATABASE_PATH", DEFAULT_DATABASE)
    ).resolve()
    repository = BindingRepository(database_path)
    today = datetime.now(TIMEZONE).date()
    records = load_due_records(
        database_path,
        group_id,
        today=today,
        grace_days=args.grace_days,
    )

    with connect(
        DEFAULT_WS_URL,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=10,
        close_timeout=3,
    ) as ws:
        login = onebot_call(ws, "get_login_info", {})
        if not isinstance(login, dict):
            raise RetryError("NapCat 返回的机器人登录信息无效")
        bot_id = str(login.get("user_id") or "")
        if not bot_id.isdecimal():
            raise RetryError("无法取得机器人 QQ 号")
        verify_bot_role(ws, group_id, bot_id)
        member_list = fetch_members(ws, group_id)
        listed_ids = {
            str(member.get("user_id"))
            for member in member_list
            if str(member.get("user_id") or "").isdecimal()
        }
        members: list[dict[str, Any]] = []
        for record in records:
            if record.qq_id not in listed_ids:
                continue
            try:
                members.append(fetch_member_detail(ws, group_id, record.qq_id))
            except RetryError:
                # Keep the ID present but force fail-closed review when authority
                # data cannot be fetched for this destructive decision.
                members.append({"user_id": record.qq_id, "role": "unknown"})
        bound_ids = {
            binding.qq_id
            for binding in repository.list_group_bindings(group_id)
        }
        review = review_due_records(
            records,
            members,
            bound_ids=bound_ids,
            bot_id=bot_id,
            today=today,
            target_level=args.target_level,
        )
        print_review(group_id, records, review, execute=args.execute)
        if not args.execute:
            return 0

        for qq_id in review.clearable_ids:
            repository.clear_group_member_compliance(group_id, qq_id)

        attempted = review.candidates[: args.max_removals]
        action_failures: list[str] = []
        for candidate in attempted:
            try:
                onebot_call(
                    ws,
                    "set_group_kick",
                    {
                        "group_id": int(group_id),
                        "user_id": int(candidate.qq_id),
                        "reject_add_request": False,
                    },
                )
            except RetryError:
                action_failures.append(candidate.qq_id)

        if attempted:
            time.sleep(1)
        remaining_ids = {
            str(member.get("user_id"))
            for member in fetch_members(ws, group_id)
            if str(member.get("user_id") or "").isdecimal()
        }

    removed_ids = tuple(
        item.qq_id for item in attempted if item.qq_id not in remaining_ids
    )
    failed_ids = tuple(
        item.qq_id for item in attempted if item.qq_id in remaining_ids
    )
    for qq_id in removed_ids:
        repository.clear_group_member_compliance(group_id, qq_id)

    print(f"removed={len(removed_ids)}")
    print(f"still_in_group={len(failed_ids)}")
    print(f"deferred={max(0, len(review.candidates) - len(attempted))}")
    if removed_ids:
        print(f"removed_qq={','.join(removed_ids)}")
    if failed_ids:
        print(f"failed_qq={','.join(failed_ids)}")
    if action_failures:
        print(f"onebot_failed_qq={','.join(action_failures)}")
    return 1 if failed_ids else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RetryError, sqlite3.Error) as exc:
        print(f"FAIL {exc}")
        raise SystemExit(1) from None
