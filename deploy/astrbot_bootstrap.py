"""Initialize private AstrBot state, then launch the unmodified pinned core.

Secrets are read from the existing private environment, never printed. Existing
state is preserved; subsequent starts do not reset credentials or conversation data.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path


def private_json(path, value):
    temporary = path.with_suffix(".new")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.chmod(0o600)
    temporary.replace(path)


def configured_chat_model():
    # An empty .env entry is not a request to send model="" to the provider.
    return (os.environ.get("USTC_LLM_MODEL") or "").strip() or "deepseek-flash"


def configure():
    os.environ.setdefault("TZ", "Asia/Shanghai")
    # Importing astrbot.core initializes its global config and may create a
    # default cmd_config.json. Capture first-run state BEFORE that side effect.
    root = Path("/AstrBot/data")
    root.mkdir(parents=True, exist_ok=True)
    target = root / "cmd_config.json"
    first_start = not target.exists()

    from astrbot.core.config.default import DEFAULT_CONFIG

    from pwcs2_bot.astrbot_runtime import (
        configure_conversation_frequency,
        load_business_settings,
    )
    from pwcs2_bot.chat_profiles import automatic_groups
    from pwcs2_bot.dafeiyu_persona import configure_persona_suite
    from pwcs2_bot.deployment_identity import operator_qq
    from pwcs2_bot.upstream_extensions import configure_extensions

    business = load_business_settings(
        Path(os.environ.get("NIUNIU_BUSINESS_CONFIG", "/config/config.json"))
    )
    business_groups = business["allowedGroups"]
    groups = list(dict.fromkeys([*business_groups, *business.get("aiChatOnlyGroups", [])]))
    if not groups or any(
        not str(g).isascii() or not str(g).isdecimal() or int(g) <= 0 for g in groups
    ):
        raise ValueError("Configure at least one valid QQ group in the local config/config.json")
    owner = operator_qq()
    auto_groups = automatic_groups(business)
    if first_start:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config.update(
            {
                "admins_id": [owner] if owner else [],
                "wake_prefix": ["/", "#"],
                "platform": [
                    {
                        "id": "niuniu-qq",
                        "type": "aiocqhttp",
                        "enable": True,
                        "ws_reverse_host": "0.0.0.0",
                        "ws_reverse_port": 6199,
                        "ws_reverse_token": os.environ["NAPCAT_ONEBOT_TOKEN"],
                    }
                ],
                "http_proxy": "",
                "log_level": "INFO",
            }
        )
        config["platform_settings"].update(
            {
                "unique_session": False,
                "ignore_bot_self_message": True,
                "id_whitelist": [f"niuniu-qq:GroupMessage:{gid}" for gid in groups],
                "wl_ignore_admin_on_group": False,
                "wl_ignore_admin_on_friend": False,
                "no_permission_reply": False,
            }
        )
        config["provider"] = [
            {
                "id": "niuniu-campus",
                "type": "openai_chat_completion",
                "enable": True,
                "provider_type": "chat_completion",
                "key": ["$USTC_LLM_API_KEY"],
                "api_base": business.get("aiBaseUrl", "https://api.llm.ustc.edu.cn/v1"),
                "model": configured_chat_model(),
                "proxy": os.environ.get("USTC_LLM_PROXY_URL", ""),
                "timeout": None,
                "modalities": ["text", "image", "tool_use"],
                # AstrBot's legacy migration takes model from this object and
                # otherwise overwrites the non-empty top-level model with "".
                "model_config": {"model": configured_chat_model()},
            }
        ]
        config["provider_settings"].update(
            {
                "enable": True,
                "web_search": False,
                "streaming_response": False,
                "computer_use_runtime": "none",
                "proactive_capability": {"add_cron_tools": False},
                "max_quoted_fallback_images": 0,
                "identifier": False,
                "quoted_message_parser": {
                    "max_component_chain_depth": 1,
                    "max_forward_node_depth": 0,
                    "max_forward_fetch": 0,
                    "warn_on_action_failure": False,
                },
            }
        )
        runner = config["agent_runner"]["config"]
        runner["model"].update({"provider_id": "niuniu-campus", "request_max_retries": 1})
        runner["compression"].update(
            {"max_turns": 12, "trim_turns": 2, "overflow_strategy": "truncate_by_turns"}
        )
        runner["misc"].update({"max_steps": 8, "tool_call_timeout": 45})
        config["provider_ltm_settings"].update(
            {
                "group_icl_enable": False,
                "group_message_history_enable": False,
                "active_reply": {
                    "enable": True,
                    "method": "possibility_reply",
                    "possibility_reply": 0.12,
                    "whitelist": [f"niuniu-qq:GroupMessage:{gid}" for gid in auto_groups],
                },
            }
        )
        password = secrets.token_urlsafe(24)
        credential_file = root / ".dashboard-password"
        credential_file.write_text(password, encoding="utf-8")
        credential_file.chmod(0o600)
        config["dashboard"].update(
            {
                "host": "0.0.0.0",
                "port": 6185,
                "username": "niuniu",
                "password": hashlib.md5(password.encode()).hexdigest(),
                "jwt_secret": secrets.token_urlsafe(48),
            }
        )
        private_json(target, config)
    else:
        config = json.loads(target.read_text(encoding="utf-8-sig"))
        if not any(p.get("id") == "niuniu-qq" for p in config.get("platform", [])):
            raise RuntimeError("Existing AstrBot config is not the managed Niuniu configuration")
        for provider in config.get("provider", []):
            if provider.get("id") == "niuniu-campus":
                provider.update(
                    {
                        "key": ["$USTC_LLM_API_KEY"],
                        "model": configured_chat_model(),
                        "proxy": os.environ.get("USTC_LLM_PROXY_URL", ""),
                        "timeout": None,
                    }
                )
        for platform in config["platform"]:
            if platform.get("id") == "niuniu-qq":
                platform["ws_reverse_token"] = os.environ["NAPCAT_ONEBOT_TOKEN"]
        config["platform_settings"]["id_whitelist"] = [
            f"niuniu-qq:GroupMessage:{gid}" for gid in groups
        ]
        config["provider_ltm_settings"]["active_reply"]["whitelist"] = [
            f"niuniu-qq:GroupMessage:{gid}" for gid in auto_groups
        ]
        private_json(target, config)
    if business.get("aiUpstreamExtensionsEnabled"):
        misc = config["agent_runner"]["config"]["misc"]
        misc["tool_call_timeout"] = max(180, misc.get("tool_call_timeout") or 0)
        private_json(target, config)
    from pwcs2_bot.persona_topics import retire_food_defaults

    retire_food_defaults(root, groups)
    plugins = root / "plugins"
    plugins.mkdir(exist_ok=True)
    for item in Path("/opt/niuniu-plugins").iterdir():
        destination = plugins / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                destination,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "tests", "test_*.py", "*.json"),
            )
            # Seed dictionaries once; never overwrite learned state or operator
            # edits in the persistent plugin volume on the next start.
            for seed in item.rglob("*.json"):
                target_seed = destination / seed.relative_to(item)
                if not target_seed.exists():
                    target_seed.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(seed, target_seed)
        elif item.suffix == ".py":
            shutil.copy2(item, destination)
    if managed_source := os.environ.get("NIUNIU_MANAGED_PLUGIN_SOURCE"):
        shutil.copytree(managed_source, plugins / "astrbot_plugin_niuniu", dirs_exist_ok=True)
    # All upstream examples are replaced by the actual group and operator.
    joined = ",".join(groups)
    configure_persona_suite(
        os.environ, groups, owner=owner, stage=os.environ.get("NIUNIU_STAGE", "0") == "1"
    )
    # Mention-only turns remain independent; never queue a synthetic follow-up
    # without its real @ event. A non-empty sentinel means no groups, not all.
    os.environ["DSH_MERGE_GROUPS"] = ",".join(auto_groups) or "__none__"
    os.environ["NIUNIU_PERSONA_SUITE"] = "1"
    for module in ("MEM_PROFILE", "PERSONA_CORE", "SOCIAL", "SCENE", "PROACTIVE", "INITIATE"):
        os.environ[f"DSH_{module}_GROUPS"] = joined
    for module in ("PERSONA_CORE", "SOCIAL", "SCENE", "PROACTIVE", "INITIATE"):
        os.environ[f"DSH_{module}_OWNER"] = owner
    for module in ("PROACTIVE", "INITIATE"):
        # No autonomous cold-open jobs in chat-only groups; ordinary replies
        # remain event-driven. Existing business-group jobs keep their scope.
        os.environ[f"DSH_{module}_GROUPS"] = ",".join(business_groups)
        os.environ[f"DSH_{module}_PLATFORM"] = "niuniu-qq"
        os.environ[f"DSH_{module}_SELF_ID"] = os.environ["NAPCAT_ACCOUNT"]
        os.environ[f"DSH_{module}_SHADOW"] = "0"
    os.environ.update(
        {
            "DSH_MEM_SELF_NAMES": "牛牛精灵,牛牛",
            "DSH_SOCIAL_MODE": "style",
            "DSH_PERSONA_CORE_SHADOW": "0",
            "DSH_DECIDE_PROVIDER": "niuniu-campus",
            "DSH_SLEEP_SLOW_MIN": "0",
            "DSH_SLEEP_SLOW_MAX": "0",
            "DSH_MEM_DAILY_CAP": "80",
            "DSH_SCENE_ABOUT": "你是牛牛精灵，和当前群的群友自然聊天。各群的记忆和功能相互隔离，"
            "只有明确开放业务功能的群才可使用本群只读统计，不得猜测或串用其他群的数据。",
            "NIUNIU_QUIET_UNTIL": str(business.get("automaticQuietUntil", "")),
        }
    )
    configure_conversation_frequency(
        os.environ, upstream_defaults=bool(business.get("aiCompanionUseUpstreamLimits", False))
    )
    if os.environ.get("NIUNIU_STAGE", "0") == "1":
        os.environ["DSH_INITIATE"] = "0"
        os.environ["DSH_PROACTIVE"] = "0"
        os.environ["DSH_MEM_DAILY_CAP"] = "0"
    configure_extensions(os.environ, business, stage=os.environ.get("NIUNIU_STAGE") == "1")
    if business.get("aiUpstreamExtensionsEnabled"):
        from pwcs2_bot.keyword_reaction import xh_reaction_image_path

        sticker = root / "stickers" / "颉颃胜负" / "seed.png"
        image = xh_reaction_image_path()
        if image.is_file() and not sticker.exists():
            sticker.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(image, sticker)


if __name__ == "__main__":
    configure()
    os.execv("/usr/local/bin/python", ["python", "/AstrBot/main.py"])
