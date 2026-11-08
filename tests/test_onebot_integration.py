import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from nanobot.bus.queue import MessageBus
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from pwcs2_bot.channel import PWCS2Channel
from pwcs2_bot.healthcheck import read_problem


async def test_real_websocket_onebot_command_round_trip(tmp_path: Path) -> None:
    reply_received = asyncio.Event()
    outbound: dict[str, Any] = {}
    authorization: list[str | None] = []

    async def napcat(connection: ServerConnection) -> None:
        authorization.append(connection.request.headers.get("Authorization"))

        login_action = json.loads(await connection.recv())
        assert login_action["action"] == "get_login_info"
        await connection.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 999999, "nickname": "Test Bot"},
                    "echo": login_action["echo"],
                }
            )
        )

        await connection.send(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "self_id": 999999,
                    "message_id": "9216437273368856076",
                    "user_id": 123456,
                    "group_id": 987654,
                    "sender": {"nickname": "Alice", "card": ""},
                    "message": [
                        {
                            "type": "text",
                            "data": {"text": "#绑定 76561198929215155"},
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

        action = json.loads(await connection.recv())
        outbound.update(action)
        await connection.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": "9216437273368856077"},
                    "echo": action["echo"],
                }
            )
        )
        reply_received.set()
        await connection.wait_closed()

    async with serve(napcat, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        channel = PWCS2Channel(
            {
                "enabled": True,
                "wsUrl": f"ws://127.0.0.1:{port}",
                "accessToken": "onebot-secret",
                "allowFrom": ["*"],
                "pwToken": "pw-secret",
                "databasePath": str(tmp_path / "bindings.db"),
            },
            MessageBus(),
        )
        channel_task = asyncio.create_task(channel.start())
        try:
            await asyncio.wait_for(reply_received.wait(), timeout=5)
        finally:
            await channel.stop()
            await asyncio.wait_for(channel_task, timeout=5)

    assert authorization == ["Bearer onebot-secret"]
    assert outbound["action"] == "send_msg"
    assert outbound["params"]["message_type"] == "group"
    assert outbound["params"]["group_id"] == 987654
    assert outbound["params"]["message"] == [
        {
            "type": "text",
            "data": {
                "text": "绑定成功：QQ 123456 → SteamID 76561198929215155",
            },
        }
    ]


async def test_stale_active_socket_reconnects_without_restarting_qq(
    tmp_path: Path, monkeypatch,
) -> None:
    connections = 0
    actions = []

    async def napcat(connection: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        generation = connections
        with suppress(ConnectionClosed):
            async for raw in connection:
                request = json.loads(raw)
                actions.append(request["action"])
                if request["action"] == "get_login_info":
                    data = {"user_id": 999999, "nickname": "Test Bot"}
                elif generation == 1:
                    # TCP/WebSocket is still up, but the OneBot application is hung.
                    continue
                else:
                    data = {"online": True, "good": True}
                await connection.send(json.dumps({
                    "status": "ok", "retcode": 0, "data": data, "echo": request["echo"],
                }))

    async with serve(napcat, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        channel = PWCS2Channel(
            {"enabled": True, "wsUrl": f"ws://127.0.0.1:{port}",
             "databasePath": str(tmp_path / "bindings.db")}, MessageBus(),
        )
        channel.transport_health.interval = 0.05
        channel.transport_health.timeout = 0.05
        ready = asyncio.Event()
        publish = channel.transport_health.publish

        async def observe_publish(state, **kwargs):
            await publish(state, **kwargs)
            if state == "online":
                ready.set()

        monkeypatch.setattr(channel.transport_health, "publish", observe_publish)
        task = asyncio.create_task(channel.start())
        try:
            await asyncio.wait_for(ready.wait(), timeout=12)
            assert connections == 2
            assert read_problem(channel.transport_health.path) is None
            assert set(actions) == {"get_login_info", "get_status"}
        finally:
            await channel.stop()
            await asyncio.wait_for(task, timeout=5)
        assert channel._transport_health_task is None
        assert not channel._pending
        assert json.loads(channel.transport_health.path.read_text())["state"] == "stopped"
