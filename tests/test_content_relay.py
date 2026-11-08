from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pwcs2_bot.content_relay import ContentRelayService, build_relay_candidate
from pwcs2_bot.database import BindingRepository

TZ = ZoneInfo("Asia/Shanghai")


def _image(file_name: str, text: str = "") -> list[dict]:
    segments: list[dict] = []
    if text:
        segments.append({"type": "text", "data": {"text": text}})
    segments.append({"type": "image", "data": {"file": file_name}})
    return segments


def test_candidate_keeps_media_but_removes_identity_segments() -> None:
    candidate = build_relay_candidate(
        [
            {"type": "reply", "data": {"id": "123"}},
            {"type": "at", "data": {"qq": "456"}},
            {"type": "text", "data": {"text": "  今天的好图  "}},
            {
                "type": "image",
                "data": {
                    "file": "abc.image",
                    "url": "https://example.invalid/signed-image",
                    "summary": "[图片]",
                },
            },
            {"type": "video", "data": {"url": "https://example.invalid/v.mp4"}},
        ]
    )

    assert candidate is not None
    assert candidate.message == (
        {"type": "text", "data": {"text": "💩 今日份群友搬运\n今天的好图"}},
        {"type": "image", "data": {"file": "abc.image"}},
        {"type": "video", "data": {"file": "https://example.invalid/v.mp4"}},
    )
    assert len(candidate.content_hash) == 64


def test_candidate_rejects_text_only_ads_and_unsafe_media_urls() -> None:
    assert build_relay_candidate([{"type": "text", "data": {"text": "纯聊天"}}]) is None
    assert build_relay_candidate(_image("ad.image", "扫码加群领取福利")) is None
    assert (
        build_relay_candidate(
            [{"type": "image", "data": {"url": "http://127.0.0.1/private"}}]
        )
        is None
    )


def test_candidate_rejects_qq_expression_images() -> None:
    assert (
        build_relay_candidate(
            [
                {
                    "type": "image",
                    "data": {
                        "file": "expression.image",
                        "sub_type": 7,
                        "summary": "[动画表情]",
                    },
                }
            ]
        )
        is None
    )
    assert (
        build_relay_candidate(
            [
                {
                    "type": "image",
                    "data": {
                        "file": "face.image",
                        "sub_type": 0,
                        "summary": "[/飞吻]",
                    },
                }
            ]
        )
        is None
    )
    assert (
        build_relay_candidate(
            [
                {
                    "type": "image",
                    "data": {
                        "file": "normal.image",
                        "sub_type": 0,
                        "summary": "",
                    },
                }
            ]
        )
        is not None
    )


def test_candidate_accepts_whitelisted_video_links() -> None:
    text_candidate = build_relay_candidate(
        [
            {
                "type": "text",
                "data": {"text": "这个视频不错 https://www.bilibili.com/video/BV1test"},
            }
        ]
    )
    card_candidate = build_relay_candidate(
        [
            {
                "type": "json",
                "data": {
                    "data": json.dumps(
                        {
                            "meta": {
                                "detail_1": {"qqdocurl": "https://v.douyin.com/test/"}
                            }
                        }
                    )
                },
            }
        ]
    )

    assert text_candidate is not None
    assert text_candidate.message == (
        {
            "type": "text",
            "data": {
                "text": "💩 今日份群友搬运\n这个视频不错 "
                "https://www.bilibili.com/video/BV1test"
            },
        },
    )
    assert card_candidate is not None
    assert card_candidate.message == (
        {
            "type": "text",
            "data": {"text": "💩 今日份群友搬运\nhttps://v.douyin.com/test/"},
        },
    )


def test_candidate_rejects_non_video_links() -> None:
    assert (
        build_relay_candidate(
            [
                {
                    "type": "text",
                    "data": {"text": "https://example.com/not-a-video"},
                }
            ]
        )
        is None
    )


