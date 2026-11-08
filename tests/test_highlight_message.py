import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nanobot.bus.queue import MessageBus

from pwcs2_bot.ai_chat import AIChatError
from pwcs2_bot.channel import PWCS2Channel, _highlight_caption
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.highlight_commentary import generate_highlight_commentary
from pwcs2_bot.highlight_delivery import HighlightVideoDelivery
from pwcs2_bot.highlight_protocol import HighlightProtocolError
from pwcs2_bot.match_formatting import build_match_snapshot

STEAM = "76561197960266101"
GROUP = "987654"
QQ = "100000011"
COMMENT = "三次击杀两次爆头，这回合的火力值得单独留个纪念。"


def _item() -> SimpleNamespace:
    return SimpleNamespace(
        job_id="highlight-test",
        group_id=GROUP,
        steam_id=STEAM,
        match_id="PVP@test",
        context={
            "match": {"result_label": "失败"},
            "highlight": {
                "tags": ["3k"],
                "stats": {"kills": 3, "headshots": 2},
                "round_number": 5,
                "events": [{"tick": tick, "weapon": "ak47"} for tick in (500, 700, 900)],
            },
        },
    )


def _channel(tmp_path: Path) -> PWCS2Channel:
    channel = PWCS2Channel.__new__(PWCS2Channel)
    channel.repository = BindingRepository(tmp_path / "bindings.db")
    channel.match_commentary = SimpleNamespace(enabled=True)
    channel.ai = SimpleNamespace(
        configured=True,
        complete_once=AsyncMock(return_value=json.dumps({
            "comments": [{"steam_id": STEAM, "comment": COMMENT}]
        })),
    )
    return channel


async def test_video_mentions_current_group_protagonist_with_comment_in_one_send(tmp_path):
    channel = _channel(tmp_path)
    channel.repository.bind("111111", "222222", STEAM)
    channel.repository.bind(GROUP, QQ, STEAM)
    caption, qq_id = await channel._highlight_message_details(_item())
    assert qq_id == QQ
    assert COMMENT in caption
    sent = AsyncMock(return_value={"status": "ok"})
    delivery = HighlightVideoDelivery(sent, allowed_groups={GROUP})
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    await delivery.send(GROUP, path, caption=caption, mention_qq_id=qq_id)
    sent.assert_awaited_once()
    action, params = sent.call_args.args
    assert action == "send_group_msg"
    assert params["group_id"] == int(GROUP)
    assert [part["type"] for part in params["message"]] == ["video", "at", "text"]
    assert params["message"][1]["data"]["qq"] == QQ
    assert COMMENT in params["message"][2]["data"]["text"]
    payload = json.loads(channel.ai.complete_once.call_args.kwargs["content"])
    assert "match" not in payload
    assert "result_label" not in payload
    assert payload["stats"]["kills"] == 3


async def test_missing_binding_does_not_mention_a_member_from_another_group(tmp_path):
    channel = _channel(tmp_path)
    channel.repository.bind("111111", QQ, STEAM)
    item = _item()
    assert await channel._highlight_message_details(item) == (_highlight_caption(item), "")
    channel.ai.complete_once.assert_not_awaited()


async def test_opted_out_member_is_mentioned_without_commentary(tmp_path):
    channel = _channel(tmp_path)
    channel.repository.bind(GROUP, QQ, STEAM)
    channel.repository.set_match_commentary_enabled(GROUP, QQ, enabled=False)
    item = _item()
    assert await channel._highlight_message_details(item) == (_highlight_caption(item), QQ)
    channel.ai.complete_once.assert_not_awaited()


@pytest.mark.parametrize("failure", [AIChatError("unavailable"), TimeoutError()])
async def test_ai_failure_still_allows_video_and_mention(tmp_path, failure):
    channel = _channel(tmp_path)
    channel.repository.bind(GROUP, QQ, STEAM)
    channel.ai.complete_once.side_effect = failure
    item = _item()
    caption, qq_id = await channel._highlight_message_details(item)
    assert (caption, qq_id) == (_highlight_caption(item), QQ)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    sent = AsyncMock(return_value={"status": "ok"})
    await HighlightVideoDelivery(sent).send(
        GROUP, path, caption=caption, mention_qq_id=qq_id
    )
    sent.assert_awaited_once()
    assert sent.call_args.args[1]["message"][1] == {"type": "at", "data": {"qq": QQ}}


async def test_ai_cancellation_propagates_without_sending(tmp_path):
    channel = _channel(tmp_path)
    channel.repository.bind(GROUP, QQ, STEAM)
    channel.ai.complete_once.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await channel._highlight_message_details(_item())


@pytest.mark.parametrize("comment", [
    "三杀直接赢下比赛！", "比分 13:0！", "@全体成员 看这里",
    "AK连收三杀，两枪爆头，火力全开拿下这波关键局！",
])
async def test_commentary_discards_outcome_claims_and_injected_mentions(tmp_path, comment):
    channel = _channel(tmp_path)
    channel.ai.complete_once.return_value = json.dumps({
        "comments": [{"steam_id": STEAM, "comment": comment}]
    })
    assert await generate_highlight_commentary(channel.ai, _item()) is None


async def test_video_delivery_rejects_at_all(tmp_path):
    sent = AsyncMock()
    with pytest.raises(HighlightProtocolError, match="single valid QQ ID"):
        await HighlightVideoDelivery(sent).send(
            GROUP, tmp_path / "video.mp4", mention_qq_id="all"
        )
    sent.assert_not_awaited()


async def test_match_rewards_are_credited_silently_and_still_deduplicated(tmp_path):
    channel = PWCS2Channel({
        "enabled": True,
        "databasePath": str(tmp_path / "bindings.db"),
        "statsImageEnabled": False,
        "aiEnabled": False,
    }, MessageBus())
    channel.repository.bind(GROUP, QQ, STEAM)
    before, _ = channel.community_repository.wallet(GROUP, QQ)
    channel._reply = AsyncMock()
    match = build_match_snapshot(
        {"matchId": "quiet-reward", "team": 1, "winTeam": 1, "score1": 13, "score2": 8},
        steam_id=STEAM,
        profile={"name": "Player"},
    )
    try:
        await channel._send_match_notification(GROUP, match)
        after, _ = channel.community_repository.wallet(GROUP, QQ)
        assert after > before
        channel._reply.assert_awaited_once()
        assert "完赛奖励" not in channel._reply.call_args.args[2]
        await channel._send_match_notification(GROUP, match)
        assert channel.community_repository.wallet(GROUP, QQ)[0] == after
        assert channel._reply.await_count == 2
    finally:
        await channel.stop()
