import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_astrbot_transport import make_app

from pwcs2_bot.ai_chat import AIChatService
from pwcs2_bot.astrbot_policy import (
    AstrBotOutboundPolicy,
    OutboundBlocked,
    classify,
    reserve_automatic_turn,
)
from pwcs2_bot.astrbot_runtime import (
    SharedGroupConcurrency,
    bounded_history,
    clean_completed_turn,
    configure_conversation_frequency,
    turn_tail,
)


class Event:
    def __init__(self, **extra):
        self.extra = extra

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value


async def test_upstream_frequency_mode_never_bypasses_quiet_or_group_guards(tmp_path):
    app = make_app(
        tmp_path,
        aiCompanionEnabled=True,
        aiCompanionGroups=["1"],
        aiCompanionProactiveEnabled=True,
        aiCompanionUseUpstreamLimits=True,
    )
    try:
        assert reserve_automatic_turn(app, "1")
        app.companion.release("1")
        assert reserve_automatic_turn(app, "1")  # No additional 30-minute budget.
        app.companion.release("1")
        assert not reserve_automatic_turn(app, "2")
        app.config.automatic_quiet_until = datetime.now(UTC) + timedelta(days=1)
        assert not reserve_automatic_turn(app, "1")
        app.config.automatic_quiet_until = None
        app.companion.proactive = False
        assert not reserve_automatic_turn(app, "1")
    finally:
        await app.stop()


def test_upstream_defaults_remove_only_deployment_frequency_overrides():
    environment = {"DSH_PROACTIVE_OWNER": "123", "NIUNIU_QUIET_UNTIL": "unchanged"}
    original = dict(environment)
    configure_conversation_frequency(environment, upstream_defaults=False)
    assert environment["DSH_PROACTIVE_MIN_INTERVAL_MS"] == "1800000"
    assert environment["DSH_INITIATE_COOLDOWN"] == "10800"
    configure_conversation_frequency(environment, upstream_defaults=True)
    assert environment == original


async def test_native_plugin_sends_cannot_bypass_quiet_allowlist_or_budget(tmp_path):
    app = make_app(
        tmp_path, automaticQuietUntil=(datetime.now(UTC) + timedelta(days=1)).isoformat()
    )
    try:
        policy = AstrBotOutboundPolicy(app, lambda: None)
        for action in (
            "send_msg",
            "send_group_forward_msg",
            "upload_group_file",
            "_send_group_notice",
        ):
            with pytest.raises(OutboundBlocked, match="paused"):
                policy.validate(action, {"group_id": 1})
            policy.validate(action, {"group_id": 1}, Event(niuniu_explicit=True))
            with pytest.raises(OutboundBlocked, match="allowlist"):
                policy.validate(action, {"group_id": 2}, Event(niuniu_explicit=True))
        with pytest.raises(OutboundBlocked, match="Private"):
            policy.validate("send_private_msg", {"user_id": 10}, Event(niuniu_explicit=True))
        app.config.automatic_quiet_until = None
        with pytest.raises(OutboundBlocked, match="reserved"):
            policy.validate("send_group_msg", {"group_id": 1}, Event())
        policy.validate("send_group_msg", {"group_id": 1}, Event(niuniu_auto_reserved=True))
        policy.validate("get_group_member_list", {"group_id": 1}, Event())
    finally:
        await app.stop()


async def test_lowest_native_api_is_guarded_and_tracks_actual_outbound_ids(tmp_path):
    app = make_app(tmp_path)
    try:
        called = []

        async def call(action, **params):
            called.append(action)
            return {"message_id": 123}

        native = SimpleNamespace(_api=SimpleNamespace(call_action=call))
        event = Event(niuniu_explicit=True)
        AstrBotOutboundPolicy(app, lambda: event).install(native)
        await native._api.call_action("send_group_msg", group_id=1, message="hi")
        assert called == ["send_group_msg"] and 123 in app._bot_outbound_ids
        with pytest.raises(OutboundBlocked):
            await native._api.call_action("send_group_msg", group_id=2, message="no")
        assert len(called) == 1
    finally:
        await app.stop()


