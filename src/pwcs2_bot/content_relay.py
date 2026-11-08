"""Sanitized, rate-limited media relay between explicitly configured QQ groups."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from loguru import logger

from pwcs2_bot.database import BindingRepository

SendRelay = Callable[[str, list[dict[str, Any]]], Awaitable[None]]

DEFAULT_BLOCKED_KEYWORDS = (
    "广告",
    "推广",
    "加群",
    "代练",
    "接单",
    "博彩",
    "返利",
    "抽奖",
    "扫码",
    "二维码",
)

VIDEO_LINK_DOMAINS = (
    "b23.tv",
    "bilibili.com",
    "douyin.com",
    "iesdouyin.com",
    "kuaishou.com",
    "chenzhongtech.com",
    "xiaohongshu.com",
    "xhslink.com",
)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RelayCandidate:
    content_hash: str
    message: tuple[dict[str, Any], ...]


def _safe_media_file(data: dict[str, Any]) -> str:
    cached_file = str(data.get("file") or "").strip()
    if cached_file:
        return cached_file
    url = str(data.get("url") or "").strip()
    if urlparse(url).scheme.casefold() == "https":
        return url
    return ""


def _is_expression_image(data: dict[str, Any]) -> bool:
    subtype = str(data.get("sub_type") if data.get("sub_type") is not None else "")
    if subtype.strip() not in {"", "0"}:
        return True
    summary = str(data.get("summary") or "").strip()
    return summary == "[动画表情]" or summary.startswith("[/")


def _is_allowed_video_link(value: str) -> bool:
    hostname = (urlparse(value).hostname or "").casefold().rstrip(".")
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in VIDEO_LINK_DOMAINS
    )


def _extract_urls(value: str) -> list[str]:
    return [
        match.group(0).rstrip(".,!?;:，。！？；：）)]}")
        for match in URL_PATTERN.finditer(value.replace("\\/", "/"))
    ]


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _share_card_links(data: dict[str, Any]) -> list[str]:
    raw = data.get("data", data)
    if isinstance(raw, str):
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
    else:
        payload = raw
    links: list[str] = []
    for value in _walk_strings(payload):
        links.extend(_extract_urls(value))
    return links


def build_relay_candidate(
    segments: list[dict[str, Any]],
    *,
    max_text_chars: int = 120,
    max_media: int = 4,
    blocked_keywords: tuple[str, ...] = DEFAULT_BLOCKED_KEYWORDS,
) -> RelayCandidate | None:
    """Keep short text and media while dropping sender, reply and mention metadata."""
    text_parts: list[str] = []
    media: list[dict[str, Any]] = []
    fingerprints: list[tuple[str, str]] = []
    video_links: list[str] = []
    forward_ids: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "").casefold()
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        if segment_type == "text":
            value = " ".join(str(data.get("text") or "").split())
            if value:
                text_parts.append(value)
                video_links.extend(_extract_urls(value))
            continue
        if segment_type in {"json", "share"}:
            video_links.extend(_share_card_links(data))
            continue
        if segment_type == "forward":
            forward_id = str(data.get("id") or "").strip()
            if forward_id:
                forward_ids.append(forward_id)
            continue
        if segment_type not in {"image", "video"} or len(media) >= max_media:
            continue
        if segment_type == "image" and _is_expression_image(data):
            continue
        file_value = _safe_media_file(data)
        if not file_value:
            continue
        media.append({"type": segment_type, "data": {"file": file_value}})
        fingerprints.append((segment_type, file_value))

    video_links = list(
        dict.fromkeys(link for link in video_links if _is_allowed_video_link(link))
    )
    forward_ids = list(dict.fromkeys(forward_ids))
    if not media and not video_links and not forward_ids:
        return None
    text = " ".join(text_parts).strip()
    if len(text) > max_text_chars:
        if not media and video_links:
            text = ""
        else:
            return None
    folded_text = text.casefold()
    if any(keyword.casefold() in folded_text for keyword in blocked_keywords):
        return None

    payload = json.dumps(
        {
            "text": text,
            "media": fingerprints,
            "video_links": video_links,
            "forward_ids": forward_ids,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_hash = hashlib.sha256(payload).hexdigest()
    if forward_ids:
        return RelayCandidate(
            content_hash=content_hash,
            message=({"type": "forward", "data": {"id": forward_ids[0]}},),
        )
    intro = "💩 今日份群友搬运"
    if text:
        intro += f"\n{text}"
    for link in video_links:
        if link not in text:
            intro += f"\n{link}"
    message = ({"type": "text", "data": {"text": intro}}, *media)
    return RelayCandidate(content_hash=content_hash, message=message)


class ContentRelayService:
    def __init__(
        self,
        repository: BindingRepository,
        send_relay: SendRelay,
        *,
        enabled: bool = False,
        source_groups: list[str] | None = None,
        target_group: str = "",
        daily_limit: int = 5,
        cooldown_seconds: int = 3600,
        priority_source_groups: list[str] | None = None,
        priority_cooldown_seconds: int = 900,
        secondary_daily_limit: int | None = None,
        timezone_name: str = "Asia/Shanghai",
        max_text_chars: int = 120,
        max_media: int = 4,
        blocked_keywords: list[str] | None = None,
    ) -> None:
        self.repository = repository
        self.send_relay = send_relay
        self.enabled = enabled
        self.source_groups = set(source_groups or [])
        self.target_group = target_group
        self.daily_limit = daily_limit
        self.cooldown_seconds = cooldown_seconds
        self.priority_source_groups = set(priority_source_groups or [])
        self.priority_cooldown_seconds = priority_cooldown_seconds
        self.secondary_daily_limit = (
            daily_limit if secondary_daily_limit is None else secondary_daily_limit
        )
        self.timezone = ZoneInfo(timezone_name)
        self.max_text_chars = max_text_chars
        self.max_media = max_media
        self.blocked_keywords = tuple(blocked_keywords or DEFAULT_BLOCKED_KEYWORDS)
        self._lock = asyncio.Lock()

    def is_source_group(self, group_id: str) -> bool:
        return (
            self.enabled
            and bool(self.target_group)
            and group_id in self.source_groups
            and group_id != self.target_group
        )

    async def handle_message(
        self,
        source_group: str,
        segments: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> bool:
        if not self.is_source_group(source_group):
            return False
        candidate = build_relay_candidate(
            segments,
            max_text_chars=self.max_text_chars,
            max_media=self.max_media,
            blocked_keywords=self.blocked_keywords,
        )
        if candidate is None:
            return False

        local_now = now or datetime.now(self.timezone)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=self.timezone)
        else:
            local_now = local_now.astimezone(self.timezone)
        sent_at = local_now.astimezone(UTC)
        sent_date = local_now.date().isoformat()

        async with self._lock:
            if self.repository.is_content_relay_sent(
                self.target_group, candidate.content_hash
            ):
                return False
            if (
                self.repository.count_content_relays(self.target_group, sent_date)
                >= self.daily_limit
            ):
                return False
            is_priority = source_group in self.priority_source_groups
            if (
                not is_priority
                and self.repository.count_content_relays_from_source(
                    self.target_group,
                    source_group,
                    sent_date,
                )
                >= self.secondary_daily_limit
            ):
                return False
            previous = self.repository.last_content_relay_sent_at(self.target_group)
            if previous is not None:
                elapsed = (sent_at - previous).total_seconds()
                required_cooldown = (
                    self.priority_cooldown_seconds
                    if is_priority
                    else self.cooldown_seconds
                )
                if elapsed < required_cooldown:
                    return False
            await self.send_relay(self.target_group, list(candidate.message))
            self.repository.mark_content_relay_sent(
                target_group_id=self.target_group,
                source_group_id=source_group,
                content_hash=candidate.content_hash,
                sent_date=sent_date,
                sent_at=sent_at,
            )
        logger.info(
            "pwcs2: relayed sanitized media source_group={} target_group={} hash={}",
            source_group,
            self.target_group,
            candidate.content_hash[:12],
        )
        return True
