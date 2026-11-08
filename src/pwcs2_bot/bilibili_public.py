"""Bounded, unauthenticated Bilibili metadata and caption evidence."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp

from pwcs2_bot.public_fetch import PublicMediaError, PublicResolver, validate_public_url

API = "https://api.bilibili.com"
PAGE_HOSTS = {"www.bilibili.com", "m.bilibili.com", "bilibili.com"}
SHORT_HOSTS = {"b23.tv", "acg.tv"}
VIDEO_ID = re.compile(r"(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,11})")
SUMMARY_REQUEST = re.compile(
    r"总结|概括|讲了(?:什么|啥)|说了(?:什么|啥)|说的(?:什么|啥)|"
    r"怎么(?:喷|说|骂)|原话|发言内容|视频内容|讲的(?:什么|啥)|复述|字幕"
)
PASTED_EVIDENCE = re.compile(r"(?:字幕|文字稿|转录|原文|语音转写(?:（[^）]{0,30}）)?)\s*[:：]\s*\S")

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    page: int = 1

    @property
    def params(self):
        if self.video_id.startswith("BV"):
            return {"bvid": self.video_id}
        return {"aid": self.video_id[2:]}


@dataclass(frozen=True)
class VideoEvidence:
    ref: VideoRef | None = None
    title: str = ""
    part: str = ""
    author: str = ""
    description: str = ""
    caption: str = ""
    limitation: str = ""
    duration: int | None = None
    stats: tuple[tuple[str, int], ...] = ()

    def render(self):
        if not self.ref:
            return ""
        lines = [f"B站视频 {self.ref.video_id}（第 {self.ref.page} P）"]
        for label, value in (
            ("标题", self.title),
            ("分P名称", self.part),
            ("UP主", self.author),
            ("简介", self.description),
        ):
            if value:
                lines.append(f"{label}：{value}")
        if self.duration is not None:
            lines.append(f"时长：{self.duration}秒")
        if self.stats:
            lines.append("｜".join(f"{label} {number}" for label, number in self.stats))
        if self.caption:
            lines.append("公开视频字幕摘录（未核实画面或音频）：\n" + self.caption)
        else:
            lines.append("上面仅是公开资料，不是视频里的发言。")
            lines.append("字幕状态：" + (self.limitation or "没有可公开读取的字幕") + "。")
        return "\n".join(lines)

    def unavailable_reply(self):
        has_metadata = bool(self.title or self.part or self.description)
        known = "目前只读到了视频的公开资料。" if has_metadata else ""
        return (
            known
            + (self.limitation or "目前读不到公开视频字幕")
            + "，没法核实视频里具体说了什么。发字幕或文字稿过来，我可以直接总结。"
        )


def wants_video_words(text):
    return bool(SUMMARY_REQUEST.search(text) and not PASTED_EVIDENCE.search(text))


def _ref(value):
    value = value.strip()
    if VIDEO_ID.fullmatch(value):
        return VideoRef(value)
    if value.startswith(("www.", "m.bilibili.com/", "bilibili.com/")):
        value = "https://" + value
    try:
        validate_public_url(value)
        p = urlsplit(value)
    except PublicMediaError:
        return None
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,11})/?", p.path)
    if p.hostname not in PAGE_HOSTS or not match:
        return None
    page = parse_qs(p.query).get("p", ["1"])[0]
    return VideoRef(match[1], int(page) if re.fullmatch(r"[1-9][0-9]{0,3}", page) else 1)


def _short(value):
    if value.startswith(("b23.tv/", "acg.tv/")):
        value = "https://" + value
    try:
        validate_public_url(value)
        p = urlsplit(value)
    except PublicMediaError:
        return ""
    if p.hostname in SHORT_HOSTS and re.fullmatch(r"/[A-Za-z0-9]{2,32}/?", p.path):
        return f"https://{p.hostname}{p.path}"
    return ""


def is_video_reference(value):
    return bool(_ref(value) or _short(value))


async def _resolve(session, value):
    if ref := _ref(value):
        return ref
    current = _short(value)
    for _ in range(3):
        if not current:
            break
        async with session.get(current, allow_redirects=False) as response:
            if response.status not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("Location")
            if not location:
                break
            target = urljoin(current, location)
        # Only inspect a canonical video redirect; do not fetch the blocked
        # playback page, and never follow a short link to another site.
        if ref := _ref(target):
            return ref
        current = _short(target)
    return None


async def _json(session, url, *, params=None, maximum=512 * 1024):
    async with session.get(url, params=params, allow_redirects=False) as response:
        if response.status != 200:
            raise PublicMediaError(f"HTTP {response.status}")
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > maximum:
                raise PublicMediaError("Response exceeds size limit")
    return json.loads(body)


async def _api(session, path, params):
    try:
        result = await _json(session, API + path, params=params)
    except (TimeoutError, ValueError, aiohttp.ClientError):
        return None
    return result.get("data") if isinstance(result, dict) and result.get("code") == 0 else None


def _short_text(value, limit):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _caption_url(value):
    if not isinstance(value, str):
        return ""
    value = "https:" + value if value.startswith("//") else value
    try:
        validate_public_url(value)
        p = urlsplit(value)
    except PublicMediaError:
        return ""
    if p.scheme == "https" and p.port in {None, 443} and p.hostname.endswith(".hdslb.com"):
        return value
    return ""


async def _caption(session, ref, part):
    cid = part.get("cid")
    if not isinstance(cid, int) or cid <= 0:
        return "", "没取到这个分P的字幕信息"
    player = await _api(session, "/x/player/v2", {**ref.params, "cid": cid})
    if not isinstance(player, dict):
        return "", "字幕接口暂时不可用"
    if player.get("cid", cid) != cid:
        return "", "字幕与当前分P不匹配"
    if player.get("need_login_subtitle"):
        return "", "这条视频的字幕接口需要登录，机器人没有读取权限"
    subtitle = player.get("subtitle")
    tracks = subtitle.get("subtitles") if isinstance(subtitle, dict) else None
    if not isinstance(tracks, list):
        return "", "没有可公开读取的字幕"
    tracks = [track for track in tracks if isinstance(track, dict)]
    tracks.sort(key=lambda track: not str(track.get("lan", "")).startswith(("zh", "ai-zh")))
    for track in tracks[:2]:
        url = _caption_url(track.get("subtitle_url"))
        if not url:
            continue
        try:
            document = await _json(session, url, maximum=256 * 1024)
        except (TimeoutError, ValueError, aiohttp.ClientError):
            continue
        rows = document.get("body") if isinstance(document, dict) else None
        if isinstance(rows, list):
            caption = "\n".join(
                _short_text(row.get("content"), 300) for row in rows[:240] if isinstance(row, dict)
            ).strip()[:3000]
            if caption:
                return caption, ""
    return "", "没有可公开读取的字幕"


async def read_video(value, *, include_captions=True):
    """Use public alternatives once when view is blocked; never retry a 412."""
    if not is_video_reference(value):
        return VideoEvidence(limitation="没认出有效的B站视频链接或编号")
    ref = None
    try:
        async with (
            asyncio.timeout(15),
            aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=5),
                headers=_HEADERS,
            ) as session,
        ):
            ref = await _resolve(session, value)
            if not ref:
                return VideoEvidence(limitation="短链没能解析成公开的B站视频")
            view = await _api(session, "/x/web-interface/wbi/view", ref.params)
            view = view if isinstance(view, dict) else {}
            parts = view.get("pages")
            desc = view.get("desc")
            if not parts or not view:
                parts, brief = await asyncio.gather(
                    _api(session, "/x/player/pagelist", ref.params),
                    _api(session, "/x/web-interface/archive/desc", ref.params),
                )
                desc = desc or brief
            part = {}
            if isinstance(parts, list) and 0 < ref.page <= len(parts):
                item = parts[ref.page - 1]
                part = item if isinstance(item, dict) else {}
            owner = view.get("owner")
            duration = part.get("duration", view.get("duration"))
            counters = view.get("stat")
            stats = (
                tuple(
                    (label, counters[key])
                    for key, label in (
                        ("view", "播放"),
                        ("like", "点赞"),
                        ("danmaku", "弹幕"),
                        ("reply", "评论"),
                        ("favorite", "收藏"),
                        ("coin", "投币"),
                    )
                    if type(counters.get(key)) is int and counters[key] >= 0
                )
                if isinstance(counters, dict)
                else ()
            )
            caption, limitation = ("", "本次只查了公开资料")
            if include_captions:
                caption, limitation = await _caption(session, ref, part)
            return VideoEvidence(
                ref=ref,
                title=_short_text(view.get("title"), 160),
                part=_short_text(part.get("part"), 160),
                author=_short_text(owner.get("name"), 80) if isinstance(owner, dict) else "",
                description=_short_text(desc, 400),
                caption=caption,
                limitation=limitation,
                duration=duration if type(duration) is int and duration >= 0 else None,
                stats=stats,
            )
    except (TimeoutError, ValueError, aiohttp.ClientError):
        return VideoEvidence(ref=ref, limitation="暂时读不到这条视频的公开内容")
