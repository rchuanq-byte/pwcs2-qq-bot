# -*- coding: utf-8 -*-
"""近期话题热度：只记录本群真人讨论，不随机制造口味或固定爱好。"""

import json
import os
import time
from collections import deque
from pathlib import Path

from pwcs2_bot.persona_topics import TOPIC_PATTERNS

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core import logger
from astrbot.core.agent.message import TextPart


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "off", "no"}


ENABLED = _flag("DSH_INTEREST")
# 影子模式：照样统计热度、照样轮换口味，但**不**注入 prompt。
# 先看热度数字合不合理，再放开口味注入。
SHADOW = _flag("DSH_INTEREST_SHADOW")
GROUPS = {
    g.strip() for g in os.environ.get("DSH_INTEREST_GROUPS", "0").split(",") if g.strip()
}
# 热度统计窗口：这条消息之前的多少秒内算「最近」。
HOT_WINDOW = max(60, int(os.environ.get("DSH_INTEREST_HOT_WINDOW", str(5 * 60))))
# 热度升级需要的命中次数（在窗口内）。
HOT_HITS = max(2, int(os.environ.get("DSH_INTEREST_HOT_HITS", "3")))
# 热度权重最高升到基线的几倍。
HOT_CAP = float(os.environ.get("DSH_INTEREST_HOT_CAP", "1.6"))
# 口味周期：每隔多少小时轮换一次「今日馋」。
MOOD_HOURS = max(1, float(os.environ.get("DSH_INTEREST_MOOD_HOURS", "4")))
# 注入开关（睡眠时段等）。
HOURS = os.environ.get("DSH_INTEREST_HOURS", "10-23,0-2")
TIMEZONE = os.environ.get("DSH_INTEREST_TZ", "Asia/Shanghai")
STATE_PATH = Path(os.environ.get(
    "DSH_INTEREST_STATE_PATH", "/AstrBot/data/dsh_interest_state.json"))
OWNER = os.environ.get("DSH_INTEREST_OWNER", "0").strip()


