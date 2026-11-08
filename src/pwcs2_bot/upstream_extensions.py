"""Managed activation and least-privilege scopes for real upstream plugins."""

from __future__ import annotations

from pwcs2_bot.dafeiyu_persona import EXTENSION_MODULES
from pwcs2_bot.deployment_identity import operator_qq

CHAT_TOOLS = frozenset({"web_search", "read_webpage", "bilibili_video", "send_voice"})
GROUP_TOOLS = frozenset({"probe_with_poke", "propose_group_action"})


def configure_extensions(environ, business, *, stage=False):
    enabled = bool(business.get("aiUpstreamExtensionsEnabled", False))
    groups = ",".join(business["allowedGroups"])
    owner = operator_qq(environ)
    environ["NIUNIU_MANAGED_EXTENSIONS"] = "1" if enabled else "0"
    # Never silently activate paid-provider credentials left in an old env file.
    environ["DSH_VOICE_API_KEY"] = ""
    environ["DSH_WEB_SEARCH_KEY"] = ""
    for module in EXTENSION_MODULES:
        prefix = "DSH_" + module.upper()
        environ[prefix] = "1" if enabled else "0"
        environ[prefix + "_GROUPS"] = groups
        environ[prefix + "_OWNER"] = owner
        environ[prefix + "_PROVIDER"] = "niuniu-campus"
    environ.update(
        {
            "DSH_WEB_ENABLE": "1" if enabled else "0",
            "DSH_WEB_MOD_PROVIDER": "niuniu-campus",
            "DSH_WEB_MOD_TIMEOUT": "120",
            "DSH_WEB_MOD_RESPONSE_TIMEOUT": "120",
            "DSH_WEB_ASSOC": "1" if enabled else "0",
            "DSH_VID_UNDERSTAND": "1" if enabled else "0",
            "DSH_VID_GEN": "0",
            "DSH_VID_CONCEPT_AUTO": "0",
            "DSH_VID_TMP": "/AstrBot/data/niuniu-video-frames",
            "DSH_VID_MAX_SECONDS": "60",
            "DSH_VID_FRAME_W": "448",
            "DSH_VID_FRAMES": "4",
            "DSH_NIUNIU_EDGE_ONLY": "1"
            if enabled and not environ.get("DSH_VOICE_API_KEY")
            else "0",
            "DSH_VOICE_EDGE_FALLBACK": "1" if enabled else "0",
            "DSH_VOICE_CENSOR_PROVIDER": "niuniu-campus",
            "DSH_VOICE_CENSOR_TIMEOUT": "120",
            "DSH_VOICE_AUTO": "1" if enabled else "0",
            "DSH_VOICE_EMOTION_AUTO": "1" if enabled and not stage else "0",
            "DSH_VOICE_EMOTION_GROUPS": groups,
            "DSH_STEAL_PROVIDER": "niuniu-campus",
            "DSH_GUARD_SHADOW": "1",
            "DSH_GUARD_MAX_BAN_SEC": "600",
            "DSH_GUARD_FLOOD_BAN_SEC": "600",
            "DSH_GUARD_PROVIDER": "niuniu-campus",
            "DSH_GUARD_TIMEOUT": "120",
            "DSH_JOINGUARD_PROVIDER": "niuniu-campus",
            "DSH_JOINGUARD_TIMEOUT": "120",
            "DSH_JOINGUARD_OWNERS": owner,
            "DSH_POKE_PLATFORM": "niuniu-qq",
            "DSH_POKE_SELF_ID": environ["NAPCAT_ACCOUNT"],
            "DSH_STICKER_AUTO_RATE": "0.22" if enabled else "0",
        }
    )
    if stage:
        environ["DSH_WEB_INITIATE_SEARCH"] = "0"


def allowed_tools(config, gid, business_tools):
    only_chat = str(gid) in config.ai_chat_only_groups
    result = set() if only_chat else set(business_tools)
    if config.ai_upstream_extensions_enabled:
        result.update(CHAT_TOOLS)
        if not only_chat:
            result.update(GROUP_TOOLS)
    return result
