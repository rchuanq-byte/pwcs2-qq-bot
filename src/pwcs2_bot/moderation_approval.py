"""Human-confirmed management proposals; models cannot grant execution rights."""

from __future__ import annotations

import contextvars
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from pwcs2_bot.deployment_identity import operator_qq

APPROVED_ACTION = contextvars.ContextVar("niuniu_approved_management", default=None)
OWNER = operator_qq()
ACTIONS = {"warn", "mute", "unmute", "kick", "approve_join", "reject_join"}


def canonical(params):
    return {k: v for k, v in params.items() if k != "self_id"}


def approved_native_action(event, action, params):
    grant = APPROVED_ACTION.get()
    routed_self = params.get("self_id")
    event_self = getattr(getattr(event, "message_obj", None), "self_id", None)
    return bool(
        grant
        and OWNER
        and event is not None
        and event.get_extra("niuniu_admin_confirmation")
        and str(event.get_sender_id()) == OWNER
        and str(event.get_group_id()) == grant[0]
        and (routed_self is None or str(routed_self) == str(event_self))
        and action == grant[1]
        and canonical(params) == grant[2]
    )


@contextmanager
def approved_action(group, action, params):
    token = APPROVED_ACTION.set((str(group), action, canonical(params)))
    try:
        yield
    finally:
        APPROVED_ACTION.reset(token)


class ApprovalError(ValueError):
    pass


class ModerationApprovals:
    def __init__(self, path, groups, *, clock=time.time):
        self.path, self.groups, self.clock = Path(path), {str(g) for g in groups}, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS proposals (
              id TEXT PRIMARY KEY, group_id TEXT NOT NULL, requester TEXT NOT NULL,
              target TEXT NOT NULL, action TEXT NOT NULL, params TEXT NOT NULL,
              reason TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending')""")
        self.path.chmod(0o600)

    def connect(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def propose(self, group, requester, target, action, *, duration=0, reason="", flag=""):
        group, target = str(group), str(target)
        if not OWNER or group not in self.groups or action not in ACTIONS or not target.isdecimal():
            raise ApprovalError("本群或该管理操作未开放")
        if len(target) > 20 or target == OWNER:
            raise ApprovalError("不能针对维护者发起管理操作")
        if action == "warn":
            api, params = (
                "send_group_msg",
                {
                    "group_id": int(group),
                    "message": [
                        {"type": "at", "data": {"qq": target}},
                        {
                            "type": "text",
                            "data": {"text": "请注意群规：" + str(reason).replace("\n", " ")[:100]},
                        },
                    ],
                },
            )
        elif action == "mute":
            if not 1 <= int(duration) <= 600:
                raise ApprovalError("禁言时长必须为1至600秒")
            api, params = (
                "set_group_ban",
                {"group_id": int(group), "user_id": int(target), "duration": int(duration)},
            )
        elif action == "unmute":
            api, params = (
                "set_group_ban",
                {"group_id": int(group), "user_id": int(target), "duration": 0},
            )
        elif action == "kick":
            api, params = (
                "set_group_kick",
                {"group_id": int(group), "user_id": int(target), "reject_add_request": False},
            )
        else:
            if not flag or len(flag) > 1024:
                raise ApprovalError("缺少有效的入群申请凭据")
            api, params = (
                "set_group_add_request",
                {"flag": flag, "approve": action == "approve_join"},
            )
            if action == "reject_join":
                params["reason"] = "群管理未通过本次申请"
        payload = json.dumps({"api": api, "params": params}, sort_keys=True, ensure_ascii=False)
        now = self.clock()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            same = c.execute(
                "SELECT * FROM proposals WHERE group_id=? AND target=? AND action=? "
                "AND params=? AND state='pending' AND expires>?",
                (group, target, action, payload, now),
            ).fetchone()
            if same:
                return self.public(same)
            count = c.execute(
                "SELECT count(*) FROM proposals WHERE group_id=? AND created>?", (group, now - 3600)
            ).fetchone()[0]
            if count >= 6:
                raise ApprovalError("本群本小时管理提议已达上限")
            key = secrets.token_hex(5)
            c.execute(
                "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,'pending')",
                (
                    key,
                    group,
                    str(requester),
                    target,
                    action,
                    payload,
                    str(reason)[:200],
                    now,
                    now + 300,
                ),
            )
            row = c.execute("SELECT * FROM proposals WHERE id=?", (key,)).fetchone()
            c.execute("DELETE FROM proposals WHERE created<?", (now - 30 * 86400,))
            return self.public(row)

    @staticmethod
    def public(row):
        return {
            **{
                k: row[k]
                for k in ("id", "group_id", "target", "action", "reason", "expires", "state")
            },
            "duration_seconds": json.loads(row["params"])["params"].get("duration", 0),
        }

    def pending(self, group, actor):
        self.authorize(group, actor)
        with self.connect() as c:
            return [
                self.public(r)
                for r in c.execute(
                    "SELECT * FROM proposals WHERE group_id=? AND state='pending' "
                    "AND expires>? ORDER BY created LIMIT 6",
                    (str(group), self.clock()),
                )
            ]

    def authorize(self, group, actor):
        if not OWNER or str(group) not in self.groups or str(actor) != OWNER:
            raise ApprovalError("仅维护者可在原群确认管理操作")

    def claim(self, key, group, actor, *, cancel=False):
        self.authorize(group, actor)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM proposals WHERE id=? AND group_id=?", (key, str(group))
            ).fetchone()
            if row is None or row["state"] != "pending" or row["expires"] <= self.clock():
                raise ApprovalError("提议不存在、已处理或已过期，请重新发起")
            c.execute(
                "UPDATE proposals SET state=? WHERE id=?",
                ("cancelled" if cancel else "executing", key),
            )
            return {**self.public(row), **json.loads(row["params"])}

    def finish(self, key, state):
        if state not in {"done", "failed", "unknown"}:
            raise ValueError("Invalid approval state")
        with self.connect() as c:
            c.execute("UPDATE proposals SET state=? WHERE id=? AND state='executing'", (state, key))

    async def execute(self, key, group, actor, event, call, self_id):
        if str(event.get_sender_id()) != str(actor) or str(event.get_group_id()) != str(group):
            raise ApprovalError("确认消息的群或发送者不匹配")
        proposal = self.claim(key, group, actor)
        try:
            own = (
                await call(
                    "get_group_member_info",
                    {"group_id": int(group), "user_id": int(self_id), "no_cache": True},
                )
            )["data"]
            if own.get("role") not in {"owner", "admin"}:
                raise ApprovalError("机器人当前不是群管理员，未执行；授予权限后请重新发起")
            if proposal["action"] not in {"approve_join", "reject_join"}:
                info = (
                    await call(
                        "get_group_member_info",
                        {
                            "group_id": int(group),
                            "user_id": int(proposal["target"]),
                            "no_cache": True,
                        },
                    )
                )["data"]
                if str(proposal["target"]) == str(self_id) or info.get("role") != "member":
                    raise ApprovalError("目标身份不允许执行此管理操作")
            event.set_extra("niuniu_admin_confirmation", True)
            with approved_action(group, proposal["api"], proposal["params"]):
                await call(proposal["api"], proposal["params"])
        except TimeoutError:
            self.finish(key, "unknown")
            return "平台回执超时，结果未确认。为避免重复操作，不会自动重试，请手动查看。"
        except Exception as exc:
            self.finish(key, "failed")
            if isinstance(exc, ApprovalError):
                return str(exc)
            return "平台未能完成这次管理操作，未自动重试。"
        else:
            self.finish(key, "done")
            return "已按你的确认执行本次管理操作。"
