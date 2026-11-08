"""Pinned Dafeiyu personality suite and the Niuniu authority boundary.

Only conversational state can evolve. Models never receive administrative,
financial, shell or arbitrary-file capabilities from these modules.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

UPSTREAM_COMMIT = "8009d1036e5a0a6fa2a2490429375ff4735c5b67"
PERSONA_MODULES = (
    "agency",
    "aiflavour",
    "armor",
    "awareness",
    "claimguard",
    "clarify",
    "ctxclean",
    "decide",
    "desire",
    "drift",
    "effect",
    "emotion",
    "factguard",
    "fatigue",
    "followup",
    "glossary",
    "homophone",
    "human",
    "humanizer",
    "initiate",
    "interest",
    "leakguard",
    "memory",
    "mention",
    "merge",
    "mind",
    "noise",
    "persona-core",
    "proactive",
    "quote",
    "quoteref",
    "scene",
    "selfaware",
    "selfguard",
    "selfworth",
    "slang",
    "social",
    "spine",
    "style",
    "typo",
    "will",
)
EXTENSION_MODULES = (
    "web",
    "video",
    "voice",
    "sticker",
    "steal",
    "poke",
    "welcome",
    "guard",
    "joinguard",
)
CHAT_EXTENSION_COMMANDS = frozenset({"搜", "看网页", "b站", "说话"})
GROUP_EXTENSION_COMMANDS = frozenset({"表情包"})
# Equivalent capabilities already provided by our guarded input path, not a
# second URL/local-file downloader or paid voice service. No duplicate handlers.
REPLACED_MODULES = {
    "imgctx": "same-group, size-bounded explicit/quoted image input",
    "vischain": "configured multimodal provider with isolated current-image observation",
    "listen": "QQ native transcription; replies optionally use the scoped voice extension",
    "acl": "fail-closed group/operator/tool policy, not upstream fail-open ACL",
}
NOT_PERSONALITY_MODULES = {
    "express",
    "fwd",
    "imagegen",
    "pay",
    "repeat",
}
PUBLIC_COMMANDS = frozenset(
    {
        "我的档案",
        "忘记我",
        "记住我",
        "档案记住",
        "我和牛牛",
        "我和鱼",
        "社交退出",
        "社交加入",
        "想干嘛",
    }
)
SYNTHETIC_FLAGS = ("dsh_proactive", "dsh_initiate", "niuniu_merged")


def is_synthetic(event) -> bool:
    return any(event.get_extra(flag) for flag in SYNTHETIC_FLAGS)


def configure_persona_suite(environ, groups, *, owner="", stage=False):
    """Explicit activation, real identities, and pinned upstream numeric defaults."""
    environ["NIUNIU_TOPIC_PROFILE"] = "group_discussion"
    joined = ",".join(str(gid) for gid in groups)
    for module in PERSONA_MODULES:
        key = module.upper().replace("-", "_")
        environ[f"DSH_{key}"] = "1"
        environ[f"DSH_{key}_GROUPS"] = joined
        environ[f"DSH_{key}_OWNER"] = owner
        environ[f"DSH_{key}_NAMES"] = "牛牛精灵,牛牛"
        environ[f"DSH_{key}_SHADOW"] = "0"
    environ.update(
        {
            "DSH_MEM_PROFILE_GROUPS": joined,
            "DSH_MEM_PROFILE_OWNER": owner,
            "DSH_MEM_SELF_NAMES": "牛牛精灵,牛牛",
            "DSH_SOCIAL_MODE": "style",
            "DSH_AGENCY_MODE": "live",
            "DSH_DESIRE_MODE": "style",
            # Upstream P0 is intentionally an observer, not an invented active mode.
            "DSH_MIND_MODE": "observe",
            "DSH_EFFECT_PROVIDER": "niuniu-campus",
            "DSH_CLARIFY_PROVIDER": "niuniu-campus",
            "DSH_DECIDE_PROVIDER": "niuniu-campus",
            # Campus model queueing may exceed upstream's 12/25-second
            # classification deadlines. Keep all pacing/frequency rules intact.
            "DSH_DECIDE_TIMEOUT": "120",
            "DSH_CLARIFY_TIMEOUT": "120",
            "DSH_INITIATE_TIMEOUT": "120",
            "DSH_SLANG_PROVIDER": "niuniu-campus",
            "DSH_SLANG_AUTO": "1",
            "DSH_SLANG_RESEARCH": "1",
        }
    )
    for module in ("PROACTIVE", "INITIATE"):
        environ[f"DSH_{module}_PLATFORM"] = "niuniu-qq"
        environ[f"DSH_{module}_SELF_ID"] = environ["NAPCAT_ACCOUNT"]
    if stage:
        # Sandbox acceptance drives hooks directly; no unscheduled LLM requests.
        for name in ("INITIATE", "PROACTIVE", "EFFECT"):
            environ[f"DSH_{name}"] = "0"
        environ["DSH_SLANG_AUTO"] = "0"
        environ["DSH_SLANG_RESEARCH"] = "0"


def load_persona_commands(plugins: Path) -> frozenset[str]:
    """Read commands from reviewed source, without importing/running plugin code."""
    names = {"人格成长"}
    for module in (*PERSONA_MODULES, *EXTENSION_MODULES):
        path = plugins / f"dsh-{module}" / "main.py"
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "filter"
                    and dec.func.attr == "command"
                ):
                    continue
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    names.add(str(dec.args[0].value).casefold())
                for kw in dec.keywords:
                    if kw.arg == "alias":
                        names.update(str(x).casefold() for x in ast.literal_eval(kw.value))
    return frozenset(names)


def requires_operator(command: str) -> bool:
    return (
        command.casefold()
        not in PUBLIC_COMMANDS | CHAT_EXTENSION_COMMANDS | GROUP_EXTENSION_COMMANDS
    )


def conversational_output(event) -> bool:
    """Never corrupt data, tool results, explicit tasks, code or long answers."""
    if os.environ.get("NIUNIU_PERSONA_SUITE") != "1":
        return True  # Original upstream's standalone replay/test environment.
    if event.get_extra("niuniu_explicit") or not event.get_extra("niuniu_ai"):
        return False
    if event.get_extra("niuniu_tool_count", 0):
        return False
    result = event.get_result()
    if not result or not result.is_model_result():
        return False
    text = result.get_plain_text() or ""
    return len(text) <= 200 and not any(
        x in text for x in ("```", "SteamID", "Rating", "https://", "http://")
    )


def factual_turn(event) -> bool:
    """Mood/temperament must never block a requested capability or known command."""
    if os.environ.get("NIUNIU_PERSONA_SUITE") != "1":
        return False
    if event.get_extra("niuniu_tool_count", 0):
        return True
    text = str(getattr(event, "message_str", "") or "")
    return any(
        word in text
        for word in (
            "战绩",
            "数据",
            "统计",
            "排名",
            "余额",
            "牛币",
            "绑定",
            "商城",
            "商店",
            "游戏状态",
            "高光",
            "回放",
            "服务器",
            "安全",
            "配置",
            "代码",
            "报错",
            "计算",
            "翻译",
            "总结",
            "解释",
            "怎么",
            "为什么",
            "如何",
            "多少",
            "几场",
        )
    )


def suite_status() -> str:
    return (
        f"牛牛人格成长｜大肥鱼上游人格链 {len(PERSONA_MODULES)} 个模块\n"
        "兴趣/情绪/动机/关系/长期记忆会持续更新；黑话先提取、研究、审核再使用。\n"
        "心智协调层按上游 P0 只观测；固定身份、权限和事实任务不随情绪改变。\n"
        "查看：/人格状态 /情绪状态 /兴趣状态 /黑话学习状态 /心智状态"
    )
