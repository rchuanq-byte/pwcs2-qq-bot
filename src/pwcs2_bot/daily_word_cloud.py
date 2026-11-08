"""Aggregate group chat word counts and send a daily rendered word cloud."""

from __future__ import annotations

import asyncio
import html
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from loguru import logger

from pwcs2_bot.database import BindingRepository

SendWordCloud = Callable[[str, "WordCloudSnapshot"], Awaitable[None]]

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_STEAM_OR_QQ_RE = re.compile(r"\b\d{5,20}\b")
_CHINESE_WORD_RE = re.compile(r"[\u3400-\u9fff]{2,8}")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+.-]{2,19}")
_STOPWORDS = {
    "一个",
    "一些",
    "不是",
    "不会",
    "不能",
    "什么",
    "他们",
    "你们",
    "我们",
    "但是",
    "以及",
    "因为",
    "所以",
    "这个",
    "那个",
    "这些",
    "那些",
    "还是",
    "已经",
    "没有",
    "然后",
    "真的",
    "就是",
    "可以",
    "可能",
    "怎么",
    "现在",
    "今天",
    "明天",
    "昨天",
    "自己",
    "大家",
    "一下",
    "这里",
    "那里",
    "比较",
    "感觉",
    "东西",
    "时候",
    "这样",
    "那样",
    "如果",
    "而且",
    "或者",
    "不过",
    "还有",
    "为什么",
    "知道",
    "觉得",
    "哈哈哈",
    "哈哈哈哈",
    "ai",
    "qq",
}


@dataclass(frozen=True, slots=True)
class WordCloudWord:
    text: str
    count: int


@dataclass(frozen=True, slots=True)
class WordCloudSnapshot:
    group_id: str
    cloud_date: str
    message_count: int
    token_count: int
    words: tuple[WordCloudWord, ...]


def tokenize_message(text: str) -> Counter[str]:
    cleaned = _URL_RE.sub(" ", text[:2000])
    cleaned = _STEAM_OR_QQ_RE.sub(" ", cleaned)
    cleaned = re.sub(r"哈{3,}", " 哈哈 ", cleaned)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    import jieba

    counts: Counter[str] = Counter()
    for raw in jieba.cut(cleaned, cut_all=False):
        word = raw.strip().casefold()
        if not word or word in _STOPWORDS:
            continue
        if _CHINESE_WORD_RE.fullmatch(word) or _LATIN_WORD_RE.fullmatch(word):
            counts[word] += 1
        if sum(counts.values()) >= 100:
            break
    return counts


_POSITIONS = (
    (50, 48, 0),
    (31, 45, -6),
    (69, 45, 7),
    (49, 29, 4),
    (51, 67, -5),
    (20, 28, 8),
    (80, 29, -8),
    (19, 64, -7),
    (81, 65, 6),
    (35, 18, -4),
    (65, 18, 5),
    (35, 78, 6),
    (67, 79, -6),
    (10, 45, -8),
    (90, 47, 8),
    (12, 14, 5),
    (88, 14, -5),
    (11, 82, -6),
    (89, 83, 6),
    (49, 9, 0),
    (51, 89, 0),
    (25, 9, -5),
    (75, 9, 5),
    (25, 90, 5),
    (76, 90, -5),
    (6, 29, 8),
    (94, 30, -8),
    (6, 68, -7),
    (94, 69, 7),
    (22, 50, 0),
    (78, 50, 0),
    (42, 61, -4),
    (59, 61, 4),
    (41, 39, 5),
    (60, 38, -5),
    (29, 61, 5),
    (72, 62, -5),
    (28, 31, -5),
    (72, 31, 5),
    (50, 79, 0),
)
_COLORS = ("#ffb52e", "#60a5fa", "#f87171", "#34d399", "#c084fc", "#f8fafc")


def render_word_cloud_html(snapshot: WordCloudSnapshot) -> str:
    highest = max((word.count for word in snapshot.words), default=1)
    lowest = min((word.count for word in snapshot.words), default=1)
    spread = max(1, highest - lowest)
    elements: list[str] = []
    for index, word in enumerate(snapshot.words[: len(_POSITIONS)]):
        left, top, rotation = _POSITIONS[index]
        ratio = (word.count - lowest) / spread
        size = 24 + math.sqrt(max(0, ratio)) * 54
        weight = 600 + min(300, int(ratio * 300))
        elements.append(
            '<span class="word" style="'
            f"left:{left}%;top:{top}%;font-size:{size:.1f}px;"
            f"font-weight:{weight};color:{_COLORS[index % len(_COLORS)]};"
            f"transform:translate(-50%,-50%) rotate({rotation}deg)"
            f'" title="出现 {word.count} 次">{html.escape(word.text)}</span>'
        )
    words_html = "".join(elements)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}html,body{{margin:0;background:transparent}}
