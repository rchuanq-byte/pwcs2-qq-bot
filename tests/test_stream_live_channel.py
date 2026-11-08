import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.napcat import NapcatChannel

from pwcs2_bot.channel import PWCS2Channel, PWCS2Config
from pwcs2_bot.stream_live import (
    LivePlatformClient,
    StreamerTarget,
    StreamLiveError,
)


def _channel_config(
    tmp_path: Path,
    *,
    allowed_groups: list[str],
    stream_live_enabled: bool = True,
) -> dict[str, Any]:
    return {
        "enabled": True,
        "wsUrl": "ws://127.0.0.1:3001",
        "accessToken": "test-token",
        "allowFrom": ["*"],
        "pwToken": "pw-test-token",
        "databasePath": str(tmp_path / "bindings.db"),
        "allowedGroups": allowed_groups,
        "streamLiveEnabled": stream_live_enabled,
        "streamLiveTargets": [
            {
                "platform": "douyu",
                "name": "Machine",
                "roomId": "6979222",
                "url": "https://www.douyu.com/6657",
            }
        ],
        "matchNotificationsEnabled": False,
        "predictionEnabled": False,
        "dailySummaryEnabled": False,
        "dailyAdminEnabled": False,
        "membershipSyncEnabled": False,
        "groupLevelReminderEnabled": False,
        "aiMemberMemoryEnabled": False,
        "dailyWordCloudEnabled": False,
        "skinPriceEnabled": False,
        "weeklyChemistryEnabled": False,
        "grenadeQuizEnabled": False,
        "weeklyReportEnabled": False,
        "tsEnabled": False,
        "valorantEnabled": False,
        "aiEnabled": False,
    }


def test_library_default_disables_live_monitor_but_project_config_enables_it() -> None:
    assert PWCS2Config().stream_live_enabled is False
    assert PWCS2Channel.default_config()["streamLiveEnabled"] is False

    project_config = json.loads(
        (Path(__file__).parents[1] / "config" / "config.json").read_text(
            encoding="utf-8"
        )
    )
    live_config = PWCS2Config.model_validate(project_config["channels"]["pwcs2"])
    assert live_config.stream_live_enabled is True
    assert live_config.stream_live_targets[2].room_id == "59901412180"
    assert live_config.stream_live_targets[2].expected_sec_uid == (
        "MS4wLjABAAAAd0oHxHUvBQGLCc5xbYs1pnCtsojy7MBXZCYcjJ1kxuOmrdL0ZSuyDfZYoxVReCBD"
    )


def test_live_target_duplicate_validation_only_counts_enabled_targets() -> None:
    with pytest.raises(ValueError, match="duplicate rooms"):
        PWCS2Config.model_validate(
            {
                "streamLiveTargets": [
                    {"platform": "huya", "name": "A", "roomId": "10188"},
                    {"platform": "huya", "name": "B", "roomId": "10188"},
                ]
            }
        )

    config = PWCS2Config.model_validate(
        {
            "streamLiveTargets": [
                {"platform": "huya", "name": "A", "roomId": "10188"},
                {
                    "platform": "huya",
                    "name": "disabled duplicate",
                    "roomId": "10188",
                    "enabled": False,
                },
            ]
        }
    )
    assert len(config.stream_live_targets) == 2


async def test_empty_allowed_groups_prevents_channel_live_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport_entered = asyncio.Event()
    transport_release = asyncio.Event()

    async def fake_transport_start(_channel: NapcatChannel) -> None:
        transport_entered.set()
        await transport_release.wait()

    async def fake_transport_stop(_channel: NapcatChannel) -> None:
        transport_release.set()

    monkeypatch.setattr(NapcatChannel, "start", fake_transport_start)
    monkeypatch.setattr(NapcatChannel, "stop", fake_transport_stop)
    channel = PWCS2Channel(
        _channel_config(tmp_path, allowed_groups=[]),
        MessageBus(),
    )
    start_task = asyncio.create_task(channel.start())
    try:
        await asyncio.wait_for(transport_entered.wait(), timeout=1)
        assert channel.stream_live.enabled is False
        assert channel._stream_live_task is None
    finally:
        transport_release.set()
        await asyncio.wait_for(start_task, timeout=1)
        await channel.stop()


async def test_channel_start_and_stop_own_live_monitor_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport_release = asyncio.Event()
    monitor_started = asyncio.Event()
    monitor_cancelled = asyncio.Event()
    close_calls = 0

    async def fake_transport_start(_channel: NapcatChannel) -> None:
        await transport_release.wait()

    async def fake_transport_stop(_channel: NapcatChannel) -> None:
        transport_release.set()

    monkeypatch.setattr(NapcatChannel, "start", fake_transport_start)
    monkeypatch.setattr(NapcatChannel, "stop", fake_transport_stop)
    channel = PWCS2Channel(
        _channel_config(tmp_path, allowed_groups=["123"]),
        MessageBus(),
    )

    async def fake_monitor_run() -> None:
        monitor_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            monitor_cancelled.set()

    original_close = channel.stream_live.close

    async def checked_close() -> None:
        nonlocal close_calls
        close_calls += 1
        await original_close()

    monkeypatch.setattr(channel.stream_live, "run", fake_monitor_run)
    monkeypatch.setattr(channel.stream_live, "close", checked_close)

    start_task = asyncio.create_task(channel.start())
    await asyncio.wait_for(monitor_started.wait(), timeout=1)
    live_task = channel._stream_live_task
    assert live_task is not None
    assert live_task.get_name() == "pwcs2-stream-live"

    await channel.stop()
    await asyncio.wait_for(start_task, timeout=1)

    assert channel._stream_live_task is None
    assert live_task.cancelled()
    assert monitor_cancelled.is_set()
    assert close_calls == 1


async def test_live_notification_callback_uses_reply_and_group_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = PWCS2Channel(
        _channel_config(tmp_path, allowed_groups=["123"]),
        MessageBus(),
    )
    sent: list[OutboundMessage] = []

    async def capture(message: OutboundMessage) -> None:
        sent.append(message)

    monkeypatch.setattr(channel, "send", capture)
    try:
        await channel._send_stream_live_notification("123", "DANK1NG is live")
        with pytest.raises(RuntimeError, match="outbound QQ group is not allowed"):
            await channel._send_stream_live_notification("456", "must not send")
    finally:
        await channel.stop()

    assert len(sent) == 1
    assert sent[0].chat_id == "group:123"
    assert sent[0].content == "DANK1NG is live"


@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_provider_http_failure_stays_unknown(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    client = LivePlatformClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(StreamLiveError, match="request failed"):
            await client.fetch(
                StreamerTarget(
                    platform="douyu",
                    name="Machine",
                    roomId="6979222",
                )
            )
    finally:
        await client.close()
