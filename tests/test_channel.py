import asyncio
import base64
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nanobot.bus.queue import MessageBus

from pwcs2_bot.ai_chat import AIChatError
from pwcs2_bot.channel import PWCS2Channel, split_outbound_text
from pwcs2_bot.chemistry import build_chemistry_network
from pwcs2_bot.commands import CommandResult
from pwcs2_bot.group_level_reminder import (
    LEVEL_REASON,
    UNBOUND_REASON,
    ComplianceReminder,
)
from pwcs2_bot.match_formatting import build_match_snapshot
from pwcs2_bot.skin_prices import SkinPriceReport, SkinQuote, SkinReportEntry
from pwcs2_bot.stats_card import StatsCardRenderError
from pwcs2_bot.steam_presence import GameStatusEntry, GameStatusSnapshot
from pwcs2_bot.training_game import (
    format_training_game_intro,
    training_game_html_path,
)
from pwcs2_bot.valorant import ValorantSnapshot
from pwcs2_bot.watch_stage import WatchStageSnapshot

TZ = ZoneInfo("Asia/Shanghai")


def test_split_outbound_text_preserves_complete_long_answer() -> None:
    content = "完整回答。" * 1200

    chunks = split_outbound_text(content, limit=500)

    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks) == content


async def test_channel_intercepts_command_without_forwarding_to_agent(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "wsUrl": "ws://127.0.0.1:3001",
            "accessToken": "secret",
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "forwardUnmatched": False,
        },
        MessageBus(),
    )
    replies: list[tuple[str, str, str]] = []

    async def capture(kind: str, target: str, content: str) -> None:
        replies.append((kind, target, content))

    monkeypatch.setattr(channel, "_reply", capture)
    event = {
        "message_type": "group",
        "message_id": 100,
        "user_id": 123456,
        "group_id": 987654,
        "message": [
            {"type": "text", "data": {"text": "#绑定 76561198929215155"}},
        ],
    }
    try:
        await channel._on_message(event)
        await channel._on_message(event)
        await channel._on_message(
            {
                **event,
                "message_id": 101,
                "message": [{"type": "text", "data": {"text": "普通群消息"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == [
        (
            "group",
            "987654",
            "绑定成功：QQ 123456 → SteamID 76561198929215155",
        )
    ]


async def test_channel_rejects_disallowed_group(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "allowedGroups": ["111"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    replies: list[str] = []

    async def capture(_kind: str, _target: str, content: str) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 200,
                "user_id": 123456,
                "group_id": 222,
                "message": [{"type": "text", "data": {"text": "#帮助"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == []


async def test_channel_records_only_normal_target_group_chat_for_word_cloud(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "aiEnabled": False,
        },
        MessageBus(),
    )

    async def discard(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 205,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "今晚一起荒漠迷城"}}],
            }
        )
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 206,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "#帮助"}}],
            }
        )
    finally:
        await channel.stop()

    message_count, token_count = channel.repository.get_daily_word_cloud_activity(
        "987654", datetime.now(TZ).date().isoformat()
    )
    assert message_count == 1
    assert token_count > 0
    assert channel.repository.get_daily_member_message_counts(
        "987654", datetime.now(TZ).date().isoformat()
    ) == {"123456": 2}


async def test_channel_sends_configured_xh_reaction_once_and_skips_ai(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = tmp_path / "assets" / "reactions" / "xh-defeat.png"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"synthetic test fixture")
    monkeypatch.setenv("NIUNIU_XH_REACTION_IMAGE", str(fixture))
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "unboundGuardEnabled": False,
            "aiEnabled": True,
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    replies: list[tuple[str, str, str, list[str]]] = []

    async def capture(
        kind: str,
        target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((kind, target, content, media or []))

    async def fail_chat(**_kwargs) -> str:
        raise AssertionError("xh reaction must not call AI")

    monkeypatch.setattr(channel, "_reply", capture)
    monkeypatch.setattr(channel.ai, "chat", fail_chat)
    event = {
        "message_type": "group",
        "message_id": 2061,
        "user_id": 123456,
        "group_id": 987654,
        "sender": {"card": "Alice", "role": "member"},
        "message": [{"type": "text", "data": {"text": "这把 XHNB 了"}}],
    }
    try:
        await channel._on_message(event)
        await channel._on_message(event)
    finally:
        await channel.stop()

    assert len(replies) == 1
    kind, target, content, media = replies[0]
    assert (kind, target, content) == ("group", "987654", "")
    assert len(media) == 1
    assert media[0].replace("\\", "/").endswith("/assets/reactions/xh-defeat.png")


async def test_channel_distributes_training_game_and_enforces_group_cooldown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    game_url = "https://game.example.test/cs2-training.html"
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "unboundGuardEnabled": False,
            "aiEnabled": True,
            "aiApiKey": "sk-test-secret",
            "trainingGameUrl": game_url,
            "trainingGameGroupCooldownSeconds": 1800,
            "trainingGameUserCooldownSeconds": 60,
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    replies: list[str] = []

    async def capture(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    async def fail_chat(**_kwargs) -> str:
        raise AssertionError("passive training-game distribution must not call AI")

    monkeypatch.setattr(channel, "_reply", capture)
    monkeypatch.setattr(channel.ai, "chat", fail_chat)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 2062,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "Alice", "role": "member"},
                "message": [{"type": "text", "data": {"text": "今晚练枪"}}],
            }
        )
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 2063,
                "user_id": 222222,
                "group_id": 987654,
                "sender": {"card": "Bob", "role": "member"},
                "message": [{"type": "text", "data": {"text": "CS2"}}],
            }
        )
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 2064,
                "user_id": 222222,
                "group_id": 987654,
                "sender": {"card": "Bob", "role": "member"},
                "message": [{"type": "text", "data": {"text": "把小游戏链接发我"}}],
            }
        )
    finally:
        await channel.stop()

    intro = format_training_game_intro(game_url)
    assert replies == [intro, intro]


async def test_channel_routes_addressed_training_game_question_to_ai(
    tmp_path: Path,
    monkeypatch,
) -> None:
    game_url = "https://game.example.test/cs2-training.html"
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "unboundGuardEnabled": False,
            "aiEnabled": True,
            "aiApiKey": "sk-test-secret",
            "trainingGameUrl": game_url,
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    prompts: list[dict] = []
    replies: list[str] = []

    async def fake_chat(**kwargs) -> str:
        prompts.append(kwargs)
        return "四种模式：爆头、跟枪、压枪和反应测试。"

    async def capture(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 2065,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "Alice", "role": "member"},
                "message": [
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {
                        "type": "text",
                        "data": {"text": " CS2 训练营有哪些模式"},
                    },
                ],
            }
        )
    finally:
        await channel.stop()

    assert len(prompts) == 1
    assert prompts[0]["content"] == "CS2 训练营有哪些模式"
    assert game_url in channel.ai.system_prompt
    assert "金色靶 $900" in channel.ai.system_prompt
    assert replies == ["Alice：\n四种模式：爆头、跟枪、压枪和反应测试。"]


async def test_channel_uploads_packaged_training_game_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "unboundGuardEnabled": False,
            "aiEnabled": False,
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    actions: list[tuple[str, dict]] = []
    replies: list[str] = []

    async def action(name: str, params: dict) -> dict:
        actions.append((name, params))
        return {"status": "ok", "retcode": 0}

    async def capture(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_call_action", action)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 2066,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "Alice", "role": "member"},
                "message": [{"type": "text", "data": {"text": "#训练文件"}}],
            }
        )
    finally:
        await channel.stop()

    assert len(actions) == 1
    action_name, params = actions[0]
    assert action_name == "upload_group_file"
    assert params["group_id"] == 987654
    assert params["name"] == "cs2-training.html"
    assert params["file"].startswith("base64://")
    assert base64.b64decode(params["file"].removeprefix("base64://")) == (
        training_game_html_path().read_bytes()
    )
    assert replies == ["已上传 CS2 战术训练营原文件；下载后双击即可运行。"]


