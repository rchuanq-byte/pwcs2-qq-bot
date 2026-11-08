import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pwcs2_bot.astrbot_transport import AstrBotCSApplication


class FakeBot:
    def __init__(self):
        self._wsr_api_clients = {999: object()}
        self.calls = []

    async def call_action(self, name, **params):
        self.calls.append((name, params))
        return {"message_id": len(self.calls)}


def make_app(tmp_path, **settings):
    app = AstrBotCSApplication(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "allowedGroups": ["1"],
            "privateRepliesEnabled": False,
            "databasePath": str(tmp_path / "bot.db"),
            "pwToken": "test",
            "unboundGuardEnabled": False,
            "trainingGameEnabled": False,
            **settings,
        }
    )
    app.bind_bot(FakeBot(), 999)
    return app


async def test_astrbot_owns_connection_and_runs_existing_help(tmp_path):
    app = make_app(tmp_path)
    try:
        await app._on_message(
            {
                "message_type": "group",
                "group_id": 1,
                "user_id": 10,
                "message_id": 1,
                "message": [{"type": "text", "data": {"text": "#帮助"}}],
                "sender": {"nickname": "test", "role": "member"},
            }
        )
        assert len(app.bot.calls) == 1
        name, params = app.bot.calls[0]
        assert name == "send_msg" and params["self_id"] == 999 and params["group_id"] == 1
        assert "命令" in params["message"][0]["data"]["text"]
        assert 1 in app._bot_outbound_ids
    finally:
        await app.stop()


async def test_astrbot_quiet_allows_commands_and_blocks_background(tmp_path):
    app = make_app(
        tmp_path, automaticQuietUntil=(datetime.now(UTC) + timedelta(days=1)).isoformat()
    )
    try:
        await app._send_daily_summary("1", "automatic")
        assert not app.bot.calls
        await app._on_message(
            {
                "message_type": "group",
                "group_id": 1,
                "user_id": 10,
                "message_id": 1,
                "message": [{"type": "text", "data": {"text": "#我的绑定"}}],
                "sender": {"nickname": "test", "role": "member"},
            }
        )
        assert len(app.bot.calls) == 1
        await app._send_daily_summary("1", "still automatic")
        assert len(app.bot.calls) == 1
    finally:
        await app.stop()


async def test_astrbot_action_normalization_disconnect_and_timeout(tmp_path):
    app = make_app(tmp_path)
    try:
        assert (await app._call_action("get_status", {}))["retcode"] == 0
        app.bot._wsr_api_clients.clear()
        assert app._ws is None
        with pytest.raises(RuntimeError, match="not ready"):
            await app._call_action("send_msg", {})
        app.bot._wsr_api_clients = {"999": object()}

        async def slow(*a, **kw):
            await asyncio.sleep(10)

        app.bot.call_action = slow
        with pytest.raises(TimeoutError):
            await app._call_action("get_status", {}, timeout=0.01)
    finally:
        await app.stop()


async def test_astrbot_preserves_group_boundaries_and_no_legacy_chat(tmp_path):
    app = make_app(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="not allowed"):
            await app._reply("group", "2", "no")
        await app._handle_ai_message(SimpleNamespace())
        assert not app.bot.calls
    finally:
        await app.stop()


async def test_astrbot_background_callbacks_are_owned_and_cancelled(tmp_path):
    app = make_app(tmp_path)
    ready = asyncio.Event()

    async def work():
        ready.set()
        await asyncio.sleep(100)

    app._create_background_task(work(), "card-restore")
    await ready.wait()
    assert len(app._background_tasks) == 1
    await app.stop()
    assert not app._background_tasks