def test_candidate_accepts_qq_merged_chat_history() -> None:
    candidate = build_relay_candidate(
        [{"type": "forward", "data": {"id": "forward-message-123"}}]
    )

    assert candidate is not None
    assert candidate.message == (
        {"type": "forward", "data": {"id": "forward-message-123"}},
    )
    assert len(candidate.content_hash) == 64


async def test_relay_is_deduplicated_rate_limited_and_persistent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bindings.db"
    repository = BindingRepository(path)
    sent: list[tuple[str, list[dict]]] = []

    async def send(target_group: str, message: list[dict]) -> None:
        sent.append((target_group, message))

    service = ContentRelayService(
        repository,
        send,
        enabled=True,
        source_groups=["source-a", "source-b"],
        target_group="target",
        daily_limit=2,
        cooldown_seconds=3600,
    )

    assert await service.handle_message(
        "source-a", _image("a.image"), now=datetime(2026, 7, 3, 10, 0, tzinfo=TZ)
    )
    assert not await service.handle_message(
        "source-b", _image("a.image"), now=datetime(2026, 7, 3, 11, 1, tzinfo=TZ)
    )
    assert not await service.handle_message(
        "source-b", _image("b.image"), now=datetime(2026, 7, 3, 10, 30, tzinfo=TZ)
    )
    assert await service.handle_message(
        "source-b", _image("b.image"), now=datetime(2026, 7, 3, 11, 1, tzinfo=TZ)
    )
    assert not await service.handle_message(
        "source-a", _image("c.image"), now=datetime(2026, 7, 3, 12, 2, tzinfo=TZ)
    )

    reopened = BindingRepository(path)
    assert len(sent) == 2
    assert {target for target, _message in sent} == {"target"}
    assert reopened.count_content_relays("target", "2026-07-03") == 2
    candidate = build_relay_candidate(_image("a.image"))
    assert candidate is not None
    assert reopened.is_content_relay_sent("target", candidate.content_hash)


async def test_failed_send_is_not_recorded(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")

    async def fail(_target_group: str, _message: list[dict]) -> None:
        raise RuntimeError("OneBot offline")

    service = ContentRelayService(
        repository,
        fail,
        enabled=True,
        source_groups=["source"],
        target_group="target",
        cooldown_seconds=0,
    )
    candidate = build_relay_candidate(_image("retry.image"))
    assert candidate is not None

    try:
        await service.handle_message("source", _image("retry.image"))
    except RuntimeError as exc:
        assert str(exc) == "OneBot offline"
    else:
        raise AssertionError("send failure must propagate")
    assert not repository.is_content_relay_sent("target", candidate.content_hash)


async def test_priority_source_gets_more_slots_and_shorter_cooldown(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    sent: list[str] = []

    async def send(_target_group: str, message: list[dict]) -> None:
        sent.append(message[1]["data"]["file"])

    service = ContentRelayService(
        repository,
        send,
        enabled=True,
        source_groups=["priority", "secondary"],
        target_group="target",
        daily_limit=10,
        cooldown_seconds=1800,
        priority_source_groups=["priority"],
        priority_cooldown_seconds=900,
        secondary_daily_limit=1,
    )

    assert await service.handle_message(
        "secondary",
        _image("secondary-a.image"),
        now=datetime(2026, 7, 3, 10, 0, tzinfo=TZ),
    )
    assert not await service.handle_message(
        "secondary",
        _image("secondary-b.image"),
        now=datetime(2026, 7, 3, 10, 31, tzinfo=TZ),
    )
    assert await service.handle_message(
        "priority",
        _image("priority-a.image"),
        now=datetime(2026, 7, 3, 10, 16, tzinfo=TZ),
    )

    assert sent == ["secondary-a.image", "priority-a.image"]
    assert repository.count_content_relays_from_source(
        "target", "secondary", "2026-07-03"
    ) == 1
    assert repository.count_content_relays_from_source(
        "target", "priority", "2026-07-03"
    ) == 1