body{{font-family:"Noto Sans CJK SC","Noto Sans SC","Microsoft YaHei",sans-serif;color:#f8fafc}}
#word-cloud{{width:1080px;height:760px;padding:38px 42px 28px;border-radius:30px;overflow:hidden;
position:relative;background:radial-gradient(circle at 82% 8%,rgba(245,158,11,.28),transparent 30%),
radial-gradient(circle at 8% 95%,rgba(37,99,235,.24),transparent 34%),
linear-gradient(145deg,#111827,#070b12);
border:1px solid rgba(255,255,255,.12)}}
header{{height:90px;display:flex;align-items:flex-start;justify-content:space-between;position:relative;z-index:2}}
h1{{margin:0;font-size:34px;letter-spacing:2px}}.date{{color:#fbbf24;font-size:20px;font-weight:800}}
.meta{{margin-top:8px;color:#94a3b8;font-size:16px}}.cloud{{height:570px;position:relative;
border-radius:24px;background:rgba(15,23,42,.54);
border:1px solid rgba(255,255,255,.07);overflow:hidden}}
.word{{position:absolute;white-space:nowrap;line-height:1;text-shadow:0 8px 24px rgba(0,0,0,.45)}}
footer{{height:42px;display:flex;align-items:flex-end;justify-content:space-between;color:#64748b;
font-size:13px;letter-spacing:1px}}</style></head><body>
<main id="word-cloud"><header><div><h1>今日群聊词云</h1>
<div class="meta">{snapshot.message_count} 条有效消息 · {snapshot.token_count} 个词元</div></div>
<div class="date">{html.escape(snapshot.cloud_date)}</div></header>
<section class="cloud">{words_html}</section>
<footer><span>仅统计聚合词频，不保存聊天原文</span>
<span>PWCS2 BOT · DAILY WORD CLOUD</span></footer>
</main></body></html>"""


class DailyWordCloudService:
    def __init__(
        self,
        repository: BindingRepository,
        send_word_cloud: SendWordCloud,
        *,
        enabled: bool = True,
        send_time: str = "23:30",
        timezone_name: str = "Asia/Shanghai",
        poll_interval_seconds: float = 300,
        initial_delay_seconds: float = 30,
        max_words: int = 40,
        retention_days: int = 14,
        allowed_groups: list[str] | None = None,
    ) -> None:
        hour, minute = (int(part) for part in send_time.split(":"))
        self.repository = repository
        self.send_word_cloud = send_word_cloud
        self.enabled = enabled
        self.send_time = time(hour, minute)
        self.timezone = ZoneInfo(timezone_name)
        self.poll_interval_seconds = poll_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.max_words = max_words
        self.retention_days = retention_days
        self.allowed_groups = set(allowed_groups or [])
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def record_message(
        self,
        group_id: str,
        text: str,
        *,
        now: datetime | None = None,
    ) -> int:
        if not self.enabled or not text.strip():
            return 0
        local_now = now or datetime.now(self.timezone)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=self.timezone)
        else:
            local_now = local_now.astimezone(self.timezone)
        counts = tokenize_message(text)
        if not counts:
            return 0
        self.repository.record_daily_word_counts(
            group_id,
            local_now.date().isoformat(),
            dict(counts),
        )
        return sum(counts.values())

    async def run(self) -> None:
        if not self.enabled or await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: daily word cloud started (time={} timezone={})",
            self.send_time.strftime("%H:%M"),
            self.timezone.key,
        )
        while not self._stop_event.is_set():
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected daily word cloud failure")
            if await self._wait(self.poll_interval_seconds):
                return

    async def _wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def run_due_once(self, now: datetime | None = None) -> int:
        local_now = now or datetime.now(self.timezone)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=self.timezone)
        else:
            local_now = local_now.astimezone(self.timezone)
        if local_now.time().replace(tzinfo=None) < self.send_time:
            return 0
        cloud_date = local_now.date().isoformat()
        group_ids = (
            self.allowed_groups
            if self.allowed_groups
            else set(self.repository.list_daily_word_cloud_groups(cloud_date))
        )
        sent = 0
        for group_id in sorted(group_ids):
            if self.repository.is_daily_word_cloud_processed(group_id, cloud_date):
                continue
            rows = list(
                self.repository.get_daily_word_counts(
                    group_id,
                    cloud_date,
                    limit=max(100, self.max_words * 3),
                )
            )
            frequent = [row for row in rows if row[1] >= 2]
            selected = frequent[: self.max_words]
            if len(selected) < min(10, self.max_words):
                selected_ids = {word for word, _count in selected}
                selected.extend(
                    row
                    for row in rows
                    if row[0] not in selected_ids
                )
                selected = selected[: self.max_words]
            if not selected:
                self.repository.mark_daily_word_cloud_processed(
                    group_id,
                    cloud_date,
                    was_sent=False,
                    retention_days=self.retention_days,
                )
                continue
            message_count, token_count = self.repository.get_daily_word_cloud_activity(
                group_id, cloud_date
            )
            snapshot = WordCloudSnapshot(
                group_id=group_id,
                cloud_date=cloud_date,
                message_count=message_count,
                token_count=token_count,
                words=tuple(WordCloudWord(word, count) for word, count in selected),
            )
            try:
                await self.send_word_cloud(group_id, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: daily word cloud failed group={} date={}: {}",
                    group_id,
                    cloud_date,
                    exc,
                )
                continue
            self.repository.mark_daily_word_cloud_processed(
                group_id,
                cloud_date,
                was_sent=True,
                retention_days=self.retention_days,
            )
            sent += 1
        return sent