async def test_channel_falls_back_to_training_game_link_when_file_upload_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    game_url = "https://game.example.test/cs2-training.html"
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "unboundGuardEnabled": False,
            "aiEnabled": False,
            "trainingGameUrl": game_url,
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    replies: list[str] = []

    async def failed_action(name: str, _params: dict) -> dict:
        assert name == "upload_group_file"
        raise RuntimeError("temporary OneBot upload failure")

    async def capture(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_call_action", failed_action)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 2067,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "Alice", "role": "member"},
                "message": [{"type": "text", "data": {"text": "#训练文件"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == [f"群文件上传失败，可直接打开或另存为：{game_url}"]


async def test_admin_can_rename_and_lock_another_members_group_card(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
        },
        MessageBus(),
    )
    channel._self_id = 999999
    members = [
        {"user_id": 999999, "role": "owner", "is_robot": True},
        {"user_id": 123456, "role": "admin", "is_robot": False},
        {
            "user_id": 222222,
            "role": "member",
            "is_robot": False,
            "card": "旧名字",
        },
    ]
    actions: list[tuple[str, dict]] = []
    replies: list[str] = []

    async def fetch(_group_id: str):
        return members

    async def action(name: str, params: dict):
        actions.append((name, params))
        if name == "get_group_member_info":
            role = "admin" if params["user_id"] == 123456 else "member"
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "user_id": params["user_id"],
                    "role": role,
                    "is_robot": False,
                },
            }
        assert channel.repository.get_group_card_lock("987654", "222222") == "牛牛战神"
        return {"status": "ok", "retcode": 0}

    async def reply(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_fetch_group_members", fetch)
    monkeypatch.setattr(channel, "_call_action", action)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 207,
                "user_id": 123456,
                "group_id": 987654,
                # The message role may be stale immediately after promotion;
                # fresh NapCat member data is authoritative.
                "sender": {"role": "member", "card": "值日管理"},
                "message": [
                    {"type": "text", "data": {"text": "#改名 "}},
                    {"type": "at", "data": {"qq": "222222"}},
                    {"type": "text", "data": {"text": " 牛牛战神"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert actions == [
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 123456, "no_cache": True},
        ),
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 222222, "no_cache": True},
        ),
        (
            "set_group_card",
            {"group_id": 987654, "user_id": 222222, "card": "牛牛战神"},
        ),
    ]
    assert replies == [
        "改名成功：QQ 222222 → 牛牛战神（已锁定，成员自行修改会自动恢复）"
    ]


async def test_member_cannot_use_group_card_rename_command(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    replies: list[str] = []

    async def fetch(_group_id: str):
        return [
            {"user_id": 123456, "role": "member", "is_robot": False},
            {"user_id": 222222, "role": "member", "is_robot": False},
        ]

    async def reply(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_fetch_group_members", fetch)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 208,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"role": "member"},
                "message": [
                    {"type": "text", "data": {"text": "#改名 "}},
                    {"type": "at", "data": {"qq": "222222"}},
                    {"type": "text", "data": {"text": " 新名字"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert replies == ["发起者已不再是群管理员"]


async def test_group_card_notice_restores_locked_name(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
        },
        MessageBus(),
    )
    channel._self_id = 999999
    channel.repository.set_group_card_lock(
        "987654", "222222", "锁定名", updated_by_qq="123456"
    )
    actions: list[tuple[str, dict]] = []

    async def action(name: str, params: dict):
        actions.append((name, params))
        if name == "get_group_member_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "user_id": 222222,
                    "role": "member",
                    "is_robot": False,
                },
            }
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(channel, "_call_action", action)
    try:
        await channel._on_notice(
            {
                "notice_type": "group_card",
                "group_id": 987654,
                "user_id": 222222,
                "card_old": "锁定名",
                "card_new": "我自己改的",
            }
        )
    finally:
        await channel.stop()

    assert actions == [
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 222222, "no_cache": True},
        ),
        (
            "set_group_card",
            {"group_id": 987654, "user_id": 222222, "card": "锁定名"},
        ),
    ]


async def test_first_group_card_notice_with_empty_old_card_uses_fresh_nickname(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
        },
        MessageBus(),
    )
    channel._self_id = 999999
    actions: list[tuple[str, dict]] = []

    async def action(name: str, params: dict):
        actions.append((name, params))
        if name == "get_group_member_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "user_id": 222222,
                    "role": "member",
                    "is_robot": False,
                    "card": "Member changed this",
                    "nickname": "Stable Nickname",
                },
            }
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(channel, "_call_action", action)
    try:
        await channel._on_notice(
            {
                "notice_type": "group_card",
                "group_id": 987654,
                "user_id": 222222,
                "card_old": "",
                "card_new": "Member changed this",
            }
        )
        assert channel.repository.get_group_card_lock("987654", "222222") == (
            "Stable Nickname"
        )

        # Once the stable fallback has been persisted, a matching event must
        # not issue another member lookup or set_group_card action.
        await channel._on_notice(
            {
                "notice_type": "group_card",
                "group_id": 987654,
                "user_id": 222222,
                "card_old": "Member changed this",
                "card_new": "Stable Nickname",
            }
        )
    finally:
        await channel.stop()

    assert actions == [
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 222222, "no_cache": True},
        ),
        (
            "set_group_card",
            {"group_id": 987654, "user_id": 222222, "card": "Stable Nickname"},
        ),
    ]


async def test_group_message_with_drifted_sender_card_triggers_fast_restore(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "aiEnabled": False,
            "forwardUnmatched": False,
        },
        MessageBus(),
    )
    channel._self_id = 999999
    channel.repository.set_group_card_lock(
        "987654", "222222", "Locked Name", updated_by_qq="123456"
    )
    actions: list[tuple[str, dict]] = []
    restored = asyncio.Event()

    async def action(name: str, params: dict):
        actions.append((name, params))
        if name == "get_group_member_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "user_id": 222222,
                    "role": "member",
                    "is_robot": False,
                    "card": "Member changed this",
                },
            }
        if name == "set_group_card":
            restored.set()
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(channel, "_call_action", action)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 209,
                "user_id": 222222,
                "group_id": 987654,
                "sender": {
                    "role": "member",
                    "card": "Member changed this",
                },
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )
        await asyncio.wait_for(restored.wait(), timeout=1)
    finally:
        await channel.stop()

    assert actions == [
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 222222, "no_cache": True},
        ),
        (
            "set_group_card",
            {"group_id": 987654, "user_id": 222222, "card": "Locked Name"},
        ),
    ]


async def test_repeated_drifted_messages_share_one_pending_group_card_restore(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "aiEnabled": False,
            "forwardUnmatched": False,
        },
        MessageBus(),
    )
    channel._self_id = 999999
    channel.repository.set_group_card_lock(
        "987654", "222222", "Locked Name", updated_by_qq="123456"
    )
    restore_started = asyncio.Event()
    restore_release = asyncio.Event()
    restore_calls = 0

    async def restore(_group_id: str, _qq_id: str, **_kwargs) -> bool:
        nonlocal restore_calls
        restore_calls += 1
        restore_started.set()
        await restore_release.wait()
        return True

    monkeypatch.setattr(channel, "_restore_locked_group_card", restore)
    event = {
        "message_type": "group",
        "message_id": 210,
        "user_id": 222222,
        "group_id": 987654,
        "sender": {"role": "member", "card": "Member changed this"},
        "message": [{"type": "text", "data": {"text": "hello"}}],
    }
    try:
        await channel._on_message(event)
        await asyncio.wait_for(restore_started.wait(), timeout=1)
        await channel._on_message({**event, "message_id": 211})
        await asyncio.sleep(0)

        assert restore_calls == 1
    finally:
        restore_release.set()
        if channel._background_tasks:
            await asyncio.gather(*tuple(channel._background_tasks))
        await channel.stop()


