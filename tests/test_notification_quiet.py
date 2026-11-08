import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from nanobot.bus.queue import MessageBus
from nanobot.channels.napcat import NapcatChannel
from pydantic import ValidationError

from pwcs2_bot.channel import PWCS2Channel, PWCS2Config
from pwcs2_bot.match_formatting import build_match_snapshot


def make_channel(tmp_path, **settings):
    return PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "allowedGroups": ["1"],
            "databasePath": str(tmp_path / "bindings.db"),
            "pwToken": "test",
            "aiApiKey": "test",
            "aiCompanionEnabled": True,
            "aiCompanionGroups": ["1"],
            "aiCompanionProactiveEnabled": True,
            "unboundGuardEnabled": False,
            "trainingGameEnabled": False,
            "automaticQuietUntil": (datetime.now(UTC) + timedelta(hours=24)).isoformat(),
            **settings,
        },
        MessageBus(),
    )


def event(number, text, user=10, **extra):
    return {
        "message_type": "group",
        "group_id": 1,
        "user_id": user,
        "message_id": number,
        "sender": {"nickname": "test", "role": "member"},
        "message": [{"type": "text", "data": {"text": text}}],
        **extra,
    }


async def test_quiet_suppresses_background_but_preserves_explicit_replies(tmp_path, monkeypatch):
    channel = make_channel(tmp_path)
    sent = []
    calls = []

    async def send(self, msg):
        sent.append(msg.content)

    async def chat(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0)
        return "正常回复"

    monkeypatch.setattr(NapcatChannel, "send", send)
    monkeypatch.setattr(channel.ai, "chat", chat)
    try:
        await asyncio.gather(
            channel._on_message(event(1, "#ai 你好")),
            channel._send_daily_summary("1", "不应发布的旧群报"),
            channel._on_message(event(2, "牛牛，你好", user=20)),
        )
        assert sent == ["正常回复", "正常回复"]
        assert len(calls) == 2
        await channel._send_daily_summary("1", "显式请求的作用域不能泄漏到后台")
        assert len(sent) == 2
        await channel._on_message(event(3, "#帮助"))
        assert len(sent) == 3 and "命令" in sent[-1]
    finally:
        await channel.stop()


async def test_quiet_blocks_proactive_model_requests_and_raw_automatic_sends(tmp_path, monkeypatch):
    channel = make_channel(tmp_path)
    calls = []

    async def unexpected(*args, **kwargs):
        calls.append(args)
        raise AssertionError("quiet automatic path must not request AI or OneBot sends")

    monkeypatch.setattr(channel.ai, "chat", unexpected)
    monkeypatch.setattr(channel, "_call_action", unexpected)
    try:
        await channel._on_message(event(1, "客户端卡住了"))
        await channel._on_message(event(2, "我也看看设置", user=20))
        await channel._on_message(event(3, "有没有人知道这个问题怎么解决？"))
        await channel._send_content_relay("1", [])
        await channel._send_group_level_reminder("1", ())
        assert not calls
    finally:
        await channel.stop()


async def test_old_inbound_commands_not_replayed_and_quiet_expires(tmp_path, monkeypatch):
    cutoff = datetime.now(UTC) - timedelta(minutes=1)
    channel = make_channel(tmp_path, messageReplayIgnoreBefore=cutoff.isoformat())
    sent = []

    async def send(self, msg):
        sent.append(msg.content)

    monkeypatch.setattr(NapcatChannel, "send", send)
    try:
        await channel._on_message(event(1, "#帮助", time=cutoff.timestamp() - 10))
        assert not sent
        await channel._on_message(event(2, "#帮助", time=cutoff.timestamp() + 10))
        assert len(sent) == 1
        channel.config.automatic_quiet_until = datetime.now(UTC) - timedelta(seconds=1)
        await channel._send_daily_summary("1", "到期后正常播报")
        assert sent[-1] == "到期后正常播报"
    finally:
        await channel.stop()


async def test_quiet_match_still_records_rewards_without_render_or_demo(tmp_path, monkeypatch):
    channel = make_channel(tmp_path)
    rewards = []

    async def reward(group, match, *, announce):
        rewards.append(announce)

    async def unexpected(*args, **kwargs):
        raise AssertionError("must not render, enqueue or comment while quiet")

    monkeypatch.setattr(channel, "_apply_match_rewards", reward)
    monkeypatch.setattr(channel.stats_renderer, "render_match", unexpected)
    monkeypatch.setattr(channel, "_enqueue_match_highlight_with_retry", unexpected)
    monkeypatch.setattr(channel.member_memory, "remember_match", lambda *a: None)
    try:
        match = build_match_snapshot(
            {"matchId": "quiet-test", "kill": 20, "death": 10}, steam_id="76561197960266101"
        )
        await channel._send_match_notification("1", match)
        assert rewards == [False]
    finally:
        await channel.stop()


def test_quiet_deadline_requires_timezone_and_is_opt_in():
    assert PWCS2Config().automatic_quiet_until is None
    with pytest.raises(ValidationError):
        PWCS2Config(automaticQuietUntil="2026-10-27T09:00:00")
