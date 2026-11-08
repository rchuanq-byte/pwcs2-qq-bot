"""Deterministic distribution and fixed AI knowledge for the CS2 training game."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

DEFAULT_TRAINING_GAME_URL = "http://192.0.2.10/cs2-training.html"
TRAINING_GAME_FILE_NAME = "cs2-training.html"
TRAINING_GAME_MAX_FILE_BYTES = 1_000_000

_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_ASCII_KEYWORD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:cs2|aim|fps)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_CHINESE_KEYWORDS = (
    "训练营",
    "训练",
    "练枪",
    "小游戏",
    "打枪",
    "爆头",
    "压枪",
    "反应测试",
    "跟枪",
)
_DIRECT_COMMANDS = {
    "#训练营",
    "/训练营",
    "#小游戏",
    "/小游戏",
    "#练枪",
    "/练枪",
}
_FILE_COMMANDS = {
    "#训练文件",
    "/训练文件",
    "#小游戏文件",
    "/小游戏文件",
}
_FILE_REQUEST_PHRASES = ("发文件", "发原文件", "原文件", "html文件", "html 文件")
_LINK_REQUEST_PHRASES = ("链接", "网址", "打开", "测试链接", "发我", "怎么玩")


def _without_urls(text: str) -> str:
    return _URL_PATTERN.sub("", text.strip())


def is_training_game_trigger(text: str) -> bool:
    """Match requested game keywords without matching URLs or larger ASCII words."""
    normalized = _without_urls(text)
    if not normalized:
        return False
    folded = normalized.casefold()
    if folded.startswith(("#ai", "/ai")):
        return False
    return any(keyword in normalized for keyword in _CHINESE_KEYWORDS) or bool(
        _ASCII_KEYWORD_PATTERN.search(normalized)
    )


def is_training_game_direct_request(text: str) -> bool:
    """Return true for an explicit request that may bypass the group cooldown."""
    normalized = _without_urls(text).strip()
    folded = normalized.casefold()
    if folded in _DIRECT_COMMANDS:
        return True
    return is_training_game_trigger(normalized) and any(
        phrase in normalized for phrase in _LINK_REQUEST_PHRASES
    )


def is_training_game_file_request(text: str, *, addressed_to_bot: bool = False) -> bool:
    """Recognize only fixed game-file requests; never accept a caller-supplied path."""
    normalized = _without_urls(text).strip()
    folded = normalized.casefold()
    if folded in _FILE_COMMANDS:
        return True
    has_file_phrase = any(phrase in folded for phrase in _FILE_REQUEST_PHRASES)
    if not has_file_phrase:
        return False
    if is_training_game_trigger(normalized):
        return True
    return addressed_to_bot and folded in {
        "发文件",
        "发原文件",
        "原文件",
        "html文件",
        "html 文件",
    }


def format_training_game_intro(url: str) -> str:
    return (
        "🎯 CS2 战术训练营\n"
        "网页版 CS 训练场：爆头 / 跟枪 / 压枪 / 反应测试四种模式，含经济系统和 "
        "5 把经典武器，浏览器打开即玩，无需安装。\n"
        f"链接：{url}\n"
        "操作：鼠标瞄准 · 左键射击 · R 换弹 · B 买枪 · 1-5 切枪 · "
        "右键 AWP 开镜 · Esc 暂停 · M 静音"
    )


def build_training_game_system_prompt(url: str) -> str:
    """Return immutable game facts for the AI system prompt."""
    return (
        "\n\n【CS2 战术训练营固定知识】\n"
        f"公网地址：{url}。这是浏览器单文件小游戏，无需安装。用户要打开、查看或测试时，"
        "只给出该地址；文件发送由确定性 QQ 通道处理，你不得声称自己已发送文件、打开网页、"
        "截图或远程试玩。\n"
        "四种模式：1. 爆头练习 45 秒，靶子随机刷新，普通爆头赏金 $400，金色靶 $900，"
        "连杀最高 ×5 得分加成；2. 跟枪练习 45 秒，假人左右横移并随机变向，统计跟踪率与"
        "命中率，击杀赏金 $300；3. 压枪练习在墙面显示弹道参考点，打满一个弹匣按贴合度"
        "评级 S–D，S 级赏金 $600；4. 反应测试随机延迟出靶，共 10 轮，统计平均、最快、"
        "中位数反应时间。\n"
        "武器与价格：USP-S 免费、沙漠之鹰 $700、AK-47 $2700、M4A4 $3100、AWP $4750。"
        "初始资金 $800，赏金跨局保留，B 打开购买菜单。AWP 一枪一个、右键开镜；步枪按住"
        "左键连发；AK 伤害高但较难压，M4 更稳定。最高纪录只存浏览器 localStorage，换设备"
        "或清缓存会重置。\n"
        "技巧问题只基于以上事实给通用建议。被问怎么压 AK 时可答：前 10 发主要向上跳，"
        "之后会左右偏；先向下拉鼠标抵消上跳，再进压枪练习对着参考弹道练。关于该游戏的"
        "回答控制在 3–5 行，简洁、有游戏感；禁止编造数值，也不要索要隐私信息。"
    )


def training_game_html_path() -> Path:
    return Path(__file__).with_name("assets") / "training" / TRAINING_GAME_FILE_NAME


class TrainingGameRateLimiter:
    """In-memory anti-spam limits for passive game promotion."""

    def __init__(
        self,
        *,
        group_cooldown_seconds: float,
        user_cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.group_cooldown_seconds = group_cooldown_seconds
        self.user_cooldown_seconds = user_cooldown_seconds
        self.clock = clock
        self._group_last_sent: dict[str, float] = {}
        self._user_last_sent: dict[tuple[str, str], float] = {}

    def allow(self, group_id: str, user_id: str, *, direct: bool) -> bool:
        now = self.clock()
        user_key = (group_id, user_id)
        user_last = self._user_last_sent.get(user_key)
        if (
            user_last is not None
            and now - user_last < self.user_cooldown_seconds
        ):
            return False
        group_last = self._group_last_sent.get(group_id)
        if (
            not direct
            and group_last is not None
            and now - group_last < self.group_cooldown_seconds
        ):
            return False
        self._user_last_sent[user_key] = now
        self._group_last_sent[group_id] = now
        return True