async def test_admin_mute_is_immediately_undone_at_ten_minute_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
        },
        MessageBus(),
    )
    channel._self_id = 999999
    actions: list[tuple[str, dict]] = []
    replies: list[str] = []

    async def action(name: str, params: dict):
        actions.append((name, params))
        if name == "get_group_member_info":
            role = "owner" if params["user_id"] == 999999 else "admin"
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": params["user_id"], "role": role},
            }
        return {"status": "ok", "retcode": 0}

    async def reply(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_call_action", action)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._on_notice(
            {
                "notice_type": "group_ban",
                "sub_type": "ban",
                "group_id": 987654,
                "operator_id": 123456,
                "user_id": 222222,
                "duration": 600,
            }
        )
    finally:
        await channel.stop()

    assert actions == [
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 999999, "no_cache": True},
        ),
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 123456, "no_cache": True},
        ),
        (
            "set_group_ban",
            {"group_id": 987654, "user_id": 222222, "duration": 0},
        ),
    ]
    assert len(replies) == 1
    assert "已立即解除" in replies[0]
    assert "管理员身份也已撤销" not in replies[0]
    audit = channel.repository.get_latest_admin_ban_guard_event("987654")
    assert audit is not None
    assert audit.duration_seconds == 600
    assert audit.unmuted is True
    assert audit.admin_revoked is False


async def test_admin_mute_over_ten_minutes_revokes_admin_and_daily_state(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
        },
        MessageBus(),
    )
    channel._self_id = 999999
    channel.repository.complete_daily_admin_assignment(
        "987654", "2026-07-23", "123456"
    )
    actions: list[tuple[str, dict]] = []
    replies: list[str] = []

    async def action(name: str, params: dict):
        actions.append((name, params))
        if name == "get_group_member_info":
            role = "owner" if params["user_id"] == 999999 else "admin"
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": params["user_id"], "role": role},
            }
        return {"status": "ok", "retcode": 0}

    async def reply(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_call_action", action)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._on_notice(
            {
                "notice_type": "group_ban",
                "sub_type": "ban",
                "group_id": 987654,
                "operator_id": 123456,
                "user_id": 222222,
                "duration": 601,
            }
        )
    finally:
        await channel.stop()

    assert actions[-2:] == [
        (
            "set_group_ban",
            {"group_id": 987654, "user_id": 222222, "duration": 0},
        ),
        (
            "set_group_admin",
            {"group_id": 987654, "user_id": 123456, "enable": False},
        ),
    ]
    assert channel.repository.get_daily_admin("987654") is None
    assert len(replies) == 1
    assert "管理员身份也已撤销" in replies[0]
    audit = channel.repository.get_latest_admin_ban_guard_event("987654")
    assert audit is not None
    assert audit.duration_seconds == 601
    assert audit.unmuted is True
    assert audit.admin_revoked is True


async def test_bot_automatic_mute_is_exempt_from_admin_ban_guard(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    channel._self_id = 999999
    actions: list[tuple[str, dict]] = []

    async def action(name: str, params: dict):
        actions.append((name, params))
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(channel, "_call_action", action)
    try:
        await channel._on_notice(
            {
                "notice_type": "group_ban",
                "sub_type": "ban",
                "group_id": 987654,
                "operator_id": 999999,
                "user_id": 222222,
                "duration": 1200,
            }
        )
    finally:
        await channel.stop()

    assert actions == []
    assert channel.repository.get_latest_admin_ban_guard_event("987654") is None


async def test_non_admin_mute_notice_never_triggers_guard_actions(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    channel._self_id = 999999
    actions: list[tuple[str, dict]] = []

    async def action(name: str, params: dict):
        actions.append((name, params))
        role = "owner" if params["user_id"] == 999999 else "member"
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"user_id": params["user_id"], "role": role},
        }

    monkeypatch.setattr(channel, "_call_action", action)
    try:
        await channel._on_notice(
            {
                "notice_type": "group_ban",
                "sub_type": "ban",
                "group_id": 987654,
                "operator_id": 123456,
                "user_id": 222222,
                "duration": 3600,
            }
        )
    finally:
        await channel.stop()

    assert [name for name, _params in actions] == [
        "get_group_member_info",
        "get_group_member_info",
    ]
    assert channel.repository.get_latest_admin_ban_guard_event("987654") is None


async def test_channel_relay_sends_source_media_only_to_target(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["100000021"],
            "privateRepliesEnabled": False,
            "contentRelayEnabled": True,
            "contentRelaySourceGroups": ["100000025", "100000026"],
            "contentRelayTargetGroup": "100000021",
            "contentRelayCooldownSeconds": 0,
        },
        MessageBus(),
    )
    actions: list[tuple[str, dict]] = []

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 210,
                "user_id": 123456,
                "group_id": 100000025,
                "message": [
                    {"type": "at", "data": {"qq": "999"}},
                    {"type": "text", "data": {"text": "好图"}},
                    {"type": "image", "data": {"file": "meme.image"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert actions == [
        (
            "send_group_msg",
            {
                "group_id": 100000021,
                "message": [
                    {
                        "type": "text",
                        "data": {"text": "💩 今日份群友搬运\n好图"},
                    },
                    {"type": "image", "data": {"file": "meme.image"}},
                ],
            },
        )
    ]


async def test_channel_is_silent_in_source_other_groups_and_private_chat(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["100000021"],
            "privateRepliesEnabled": False,
            "contentRelayEnabled": True,
            "contentRelaySourceGroups": ["100000025", "100000026"],
            "contentRelayTargetGroup": "100000021",
        },
        MessageBus(),
    )
    replies: list[tuple[str, str, str]] = []

    async def capture(kind: str, target: str, content: str) -> None:
        replies.append((kind, target, content))

    monkeypatch.setattr(channel, "_reply", capture)
    try:
        for message_type, group_id in (
            ("group", 100000025),
            ("group", 555555555),
            ("private", None),
        ):
            event = {
                "message_type": message_type,
                "message_id": f"silent-{message_type}-{group_id}",
                "user_id": 123456,
                "message": [{"type": "text", "data": {"text": "#帮助"}}],
            }
            if group_id is not None:
                event["group_id"] = group_id
            await channel._on_message(event)
    finally:
        await channel.stop()

    assert replies == []


async def test_channel_output_guard_blocks_other_groups_and_private_chat(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["100000021"],
            "privateRepliesEnabled": False,
        },
        MessageBus(),
    )
    outbound = []

    async def capture(message) -> None:
        outbound.append(message)

    monkeypatch.setattr(channel, "send", capture)
    try:
        with pytest.raises(RuntimeError, match="outbound QQ group is not allowed"):
            await channel._reply("group", "100000025", "不应发送")
        await channel._reply("private", "123456", "也不应发送")
    finally:
        await channel.stop()

    assert outbound == []


async def test_channel_reminds_then_mutes_unbound_member_at_daily_limit(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "unboundGuardEnabled": True,
            "unboundDailyMessageLimit": 10,
            "unboundMuteDurationSeconds": 600,
        },
        MessageBus(),
    )
    replies: list[str] = []
    actions: list[tuple[str, dict]] = []

    async def capture(_kind: str, _target: str, content: str) -> None:
        replies.append(content)

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(channel, "_reply", capture)
    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        for index in range(10):
            await channel._on_message(
                {
                    "message_type": "group",
                    "message_id": 210 + index,
                    "user_id": 123456,
                    "group_id": 987654,
                    "sender": {"card": "未绑定玩家", "role": "member"},
                    "message": [{"type": "text", "data": {"text": "普通群消息"}}],
                }
            )
    finally:
        await channel.stop()

    assert len(replies) == 4
    assert "剩余 9 条" in replies[0]
    assert "剩余 5 条" in replies[1]
    assert "剩余 1 条" in replies[2]
    assert "已达到今日 10 条消息限额，现禁言 10 分钟" in replies[3]
    assert actions == [
        (
            "set_group_ban",
            {"group_id": 987654, "user_id": 123456, "duration": 600},
        )
    ]


async def test_channel_reports_mute_permission_failure_without_claiming_success(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "unboundGuardEnabled": True,
            "unboundDailyMessageLimit": 1,
            "unboundMuteDurationSeconds": 60,
        },
        MessageBus(),
    )
    replies: list[str] = []

    async def capture(_kind: str, _target: str, content: str) -> None:
        replies.append(content)

    async def failed_action(_action: str, _params: dict) -> dict:
        raise RuntimeError("permission denied")

    monkeypatch.setattr(channel, "_reply", capture)
    monkeypatch.setattr(channel, "_call_action", failed_action)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 230,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"nickname": "未绑定玩家", "role": "member"},
                "message": [{"type": "text", "data": {"text": "普通群消息"}}],
            }
        )
    finally:
        await channel.stop()

    assert len(replies) == 1
    assert "机器人禁言失败" in replies[0]
    assert "现禁言" not in replies[0]