async def test_routing_preserves_explicit_commands_and_rejects_replay(tmp_path):
    app = make_app(tmp_path, messageReplayIgnoreBefore="2026-10-26T00:00:00Z")
    try:
        raw = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 1,
            "user_id": 10,
            "message": "#帮助",
            "time": 1793012400,
        }
        assert classify(app, raw).command is not None
        assert classify(app, raw).explicit
        assert not classify(app, {**raw, "time": 1}).allowed
        assert not classify(app, {**raw, "group_id": 2}).allowed
        assert not classify(app, {**raw, "user_id": 999}).allowed
        assert classify(app, {**raw, "message": "你好"}, quoted_bot=True).explicit
    finally:
        await app.stop()


async def test_parallel_turns_append_without_lost_updates_or_stale_replacement():
    current = ContextVar("test_event", default=None)
    rows = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old reply"}]

    class Manager:
        cid = "one"

        async def get_curr_conversation_id(self, _umo):
            return self.cid

        async def get_conversation(self, _umo, _cid):
            return SimpleNamespace(history=json.dumps(rows))

        async def update_conversation(self, _umo, conversation_id=None, history=None, **kwargs):
            rows[:] = history

    class Locks:
        @asynccontextmanager
        async def acquire_lock(self, _umo):
            yield

    manager, locks = Manager(), Locks()
    adapter = SharedGroupConcurrency(current.get, maximum=20)
    adapter.install(locks, manager)
    active, peak = 0, 0

    async def run(number):
        nonlocal active, peak
        event = Event(niuniu_ai=True, niuniu_request_started=True, niuniu_explicit=True)
        token = current.set(event)
        try:
            async with locks.acquire_lock("group"):
                snapshot = list(rows)
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                tail = [
                    {"role": "user", "content": str(number)},
                    {"role": "assistant", "content": f"reply {number}"},
                ]
                await manager.update_conversation("group", "one", history=snapshot + tail)
                active -= 1
        finally:
            current.reset(token)

    await asyncio.gather(*(run(n) for n in range(20)))
    assert peak == 20 and len(rows) == 42
    assert {m["content"] for m in rows if m["role"] == "user"} == {
        "old",
        *(str(n) for n in range(20)),
    }
    token = current.set(Event(niuniu_ai=True, niuniu_request_started=True, niuniu_explicit=True))
    try:
        manager.cid = "cleared"
        await manager.update_conversation(
            "group",
            "one",
            history=[{"role": "user", "content": "stale"}, {"role": "assistant", "content": "no"}],
        )
        assert all(m["content"] != "stale" for m in rows)
    finally:
        current.reset(token)

    manager.cid = "one"
    event = Event(
        niuniu_ai=True,
        niuniu_request_started=True,
        niuniu_explicit=False,
        niuniu_history_user="automatic context",
    )
    token = current.set(event)
    proposal = [
        {"role": "user", "content": "injected profiles"},
        {"role": "assistant", "content": "auto answer"},
    ]
    try:
        await manager.update_conversation("group", "one", history=proposal)
        assert len(rows) == 42  # A draft has not been said in the group.
        event.set_extra("niuniu_suppress", True)
        await adapter.sent(event)
        assert len(rows) == 42
        event.set_extra("niuniu_suppress", False)
        await manager.update_conversation("group", "one", history=proposal)
        await adapter.sent(event)
        assert rows[-2:] == [
            {"role": "user", "content": "automatic context"},
            {"role": "assistant", "content": "auto answer"},
        ]
    finally:
        current.reset(token)


def test_history_preserves_complete_current_tool_turn():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "tool_calls": [{}]},
        {"role": "tool", "content": "result"},
        {"role": "assistant", "content": "answer"},
    ]
    assert turn_tail(messages) == messages[2:]
    assert bounded_history(messages, maximum=5) == messages[2:]
    assert clean_completed_turn(messages, "original question [附图1张]") == [
        {"role": "user", "content": "original question [附图1张]"},
        {"role": "assistant", "content": "answer"},
    ]


async def test_vision_reading_is_stateless_without_group_persona_or_tools():
    service = AIChatService(
        "test-key", enabled=True, model="deepseek-flash", vision_model="deepseek-flash"
    )
    seen = []

    async def complete(messages, **kwargs):
        seen.append((messages, kwargs))
        return "图像是纯蓝色"

    service._complete = complete
    try:
        answer = await service.describe_images(
            prompt="主色是什么？", image_data_urls=("data:image/png;base64,test",)
        )
        assert answer == "图像是纯蓝色"
        messages, kwargs = seen[0]
        assert len(messages) == 2 and messages[0]["role"] == "system"
        assert messages[1]["content"][1]["type"] == "image_url"
        assert "toolbox" not in kwargs and not service._histories
    finally:
        await service.close()