def parse_hours(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        bits = part.strip().split("-", 1)
        try:
            start, end = int(bits[0]), int(bits[-1])
        except (ValueError, IndexError):
            continue
        hour = start
        for _ in range(24):
            out.add(hour)
            if hour == end:
                break
            hour = (hour + 1) % 24
    return out


ACTIVE_HOURS = parse_hours(HOURS)


def _now_tz(now=None):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = time.time() if now is None else now
    try:
        return datetime.fromtimestamp(now, ZoneInfo(TIMEZONE))
    except Exception:
        return datetime.fromtimestamp(now)


def in_active_hours(now: float | None = None) -> bool:
    return _now_tz(now).hour in ACTIVE_HOURS


# ------------------------------------------------------------------ 食材池
# 轮换口味从这个池子里抽。名字就是注入 prompt 里说的话——
# 留的是「大胃袋会馋」的具体名词，不要放抽象分类。
_MOOD_POOL = []  # Retired compatibility slot: never sample invented preferences.

# 生效中的热度条目：{ "group_id:兴趣名": deque[(ts, text)] }
# 每条消息只数一次；窗口过期自动丢。
_hot: dict[str, deque] = {}

# 上次写状态/换口味的时间戳（跨重启可恢复）。
_state = {"mood": {}, "hot": {}, "last_swap": 0.0, "topic_origin": "group_discussion"}


def _load_state() -> dict:
    try:
        saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            return saved
    except (OSError, ValueError):
        pass
    return {}


def _save_state() -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        logger.warning("[interest] 保存状态失败: %s", exc)


def _load_from_disk_and_merge() -> None:
    global _state
    saved = _load_state()
    if not saved:
        return
    saved.setdefault("mood", {})
    saved.setdefault("hot", {})
    saved.setdefault("last_swap", 0.0)
    _state = saved


# ------------------------------------------------------------------ 热度
_GROUP_PARTS = TOPIC_PATTERNS


def note_group_heat(gid: str, text: str, now: float) -> None:
    """把一条群消息按兴趣类别计入热度窗口。纯数据累积，无副作用。"""
    if not text or gid not in GROUPS:
        return
    for name, pat in _GROUP_PARTS.items():
        if pat.search(text):
            key = f"{gid}:{name}"
            q = _hot.get(key)
            if q is None:
                q = _hot[key] = deque(maxlen=32)
            # 窗口外的旧记录只管丢，不参与计数。
            while q and now - q[0][0] > HOT_WINDOW * 3:
                q.popleft()
            if any(old_text == text[:60] and now - stamp <= HOT_WINDOW for stamp, old_text in q):
                continue
            q.append((now, text[:60]))


def hot_weight(gid: str, name: str, now: float | None = None) -> float:
    """当前某兴趣的热度权重（1.0 = 基线，最高 HOT_CAP）。

    规则：HOT_WINDOW 内命中次数 >= HOT_HITS 才升权；
    用指数衰减（距最近命中的时间越久，越接近基线）。
    """
    now = time.time() if now is None else now
    q = _hot.get(f"{gid}:{name}")
    if not q:
        return 1.0
    recent = [ts for ts, _ in q if now - ts <= HOT_WINDOW]
    if len(recent) < HOT_HITS:
        return 1.0
    newest = max(recent)
    age = now - newest
    # age=0 时 boost=HOT_CAP；每过 HOT_WINDOW 衰减一半（约 30 分钟回 1.0）。
    boost = 1.0 + (HOT_CAP - 1.0) * 2 ** (-age / HOT_WINDOW)
    return min(HOT_CAP, max(1.0, boost))


def heat_snapshot(now: float | None = None, gid: str | None = None) -> list[str]:
    """Only repeated, recent discussion in the requesting group counts as context."""
    if not gid:
        return []
    now = time.time() if now is None else now
    rows = []
    for key, q in _hot.items():
        group, name = key.split(":", 1)
        if group != str(gid) or name not in TOPIC_PATTERNS:
            continue
        recent = [ts for ts, _ in q if 0 <= now - ts <= HOT_WINDOW]
        if len(recent) >= HOT_HITS:
            rows.append((len(recent), max(recent), name))
    rows.sort(key=lambda row: (-row[0], -row[1]))
    return [name for _, _, name in rows[:3]]


# 热度快照落盘节流（秒）：给 dsh-proactive 读的权重表不必每条消息都写盘。
_PERSIST_EVERY = float(os.environ.get("DSH_INTEREST_PERSIST_EVERY", "30"))
_last_persist = 0.0


def hot_weight_table(now: float | None = None) -> dict:
    """给 dsh-proactive 用的权重表：{gid: {兴趣名: 权重}}。

    只暴露升权中的条目；没热度的兴趣由 proactive 取基线 1.0。
    **_hot 是唯一真相源**：过期条目在 hot_weight 里按时间筛选，
    不依赖状态文件回填，所以表永远反映「此刻」的实时热度。
    """
    now = time.time() if now is None else now
    out: dict[str, dict[str, float]] = {}
    for key, q in _hot.items():
        gid, name = key.split(":", 1)
        w = hot_weight(gid, name, now)
        if w <= 1.0:
            continue
        out.setdefault(gid, {})[name] = round(w, 3)
    return out


def persist_hot_snapshot(now: float | None = None) -> None:
    """把当前热度权重表写进状态文件（节流）。"""
    global _last_persist
    now = time.time() if now is None else now
    if now - _last_persist < _PERSIST_EVERY:
        return
    _last_persist = now
    _state["hot"] = hot_weight_table(now)
    _save_state()


# ------------------------------------------------------------------ 口味轮换
def maybe_swap_mood(now: float | None = None) -> None:
    """Compatibility entry point: retire an old generated mood, never create one."""
    if _state.get("mood") or _state.get("last_swap"):
        _state["mood"], _state["last_swap"] = {}, 0.0
        _state["topic_origin"] = "group_discussion"
        _save_state()


def current_mood() -> list[str]:
    return []


def render_interest_block(now: float | None = None, gid: str | None = None) -> str:
    if not in_active_hours(now):
        return ""
    hot = heat_snapshot(now, gid=gid)
    if not hot:
        return ""
    return ("<interest_state>本群近期正在讨论：" + "、".join(hot)
            + "。仅作语境参考，不等于你的固定爱好；本轮相关时才跟进，不要转移话题。</interest_state>")


class Main(star.Star):
    def __init__(self, context: "star.Context") -> None:
        self.context = context
        _load_from_disk_and_merge()
        maybe_swap_mood()
        logger.info(
            "[interest] 已加载：%s 影子=%s 热度窗口%.0f分钟(≥%d条不同消息) 不预设口味 群=%s",
            "开" if ENABLED else "关", "开" if SHADOW else "关",
            HOT_WINDOW / 60, HOT_HITS, ",".join(sorted(GROUPS)) or "无",
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def collect_heat(self, event: AstrMessageEvent) -> None:
        if not ENABLED:
            return
        try:
            from astrbot.core.platform.message_type import MessageType
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            if event.get_platform_name() == "webchat":
                return
            gid = str(event.get_group_id() or "")
            uid = str(event.get_sender_id() or "")
            if not gid or not uid:
                return
            if gid not in GROUPS:
                return
            if uid == "0" or event.get_extra("niuniu_merged") or uid == str(event.get_self_id() or ""):
                return
            text = str(event.get_message_str() or "").strip()
            if not text or text.startswith("/"):
                return
            note_group_heat(gid, text, time.time())
            persist_hot_snapshot()
            maybe_swap_mood()
        except BaseException as exc:
            logger.debug("[interest] 热度统计失败: %s", exc)

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req) -> None:
        if not ENABLED or SHADOW or str(event.get_group_id() or "") not in GROUPS:
            return
        try:
            block = render_interest_block(gid=str(event.get_group_id() or ""))
            if not block:
                return
            req.extra_user_content_parts.append(TextPart(text=block))
            logger.info("[interest] 注入本群近期话题 gid=%s", event.get_group_id())
        except BaseException as exc:
            logger.warning("[interest] 注入失败，跳过: %r", exc)

    @filter.command("兴趣状态", alias={"馋什么"})
    async def status(self, event: AstrMessageEvent) -> None:
        if str(event.get_sender_id()) != OWNER:
            return
        hot = heat_snapshot(gid=str(event.get_group_id() or ""))
        yield event.plain_result(
            "兴趣来源：本群近期真实讨论，不生成预设口味。\n热度榜：%s\n影子：%s"
            % ("、".join(hot) if hot else "暂无持续话题", "开" if SHADOW else "关")
        )
