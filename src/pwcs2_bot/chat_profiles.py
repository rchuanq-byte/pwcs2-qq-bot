"""Conversation-only group scope and low-activity, high-response selection."""

from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

GroupPersona = Literal["sharp_banter", "naruto_mobile"]
GROUP_PERSONA_PROMPTS: dict[GroupPersona, str] = {
    "sharp_banter": (
        "这个群的发言风格是短句、犀利、爱互怼。可以接梗、抬杠和轻度嘲讽，"
        "吐槽对方的观点、战术或离谱操作，不持续针对某个人。"
        "别把玩笑升级成人身羞辱、威胁或歧视，也别和其他机器人无限来回刷屏。"
        "正常求助就先解决问题，不必硬怼。"
    ),
    "naruto_mobile": (
        "这个群偏爱《火影忍者手游》。群友提到忍者、决斗场、段位、秘卷、通灵、"
        "连招和对局时，优先考虑手游语境，顺势讨论忍者选择、打法或对局思路。"
        "其他话题正常聊，有自然关联才带入火影；不要编造版本、技能数值或活动消息。"
    ),
}
# New high-activity rooms need pacing independent of model concurrency.
# Explicit @bot requests keep their existing path and do not use this cooldown.
GROUP_PERSONA_INTERVALS: dict[GroupPersona, float] = {
    "sharp_banter": 25.0,
    "naruto_mobile": 15.0,
}


def conversation_groups(config):
    return set(config.allowed_groups) | set(config.ai_chat_only_groups)


def chat_only(config, gid):
    return str(gid) in config.ai_chat_only_groups


def mention_only(config, gid):
    """Only a native @ to this account authorizes a turn in these groups."""
    return str(gid) in getattr(config, "ai_mention_only_groups", ())


def responsive_groups(config):
    return [g for g in config.ai_companion_responsive_groups if not mention_only(config, g)]


def automatic_groups(settings):
    """Bootstrap uses raw camelCase settings, before the application is built."""
    mentioned = {str(g) for g in settings.get("aiMentionOnlyGroups", ())}
    groups = dict.fromkeys([*settings["allowedGroups"], *settings.get("aiChatOnlyGroups", ())])
    return [g for g in groups if str(g) not in mentioned]


def group_persona_prompt(config, gid):
    gid = str(gid)
    if gid not in conversation_groups(config):
        return ""
    key = getattr(config, "ai_group_personas", {}).get(gid)
    return GROUP_PERSONA_PROMPTS.get(key, "")


def persona_intervals(config):
    return {
        gid: GROUP_PERSONA_INTERVALS[key]
        for gid, key in config.ai_group_personas.items()
        if gid in conversation_groups(config)
    }


def activate_group_personas(settings: dict, profiles: dict[str, GroupPersona]) -> None:
    """Enable conversation, not business permissions, for the selected rooms."""
    if not profiles or any(
        not re.fullmatch(r"[1-9][0-9]{4,14}", gid) or key not in GROUP_PERSONA_PROMPTS
        for gid, key in profiles.items()
    ):
        raise ValueError("Expected numeric QQ group IDs and supported persona names")
    chat_groups = list(settings.get("aiChatOnlyGroups", ()))
    responsive = list(settings.get("aiCompanionResponsiveGroups", ()))
    for gid in profiles:
        if gid not in settings["allowedGroups"] and gid not in chat_groups:
            chat_groups.append(gid)
        if gid not in responsive:
            responsive.append(gid)
    settings.update(
        aiChatOnlyGroups=chat_groups,
        aiCompanionResponsiveGroups=responsive,
        aiMentionOnlyGroups=[
            gid for gid in settings.get("aiMentionOnlyGroups", ()) if gid not in profiles
        ],
        aiGroupPersonas={**settings.get("aiGroupPersonas", {}), **profiles},
    )


_OTHER_AI = re.compile(r"^\s*(?:豆包|元宝|千问|通义|文心|小爱|小度)\s*[,，:：、!！?？]")
_SECRET = re.compile(r"sk-[\w-]{12,}|bearer\s+\S{12,}|-----BEGIN", re.I)


@dataclass
class _ReplyStats:
    eligible: int = 0
    selected: int = 0
    sent: int = 0
    last_selected_at: float | None = None
    recent: deque = field(default_factory=lambda: deque(maxlen=8))


class ResponsiveChatSelector:
    """Select a configured share of eligible inputs, not all raw QQ events.

    Deficit-based selection avoids unlucky random silence in a quiet group.
    First eligible messages are answered; over each 20 eligible inputs a target
    of .85 selects 17. Actual successful sends are separately observable.
    """

    def __init__(self, groups=(), target=0.85, clock=time.monotonic, *, min_intervals=None):
        self.groups = {str(g) for g in groups}
        self.target = min(1.0, max(0.0, float(target)))
        self.clock = clock
        self.min_intervals = min_intervals or {}
        self.states = {gid: _ReplyStats() for gid in self.groups}

    def select(self, gid, text, *, mentions=(), self_id=""):
        state = self.states.get(str(gid))
        text = str(text or "").strip()
        normalized = re.sub(r"\W+", "", text.casefold())[:500]
        if (
            state is None
            or len(normalized) < 2
            or len(text) > 1200
            or text.startswith(("/", "#"))
            or _OTHER_AI.search(text)
            or _SECRET.search(text)
            or re.fullmatch(r"https?://\S+", text)
            or any(str(uid) != str(self_id) for uid in mentions)
        ):
            return False
        now = self.clock()
        interval = self.min_intervals.get(str(gid), 0)
        if state.last_selected_at is not None and now - state.last_selected_at < interval:
            return False
        if any(t == normalized and now - stamp < 60 for stamp, t in state.recent):
            return False
        state.recent.append((now, normalized[:500]))
        state.eligible += 1
        if state.selected >= math.ceil(state.eligible * self.target - 1e-9):
            return False
        state.selected += 1
        state.last_selected_at = now
        return True

    def sent(self, gid):
        if str(gid) in self.states:
            self.states[str(gid)].sent += 1

    def health(self):
        return {
            gid: {"eligible": s.eligible, "selected": s.selected, "sent": s.sent}
            for gid, s in self.states.items()
        }