async def test_channel_exempts_admin_and_binding_help_commands_from_guard(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "unboundGuardEnabled": True,
        },
        MessageBus(),
    )
    replies: list[str] = []

    async def capture(_kind: str, _target: str, content: str) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 231,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"role": "admin"},
                "message": [{"type": "text", "data": {"text": "普通群消息"}}],
            }
        )
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 232,
                "user_id": 123457,
                "group_id": 987654,
                "sender": {"role": "member"},
                "message": [{"type": "text", "data": {"text": "#帮助"}}],
            }
        )
    finally:
        await channel.stop()

    assert len(replies) == 1
    assert replies[0].startswith("CS2 战绩机器人命令")
    assert channel.repository.get_unbound_message_count("987654", "123456", "2026-07-02") == 0
    assert channel.repository.get_unbound_message_count("987654", "123457", "2026-07-02") == 0


async def test_channel_queries_member_from_onebot_at_segment(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "queryCooldownSeconds": 0,
            "statsImageEnabled": False,
        },
        MessageBus(),
    )
    channel.repository.bind("987654", "222222", "76561198929215155")
    replies: list[str] = []
    queries: list[str] = []

    async def fake_detail_stats(steam_id: str) -> dict[str, object]:
        queries.append(steam_id)
        return {
            "name": "Mentioned Player",
            "steamId": steam_id,
            "kills": 20,
            "deaths": 10,
        }

    async def capture(_kind: str, _target: str, content: str) -> None:
        replies.append(content)

    monkeypatch.setattr(channel.perfectworld, "detail_stats", fake_detail_stats)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 300,
                "user_id": 123456,
                "group_id": 987654,
                "message": [
                    {"type": "text", "data": {"text": "#数据 "}},
                    {"type": "at", "data": {"qq": "222222"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert queries == ["76561198929215155"]
    assert len(replies) == 1
    assert "CS2 完美数据｜Mentioned Player" in replies[0]


async def test_channel_sends_stats_image_for_bound_member(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "queryCooldownSeconds": 0,
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    channel.repository.bind("987654", "222222", "76561198929215155")
    channel.community_repository.add_coins(
        "987654",
        "222222",
        300,
        reason="test",
        reference_key="stats-background-credit",
    )
    channel.group_play.buy("987654", "222222", "8")
    channel.group_play.buy("987654", "222222", "1")
    expected_badge = channel.community_repository.equipped_badge("987654", "222222")
    image_path = tmp_path / "stats.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_detail_stats(steam_id: str) -> dict[str, object]:
        return {"name": "Other Player", "steamId": steam_id}

    async def fake_render(
        _stats,
        *,
        background_key: str = "classic",
        member_badge: str = "",
    ) -> Path:
        assert background_key == "cyber_neon"
        assert member_badge == expected_badge
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        assert image_path.exists()
        replies.append((content, media))

    monkeypatch.setattr(channel.perfectworld, "detail_stats", fake_detail_stats)
    monkeypatch.setattr(channel.stats_renderer, "render", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 301,
                "user_id": 123456,
                "group_id": 987654,
                "message": [
                    {"type": "text", "data": {"text": "#数据 "}},
                    {"type": "at", "data": {"qq": "222222"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()
    memory = next(
        item
        for item in channel.repository.list_group_member_memories("987654")
        if item.qq_id == "222222"
    )
    assert memory.stats["season_stats"]["player_name"] == "Other Player"


async def test_channel_archives_replied_text_and_passes_admin_role(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "aiEnabled": False,
        },
        MessageBus(),
    )
    replies: list[str] = []

    async def capture(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    async def fake_action(action: str, params: dict) -> dict:
        assert action == "get_msg"
        assert params == {"message_id": 77}
        return {
            "status": "ok",
            "data": {
                "group_id": 987654,
                "user_id": 10002,
                "sender": {"user_id": 10002, "nickname": "精灵"},
                "message": [{"type": "text", "data": {"text": "我绝对不会白给"}}],
            },
        }

    monkeypatch.setattr(channel, "_reply", capture)
    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 88,
                "user_id": 10001,
                "group_id": 987654,
                "sender": {"nickname": "牛牛", "role": "admin"},
                "message": [
                    {"type": "reply", "data": {"id": "77"}},
                    {"type": "text", "data": {"text": "#收录黑历史 经典"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert len(replies) == 1
    assert "黑历史 #1" in replies[0]
    assert "我绝对不会白给" in replies[0]


async def test_channel_sends_recent_match_history_image(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "queryCooldownSeconds": 0,
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    channel.repository.bind("987654", "222222", "76561198929215155")
    channel.community_repository.add_coins(
        "987654",
        "222222",
        300,
        reason="test",
        reference_key="history-background-credit",
    )
    channel.group_play.buy("987654", "222222", "9")
    channel.group_play.buy("987654", "222222", "1")
    expected_badge = channel.community_repository.equipped_badge("987654", "222222")
    image_path = tmp_path / "history.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_match_list(_steam_id: str, *, page_size: int) -> list[dict]:
        assert page_size == 10
        return [
            {
                "matchId": "PVP@recent",
                "timeStamp": 1,
                "mapName": "荒漠迷城",
                "team": 1,
                "winTeam": 1,
                "score1": 13,
                "score2": 8,
                "kill": 20,
                "assist": 5,
                "death": 10,
                "pwRating": 1.2,
            }
        ]

    async def fake_detail_stats(steam_id: str) -> dict[str, object]:
        return {"name": "Recent Player", "steamId": steam_id}

    async def fake_render(
        history,
        *,
        background_key: str = "classic",
        member_badge: str = "",
    ) -> Path:
        assert history.name == "Recent Player"
        assert background_key == "desert_sunset"
        assert member_badge == expected_badge
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))

    monkeypatch.setattr(channel.perfectworld, "match_list", fake_match_list)
    monkeypatch.setattr(channel.perfectworld, "detail_stats", fake_detail_stats)
    monkeypatch.setattr(channel.stats_renderer, "render_match_history", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 304,
                "user_id": 123456,
                "group_id": 987654,
                "message": [
                    {"type": "text", "data": {"text": "#战绩 "}},
                    {"type": "at", "data": {"qq": "222222"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()


async def test_channel_removes_binding_on_group_decrease_notice(tmp_path: Path) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    channel.repository.bind("987654", "222222", "76561198929215155")
    channel.repository.bind_valorant("987654", "222222", "ExamplePlayer#12345")
    channel.repository.upsert_group_member_identity("987654", "222222", "Leaving")
    channel.repository.add_group_ai_memory(
        "987654",
        "退群后应立即删除",
        created_by_qq="222222",
        created_by_nickname="Leaving",
    )
    channel.repository.add_group_ai_memory(
        "other-group",
        "其他群的数据不能受影响",
        created_by_qq="222222",
        created_by_nickname="Leaving",
    )
    channel.repository.reconcile_group_member_compliance(
        "987654",
        {"222222": (UNBOUND_REASON,)},
        check_date="2026-07-12",
    )
    try:
        await channel._on_notice(
            {
                "notice_type": "group_decrease",
                "group_id": 987654,
                "user_id": 222222,
                "sub_type": "leave",
            }
        )
    finally:
        await channel.stop()

    assert channel.repository.get_by_qq("987654", "222222") is None
    assert channel.repository.get_valorant_by_qq("987654", "222222") is None
    assert channel.repository.list_group_member_memories("987654") == ()
    assert channel.repository.list_group_ai_memories("987654") == ()
    assert len(channel.repository.list_group_ai_memories("other-group")) == 1
    assert (
        channel.repository.get_group_member_compliance("987654", "222222") is None
    )


async def test_channel_fetches_authoritative_group_member_ids(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    actions: list[tuple[str, dict]] = []

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        return {"data": [{"user_id": 100001}, {"user_id": "100002"}]}

    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        members = await channel._fetch_group_member_ids("987654")
    finally:
        await channel.stop()

    assert members == {"100001", "100002"}
    assert actions == [
        ("get_group_member_list", {"group_id": 987654, "no_cache": True})
    ]
    memories = channel.repository.list_group_member_memories("987654")
    assert [memory.qq_id for memory in memories] == ["100001", "100002"]


async def test_channel_sends_group_level_reminder_as_one_mention_message(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    channel._self_id = 999
    actions: list[tuple[str, dict]] = []

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        return {"status": "ok", "retcode": 0}

    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        await channel._send_group_level_reminder(
            "987654",
            (
                ComplianceReminder(
                    user_id="100",
                    reasons=(UNBOUND_REASON,),
                    remaining_days=14,
                ),
                ComplianceReminder(
                    user_id="999",
                    reasons=(LEVEL_REASON,),
                    remaining_days=14,
                ),
                ComplianceReminder(
                    user_id="300",
                    reasons=(LEVEL_REASON, UNBOUND_REASON),
                    remaining_days=0,
                ),
            ),
        )
    finally:
        await channel.stop()

    assert actions == [
        (
            "send_group_msg",
            {
                "group_id": 987654,
                "message": [
                    {"type": "at", "data": {"qq": "100"}},
                    {
                        "type": "text",
                        "data": {"text": "（未绑定 Steam；剩 14 天）\n"},
                    },
                    {"type": "at", "data": {"qq": "300"}},
                    {
                        "type": "text",
                        "data": {
                            "text": (
                                "（未绑定 Steam、群等级为 1；"
                                "已到期，等待机器人重试移出）\n"
                            )
                        },
                    },
                    {
                        "type": "text",
                        "data": {
                            "text": (
                                "请在倒计时结束前发送 #绑定 <SteamID64> 完成绑定，"
                                "并将群等级升至 2 以上。任一条件连续未满足满 "
                                "15 天将自动移出本群。"
                            )
                        },
                    },
                ],
            },
        )
    ]


async def test_channel_refuses_compliance_removal_without_bot_permission(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    channel._self_id = 999
    actions: list[tuple[str, dict]] = []

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        return {"data": {"user_id": 999, "role": "member"}}

    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        resolved = await channel._remove_noncompliant_members("987654", ("100",))
    finally:
        await channel.stop()

    assert resolved == ()
    assert actions == [
        (
            "get_group_member_info",
            {
                "group_id": 987654,
                "user_id": 999,
                "no_cache": True,
            },
        )
    ]


async def test_channel_rechecks_member_and_skips_member_who_became_compliant(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    channel._self_id = 999
    channel.repository.bind("987654", "100", "76561198929215155")
    actions: list[tuple[str, dict]] = []

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        if params["user_id"] == 999:
            return {"data": {"user_id": 999, "role": "admin"}}
        return {
            "data": {
                "user_id": 100,
                "role": "member",
                "level": "2",
                "is_robot": False,
            }
        }

    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        resolved = await channel._remove_noncompliant_members("987654", ("100",))
    finally:
        await channel.stop()

    assert resolved == ("100",)
    assert actions == [
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 999, "no_cache": True},
        ),
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 100, "no_cache": True},
        ),
    ]


async def test_channel_rechecks_member_and_kicks_with_safe_onebot_parameters(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    channel._self_id = 999
    actions: list[tuple[str, dict]] = []

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        if action == "set_group_kick":
            return {"status": "ok", "retcode": 0, "data": {}}
        if params["user_id"] == 999:
            return {"data": {"user_id": 999, "role": "admin"}}
        return {
            "data": {
                "user_id": 100,
                "role": "member",
                "level": "2",
                "is_robot": False,
            }
        }

    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        resolved = await channel._remove_noncompliant_members("987654", ("100",))
    finally:
        await channel.stop()

    assert resolved == ("100",)
    assert actions == [
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 999, "no_cache": True},
        ),
        (
            "get_group_member_info",
            {"group_id": 987654, "user_id": 100, "no_cache": True},
        ),
        (
            "set_group_kick",
            {
                "group_id": 987654,
                "user_id": 100,
                "reject_add_request": False,
            },
        ),
    ]


@pytest.mark.parametrize(
    "member",
    [
        {"user_id": 101, "role": "member", "level": "1", "is_robot": False},
        {"user_id": 100, "level": "1", "is_robot": False},
        {"user_id": 100, "role": "unknown", "level": "1", "is_robot": False},
        {"user_id": 100, "role": "member", "level": "1"},
        {"user_id": 100, "role": "member", "level": "?", "is_robot": False},
    ],
)
async def test_channel_fails_closed_on_incomplete_or_mismatched_member_data(
    tmp_path: Path,
    monkeypatch,
    member: dict,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
        },
        MessageBus(),
    )
    channel._self_id = 999
    actions: list[tuple[str, dict]] = []

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        if params["user_id"] == 999:
            return {"data": {"user_id": 999, "role": "admin"}}
        return {"data": member}

    monkeypatch.setattr(channel, "_call_action", fake_action)
    try:
        resolved = await channel._remove_noncompliant_members("987654", ("100",))
    finally:
        await channel.stop()

    assert resolved == ()
    assert [action for action, _params in actions] == [
        "get_group_member_info",
        "get_group_member_info",
    ]


async def test_channel_sends_group_ranking_image(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "queryCooldownSeconds": 0,
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    channel.repository.bind("987654", "222222", "76561198929215155")
    image_path = tmp_path / "ranking.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_detail_stats_v2(steam_id: str) -> dict[str, object]:
        return {
            "name": "Ranked Player",
            "steamId": steam_id,
            "pvpScore": 2000,
            "radar": {
                "marksmanship": {"detail": {"rapid_stop_rate_raw": 0.83}}
            },
        }

    async def fake_render(_ranking) -> Path:
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))

    monkeypatch.setattr(channel.perfectworld, "detail_stats_v2", fake_detail_stats_v2)
    monkeypatch.setattr(channel.stats_renderer, "render_ranking", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 303,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "#排行 急停"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()


async def test_channel_sends_and_removes_game_status_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    snapshot = GameStatusSnapshot(
        entries=(
            GameStatusEntry(
                name="牛牛",
                steam_id="76561198929215155",
                status_label="正在游戏",
                status_kind="playing",
                game_name="Counter-Strike 2",
                app_id="730",
            ),
        ),
        total_count=3,
        playing_count=1,
        hidden_count=2,
    )
    image_path = tmp_path / "game-status.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_handle_result(*_args, **_kwargs) -> CommandResult:
        return CommandResult("文字降级内容", game_status=snapshot)

    async def fake_render(received: GameStatusSnapshot) -> Path:
        assert received is snapshot
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))

    monkeypatch.setattr(channel.commands, "handle_result", fake_handle_result)
    monkeypatch.setattr(channel.stats_renderer, "render_game_status", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 304,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "#游戏状态"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()


async def test_channel_sends_and_removes_valorant_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    snapshot = ValorantSnapshot(
        riot_id="ExamplePlayer#12345",
        rank="黄金 1",
        record="9胜1负",
        win_rate="90%",
        kda="1.22",
        acs="222",
        rr_change="+174",
        source_updated_at="",
        fetched_at="2026-07-31T12:00:00+00:00",
        matches=(),
    )
    image_path = tmp_path / "valorant.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_handle_result(*_args, **_kwargs) -> CommandResult:
        return CommandResult("文字降级内容", valorant=snapshot)

    async def fake_render(received: ValorantSnapshot) -> Path:
        assert received is snapshot
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))

    monkeypatch.setattr(channel.commands, "handle_result", fake_handle_result)
    monkeypatch.setattr(channel.stats_renderer, "render_valorant", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 307,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "#瓦战绩"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()


async def test_channel_game_status_image_failure_falls_back_to_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    snapshot = GameStatusSnapshot(
        entries=(),
        total_count=3,
        playing_count=0,
        hidden_count=3,
        note="当前没人公开显示正在游戏",
    )
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_handle_result(*_args, **_kwargs) -> CommandResult:
        return CommandResult("文字降级内容", game_status=snapshot)

    async def fake_render(_snapshot: GameStatusSnapshot) -> Path:
        raise StatsCardRenderError("测试渲染失败")

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))

    monkeypatch.setattr(channel.commands, "handle_result", fake_handle_result)
    monkeypatch.setattr(channel.stats_renderer, "render_game_status", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 305,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "#游戏状态"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == [("文字降级内容", None)]


async def test_channel_game_status_send_failure_removes_image_and_retries_text(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    snapshot = GameStatusSnapshot(
        entries=(),
        total_count=3,
        playing_count=0,
        hidden_count=3,
    )
    image_path = tmp_path / "game-status-send-failure.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_handle_result(*_args, **_kwargs) -> CommandResult:
        return CommandResult("文字降级内容", game_status=snapshot)

    async def fake_render(_snapshot: GameStatusSnapshot) -> Path:
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))
        if media is not None:
            raise RuntimeError("测试图片发送失败")

    monkeypatch.setattr(channel.commands, "handle_result", fake_handle_result)
    monkeypatch.setattr(channel.stats_renderer, "render_game_status", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 306,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "#游戏状态"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == [
        ("", [str(image_path)]),
        ("文字降级内容", None),
    ]
    assert not image_path.exists()


async def test_channel_ai_ignores_normal_group_chat_and_answers_when_mentioned(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    replies: list[tuple[str, str, str]] = []
    prompts: list[dict] = []

    async def fake_chat(**kwargs) -> str:
        prompts.append(kwargs)
        return "AI 回答"

    async def capture(kind: str, target: str, content: str) -> None:
        replies.append((kind, target, content))

    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", capture)
    base_event = {
        "message_type": "group",
        "user_id": 123456,
        "group_id": 987654,
        "sender": {"card": "群友"},
    }
    try:
        await channel._on_message(
            {
                **base_event,
                "message_id": 400,
                "message": [{"type": "text", "data": {"text": "普通群消息"}}],
            }
        )
        await channel._on_message(
            {
                **base_event,
                "message_id": 401,
                "message": [
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {"type": "text", "data": {"text": " 你好"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert replies == [("group", "987654", "群友：\nAI 回答")]
    assert len(prompts) == 1
    assert {
        key: value for key, value in prompts[0].items() if key not in {"memory_context", "toolbox"}
    } == {
        "chat_id": "group:987654",
        "requester_id": "123456",
        "content": "你好",
        "speaker": "群友",
    }
    assert prompts[0]["memory_context"] == ""
    assert prompts[0]["toolbox"].group_id == "987654"
    assert prompts[0]["toolbox"].requester_id == "123456"


async def test_channel_can_disable_ai_tools_without_disabling_chat(tmp_path, monkeypatch):
    channel = PWCS2Channel(
        {"enabled": True, "allowFrom": ["*"], "pwToken": "test",
         "databasePath": str(tmp_path / "bindings.db"), "aiApiKey": "sk-test",
         "aiDataToolsEnabled": False}, MessageBus(),
    )
    captured = []
    refreshed = []

    async def chat(**kwargs):
        captured.append(kwargs)
        return "answer"

    async def memory(*args, **kwargs):
        refreshed.append(args)
        return "old memory"

    async def discard(*args, **kwargs):
        pass

    monkeypatch.setattr(channel.ai, "chat", chat)
    monkeypatch.setattr(channel.member_memory, "build_smart_ai_context", memory)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message({"message_type": "group", "user_id": 123456,
                                   "group_id": 987654, "message_id": 991,
                                   "message": [{"type": "text", "data": {"text": "#ai 测试"}}]})
    finally:
        await channel.stop()
    assert captured and "toolbox" not in captured[0]
    assert refreshed


async def test_channel_tools_enabled_avoids_redundant_heuristic_refresh(tmp_path, monkeypatch):
    channel = PWCS2Channel(
        {"enabled": True, "allowFrom": ["*"], "pwToken": "test",
         "databasePath": str(tmp_path / "bindings.db"), "aiApiKey": "sk-test"}, MessageBus(),
    )
    captured = []

    async def chat(**kwargs):
        captured.append(kwargs)
        return "answer"

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not prefetch stats before tool selection")

    async def discard(*args, **kwargs):
        pass

    monkeypatch.setattr(channel.ai, "chat", chat)
    monkeypatch.setattr(channel.member_memory, "build_smart_ai_context", forbidden)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message({"message_type": "group", "user_id": 123456,
                                   "group_id": 987654, "message_id": 992,
                                   "message": [{"type": "text", "data": {"text": "#ai 我的战绩"}}]})
    finally:
        await channel.stop()
    assert captured[0]["toolbox"].requester_id == "123456"


async def test_private_ai_never_receives_group_statistics_toolbox(tmp_path, monkeypatch):
    channel = PWCS2Channel(
        {"enabled": True, "allowFrom": ["*"], "pwToken": "test",
         "databasePath": str(tmp_path / "bindings.db"), "aiApiKey": "sk-test",
         "privateRepliesEnabled": True}, MessageBus(),
    )
    captured = []

    async def chat(**kwargs):
        captured.append(kwargs)
        return "answer"

    async def discard(*args, **kwargs):
        pass

    monkeypatch.setattr(channel.ai, "chat", chat)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message({"message_type": "private", "user_id": 123456,
                                   "message_id": 993,
                                   "message": [{"type": "text", "data": {"text": "查群数据"}}]})
    finally:
        await channel.stop()
    assert captured and "toolbox" not in captured[0]
    assert captured[0]["memory_context"] == ""


async def test_channel_supports_explicit_ai_and_context_reset(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    replies: list[str] = []

    async def fake_chat(**_kwargs) -> str:
        return "显式 AI 回答"

    async def capture(_kind: str, _target: str, content: str) -> None:
        replies.append(content)

    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 402,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "#ai 测试"}}],
            }
        )
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 403,
                "user_id": 123456,
                "group_id": 987654,
                "message": [{"type": "text", "data": {"text": "#ai 清空"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == ["123456：\n显式 AI 回答", "AI 对话上下文已清空"]


async def test_channel_ai_memory_includes_mentioned_group_member(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    channel.member_memory.remember_identity("987654", "222222", "ExamplePlayer")
    captured: list[dict] = []

    async def fake_chat(**kwargs) -> str:
        captured.append(kwargs)
        return "已读取"

    async def discard(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 4031,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "提问者"},
                "message": [
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {"type": "text", "data": {"text": " 看看 "}},
                    {"type": "at", "data": {"qq": "222222"}},
                    {"type": "text", "data": {"text": " 的战绩"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert len(captured) == 1
    assert '"qq_id":"123456"' not in captured[0]["memory_context"]
    assert '"qq_id":"222222"' in captured[0]["memory_context"]
    assert '"nickname":"ExamplePlayer"' in captured[0]["memory_context"]


async def test_channel_injects_saved_group_long_term_memory_into_ai(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    captured: list[dict] = []
    replies: list[str] = []

    async def fake_chat(**kwargs) -> str:
        captured.append(kwargs)
        return "记得，周五晚上十点。"

    async def capture(_kind: str, _target: str, content: str, **_kwargs) -> None:
        replies.append(content)

    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 4032,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "Alice", "role": "member"},
                "message": [
                    {"type": "text", "data": {"text": "#记住 群里每周五晚上十点开黑"}}
                ],
            }
        )
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 4033,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "Alice", "role": "member"},
                "message": [
                    {"type": "text", "data": {"text": "#ai 清空"}},
                ],
            }
        )
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 4034,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "Alice", "role": "member"},
                "message": [
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {"type": "text", "data": {"text": " 群里什么时候开黑？"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert len(captured) == 1
    assert captured[0]["chat_id"] == "group:987654"
    assert "群里每周五晚上十点开黑" in captured[0]["memory_context"]
    assert '"created_by_qq":"123456"' in captured[0]["memory_context"]
    assert '"created_by_nickname":"Alice"' in captured[0]["memory_context"]
    assert "未经验证" in captured[0]["memory_context"]
    assert replies[0].startswith("🧠 已加入本群 AI 长期记忆 #")
    assert replies[1] == "AI 对话上下文已清空"
    assert replies[-1] == "Alice：\n记得，周五晚上十点。"


async def test_channel_long_term_memory_keeps_target_cs_stats_first(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "allowedGroups": ["987654"],
            "aiApiKey": "sk-test-secret",
            "aiMaxInputChars": 4000,
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    channel.repository.upsert_group_member_identity("987654", "100000011", "示例群友")
    channel.repository.bind("987654", "100000011", "76561197960266101")
    channel.repository.update_group_member_stats(
        "987654",
        "100000011",
        {
            "season_stats": {
                "source": "perfect",
                "player_name": "示例玩家",
                "elo": "1950",
                "rating": "1.07",
            }
        },
    )
    for index in range(20):
        channel.repository.add_group_ai_memory(
            "987654",
            f"群梗 {index} " + "记" * 170,
            created_by_qq=str(100000 + index % 4),
            created_by_nickname=f"成员{index % 4}",
        )
    captured: list[dict] = []

    async def fake_chat(**kwargs) -> str:
        captured.append(kwargs)
        return "已读取"

    async def discard(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 4035,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "Alice", "role": "member"},
                "message": [
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {"type": "text", "data": {"text": " 看看 "}},
                    {"type": "at", "data": {"qq": "100000011"}},
                    {"type": "text", "data": {"text": " 的战绩"}},
                ],
            }
        )
    finally:
        await channel.stop()

    memory_context = captured[0]["memory_context"]
    assert '"qq_id":"100000011"' in memory_context
    assert '"player_name":"示例玩家"' in memory_context
    assert "member_authored_long_term_memories" in memory_context
    assert memory_context.index("结构化成员记忆") < memory_context.index("长期记忆声明")
    assert len(memory_context) < channel.config.ai_max_input_chars


async def test_channel_passes_qq_images_to_ai(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    captured: list[dict] = []
    replies: list[str] = []

    async def fake_collect(_segments) -> tuple[str, ...]:
        return ("data:image/png;base64,aGVsbG8=",)

    async def fake_chat(**kwargs) -> str:
        captured.append(kwargs)
        return "这是一张测试图"

    async def capture(_kind: str, _target: str, content: str) -> None:
        replies.append(content)

    monkeypatch.setattr(channel, "_collect_ai_image_data_urls", fake_collect)
    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 404,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "群友"},
                "message": [
                    {"type": "text", "data": {"text": "#ai 看图"}},
                    {"type": "image", "data": {"url": "https://example.test/a.png"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert captured[0]["content"] == "看图"
    assert captured[0]["image_data_urls"] == ("data:image/png;base64,aGVsbG8=",)
    assert replies == ["群友：\n这是一张测试图"]


async def test_channel_passes_replied_image_to_ai_when_bot_is_mentioned(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    actions: list[tuple[str, dict]] = []
    captured: list[dict] = []

    async def fake_action(action: str, params: dict) -> dict:
        actions.append((action, params))
        return {
            "status": "ok",
            "data": {
                "group_id": 987654,
                "message": [
                    {
                        "type": "image",
                        "data": {"url": "https://example.test/replied.png"},
                    }
                ],
            },
        }

    async def fake_read(source: str) -> str:
        assert source == "https://example.test/replied.png"
        return "data:image/png;base64,cmVwbGllZA=="

    async def fake_chat(**kwargs) -> str:
        captured.append(kwargs)
        return "看到了"

    async def discard(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(channel, "_call_action", fake_action)
    monkeypatch.setattr(channel, "_read_ai_image_data_url", fake_read)
    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 405,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "群友"},
                "message": [
                    {"type": "reply", "data": {"id": "77"}},
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {"type": "text", "data": {"text": " 这张图是什么"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert actions == [("get_msg", {"message_id": 77})]
    assert len(captured) == 1
    assert captured[0]["content"] == "这张图是什么"
    assert captured[0]["image_data_urls"] == (
        "data:image/png;base64,cmVwbGllZA==",
    )


async def test_channel_rejects_replied_image_from_another_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    read_sources: list[str] = []
    captured: list[dict] = []

    async def fake_action(_action: str, _params: dict) -> dict:
        return {
            "status": "ok",
            "data": {
                "group_id": 111111,
                "message": [
                    {
                        "type": "image",
                        "data": {"url": "https://example.test/foreign.png"},
                    }
                ],
            },
        }

    async def fake_read(source: str) -> str:
        read_sources.append(source)
        return "data:image/png;base64,Zm9yZWlnbg=="

    async def fake_chat(**kwargs) -> str:
        captured.append(kwargs)
        return "文字回答"

    async def discard(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(channel, "_call_action", fake_action)
    monkeypatch.setattr(channel, "_read_ai_image_data_url", fake_read)
    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 406,
                "user_id": 123456,
                "group_id": 987654,
                "message": [
                    {"type": "reply", "data": {"id": "78"}},
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {"type": "text", "data": {"text": " 看看引用"}},
                ],
            }
        )
    finally:
        await channel.stop()

    assert read_sources == []
    assert len(captured) == 1
    assert "image_data_urls" not in captured[0]


async def test_channel_keeps_current_image_when_replied_message_fetch_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    captured: list[dict] = []

    async def fake_action(_action: str, _params: dict) -> dict:
        raise RuntimeError("temporary get_msg failure")

    async def fake_read(source: str) -> str:
        assert source == "https://example.test/current.png"
        return "data:image/png;base64,Y3VycmVudA=="

    async def fake_chat(**kwargs) -> str:
        captured.append(kwargs)
        return "仍然看到了当前图片"

    async def discard(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(channel, "_call_action", fake_action)
    monkeypatch.setattr(channel, "_read_ai_image_data_url", fake_read)
    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 407,
                "user_id": 123456,
                "group_id": 987654,
                "message": [
                    {"type": "reply", "data": {"id": "79"}},
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {"type": "text", "data": {"text": " 看这两张图"}},
                    {
                        "type": "image",
                        "data": {"url": "https://example.test/current.png"},
                    },
                ],
            }
        )
    finally:
        await channel.stop()

    assert len(captured) == 1
    assert captured[0]["image_data_urls"] == (
        "data:image/png;base64,Y3VycmVudA==",
    )


async def test_channel_caps_current_and_replied_images_together(
    tmp_path: Path,
    monkeypatch,
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test-secret",
            "aiMaxImages": 2,
        },
        MessageBus(),
    )
    channel._self_id = "3875475562"
    read_sources: list[str] = []
    captured: list[dict] = []

    async def fake_action(_action: str, _params: dict) -> dict:
        return {
            "status": "ok",
            "data": {
                "group_id": 987654,
                "message": [
                    {
                        "type": "image",
                        "data": {"url": "https://example.test/replied-1.png"},
                    },
                    {
                        "type": "image",
                        "data": {"url": "https://example.test/replied-2.png"},
                    },
                ],
            },
        }

    image_data = {
        "https://example.test/current.png": "data:image/png;base64,Y3VycmVudA==",
        "https://example.test/replied-1.png": "data:image/png;base64,cmVwbGllZDE=",
        "https://example.test/replied-2.png": "data:image/png;base64,cmVwbGllZDI=",
    }

    async def fake_read(source: str) -> str:
        read_sources.append(source)
        return image_data[source]

    async def fake_chat(**kwargs) -> str:
        captured.append(kwargs)
        return "比较完成"

    async def discard(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(channel, "_call_action", fake_action)
    monkeypatch.setattr(channel, "_read_ai_image_data_url", fake_read)
    monkeypatch.setattr(channel.ai, "chat", fake_chat)
    monkeypatch.setattr(channel, "_reply", discard)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 408,
                "user_id": 123456,
                "group_id": 987654,
                "message": [
                    {"type": "reply", "data": {"id": "80"}},
                    {"type": "at", "data": {"qq": "3875475562"}},
                    {"type": "text", "data": {"text": " 比较一下"}},
                    {
                        "type": "image",
                        "data": {"url": "https://example.test/current.png"},
                    },
                ],
            }
        )
    finally:
        await channel.stop()

    assert read_sources == [
        "https://example.test/current.png",
        "https://example.test/replied-1.png",
    ]
    assert len(captured) == 1
    assert captured[0]["image_data_urls"] == (
        "data:image/png;base64,Y3VycmVudA==",
        "data:image/png;base64,cmVwbGllZDE=",
    )


async def test_channel_falls_back_to_text_when_card_rendering_fails(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "queryCooldownSeconds": 0,
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_detail_stats(steam_id: str) -> dict[str, object]:
        return {"name": "Fallback Player", "steamId": steam_id}

    async def failed_render(_stats) -> Path:
        raise StatsCardRenderError("test failure")

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))

    monkeypatch.setattr(channel.perfectworld, "detail_stats", fake_detail_stats)
    monkeypatch.setattr(channel.stats_renderer, "render", failed_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 302,
                "user_id": 123456,
                "group_id": 987654,
                "message": [
                    {
                        "type": "text",
                        "data": {"text": "#数据 76561198929215155"},
                    }
                ],
            }
        )
    finally:
        await channel.stop()

    assert len(replies) == 1
    assert replies[0][1] is None
    assert replies[0][0].startswith("CS2 完美数据｜Fallback Player")


async def test_channel_sends_and_removes_match_notification_image(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    image_path = tmp_path / "match.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_render(_match, *, commentary: str | None = None) -> Path:
        assert commentary is None
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        assert image_path.exists()
        replies.append((content, media))

    match = build_match_snapshot(
        {"matchId": "new", "team": 1, "winTeam": 1, "score1": 13, "score2": 8},
        steam_id="76561198929215155",
        profile={"name": "Player"},
    )
    monkeypatch.setattr(channel.stats_renderer, "render_match", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._send_match_notification("987654", match)
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()


async def test_channel_sends_and_removes_watch_stage_image(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "queryCooldownSeconds": 0,
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    steam_id = "76561198929215155"
    channel.repository.bind("987654", "123456", steam_id)
    image_path = tmp_path / "watch-stage.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []
    snapshot = WatchStageSnapshot(
        status="running",
        requested_steam_id=steam_id,
        requested_name="",
        platform="2",
        match_id="live-match",
        map_name="de_mirage",
        ct_score=7,
        terrorist_score=5,
        duration="12:34",
        warmup=False,
        updated_at="2026-07-27T00:00:00+00:00",
        players=(),
    )

    async def fake_snapshot(
        _steam_id: str,
        *,
        platform: str | None = None,
    ) -> WatchStageSnapshot:
        assert platform is None
        return snapshot

    async def fake_render(_snapshot: WatchStageSnapshot) -> Path:
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        assert image_path.exists()
        replies.append((content, media))

    monkeypatch.setattr(channel.watch_stage, "snapshot", fake_snapshot)
    monkeypatch.setattr(channel.stats_renderer, "render_watch_stage", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._on_message(
            {
                "message_type": "group",
                "message_id": 998,
                "user_id": 123456,
                "group_id": 987654,
                "sender": {"card": "牛牛", "role": "member"},
                "message": [{"type": "text", "data": {"text": "#观战"}}],
            }
        )
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()


async def test_channel_awards_match_coins_once(tmp_path: Path, monkeypatch) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": False,
        },
        MessageBus(),
    )
    steam_id = "76561198929215155"
    channel.repository.bind("987654", "10001", steam_id)
    channel.repository.upsert_group_member_identity("987654", "10001", "牛牛")
    replies: list[str] = []

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        assert media is None
        replies.append(content)

    match = build_match_snapshot(
        {
            "matchId": "reward-match",
            "endTime": "2026-07-26 20:00:00",
            "team": 1,
            "winTeam": 1,
            "score1": 13,
            "score2": 8,
        },
        steam_id=steam_id,
        profile={"name": "Player"},
    )
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._send_match_notification("987654", match)
        await channel._send_match_notification("987654", match)
    finally:
        await channel.stop()

    reward_messages = [message for message in replies if message.startswith("🪙 完赛奖励")]
    assert reward_messages == []
    assert len(replies) == 2
    assert channel.community_repository.wallet("987654", "10001")[0] == 110


async def test_channel_embeds_ai_commentary_in_match_image_without_extra_message(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    image_path = tmp_path / "commentary-match.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []
    rendered_commentary: list[str | None] = []

    async def fake_commentary(_group_id: str, _match) -> str:
        return "🐮 赛后 AI 锐评\n火力在线。"

    async def fake_render(_match, *, commentary: str | None = None) -> Path:
        rendered_commentary.append(commentary)
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))

    match = build_match_snapshot(
        {"matchId": "new", "team": 1, "winTeam": 1, "score1": 13, "score2": 8},
        steam_id="76561198929215155",
        profile={"name": "Player"},
        detail={
            "players": [
                {
                    "playerId": "76561198929215155",
                    "nickName": "Player",
                    "team": 1,
                    "kill": 20,
                    "assist": 5,
                    "death": 10,
                }
            ]
        },
    )
    monkeypatch.setattr(channel.match_commentary, "generate", fake_commentary)
    monkeypatch.setattr(channel.stats_renderer, "render_match", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._send_match_notification("987654", match)
    finally:
        await channel.stop()

    assert rendered_commentary == ["🐮 赛后 AI 锐评\n火力在线。"]
    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()


async def test_channel_omits_commentary_when_ai_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
            "aiApiKey": "sk-test-secret",
        },
        MessageBus(),
    )
    image_path = tmp_path / "plain-match.png"
    image_path.write_bytes(b"fake-png")
    rendered_commentary: list[str | None] = []
    replies: list[tuple[str, list[str] | None]] = []

    async def unavailable(_group_id: str, _match) -> str:
        raise AIChatError("temporary outage")

    async def fake_render(_match, *, commentary: str | None = None) -> Path:
        rendered_commentary.append(commentary)
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        replies.append((content, media))

    match = build_match_snapshot(
        {"matchId": "new", "team": 1, "winTeam": 1, "score1": 13, "score2": 8},
        steam_id="76561198929215155",
        detail={
            "players": [
                {
                    "playerId": "76561198929215155",
                    "nickName": "Player",
                    "team": 1,
                    "kill": 20,
                    "assist": 5,
                    "death": 10,
                }
            ]
        },
    )
    monkeypatch.setattr(channel.match_commentary, "generate", unavailable)
    monkeypatch.setattr(channel.stats_renderer, "render_match", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._send_match_notification("987654", match)
    finally:
        await channel.stop()

    assert rendered_commentary == [None]
    assert replies == [("", [str(image_path)])]


async def test_channel_sends_and_removes_skin_price_report_image(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    image_path = tmp_path / "skin-prices.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []

    async def fake_render(_report) -> Path:
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        assert image_path.exists()
        replies.append((content, media))

    report = SkinPriceReport(
        (SkinReportEntry(SkinQuote("AK-47 | Redline", 10, 9, 7.2)),),
        report_date="2026-07-06",
    )
    monkeypatch.setattr(channel.stats_renderer, "render_skin_prices", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._send_skin_price_report("987654", report)
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()


async def test_channel_sends_and_removes_weekly_chemistry_image(
    tmp_path: Path, monkeypatch
) -> None:
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "pwToken": "pw-secret",
            "databasePath": str(tmp_path / "bindings.db"),
            "statsImageEnabled": True,
        },
        MessageBus(),
    )
    image_path = tmp_path / "chemistry.png"
    image_path.write_bytes(b"fake-png")
    replies: list[tuple[str, list[str] | None]] = []
    shared = [{"matchId": "shared", "team": 1, "winTeam": 1}]
    snapshot = build_chemistry_network(
        {"10001": shared, "10002": shared},
        names_by_qq={"10001": "牛牛", "10002": "精灵"},
        week_start="2026-07-20",
    )

    async def fake_render(_snapshot) -> Path:
        return image_path

    async def capture(
        _kind: str,
        _target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        assert image_path.exists()
        replies.append((content, media))

    monkeypatch.setattr(channel.stats_renderer, "render_chemistry_network", fake_render)
    monkeypatch.setattr(channel, "_reply", capture)
    try:
        await channel._send_weekly_chemistry("987654", snapshot)
    finally:
        await channel.stop()

    assert replies == [("", [str(image_path)])]
    assert not image_path.exists()
