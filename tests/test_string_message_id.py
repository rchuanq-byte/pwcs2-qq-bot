from pathlib import Path

from nanobot.bus.queue import MessageBus

from pwcs2_bot.channel import PWCS2Channel


async def test_string_message_id_is_deduplicated(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    replies: list[str] = []

    async def capture(_kind: str, _target: str, content: str) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_reply", capture)
    event = {
        "message_type": "group",
        "message_id": "9216437273368856076",
        "user_id": "123456",
        "group_id": "987654",
        "message": [{"type": "text", "data": {"text": "#帮助"}}],
    }
    try:
        await channel._on_message(event)
        await channel._on_message(event)
    finally:
        await channel.perfectworld.close()

    assert len(replies) == 1

