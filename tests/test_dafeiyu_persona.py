import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pwcs2_bot.dafeiyu_persona import (
    CHAT_EXTENSION_COMMANDS,
    EXTENSION_MODULES,
    GROUP_EXTENSION_COMMANDS,
    NOT_PERSONALITY_MODULES,
    PERSONA_MODULES,
    PUBLIC_COMMANDS,
    REPLACED_MODULES,
    configure_persona_suite,
    conversational_output,
    factual_turn,
    is_synthetic,
    load_persona_commands,
    requires_operator,
)

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor/dafeiyu"


class Event:
    def __init__(self, text="聊聊", **extra):
        self.message_str = text
        self.extra = extra
        self.result = SimpleNamespace(
            is_model_result=lambda: True, get_plain_text=lambda: "自然聊天"
        )

    def get_extra(self, name, default=None):
        return self.extra.get(name, default)

    def get_result(self):
        return self.result


def test_complete_catalogue_and_source_not_placeholders():
    assert len(PERSONA_MODULES) == 41
    all_modules = (
        set(PERSONA_MODULES)
        | set(EXTENSION_MODULES)
        | set(REPLACED_MODULES)
        | NOT_PERSONALITY_MODULES
    )
    assert len(all_modules) == 59
    for name in PERSONA_MODULES:
        path = VENDOR / f"dsh-{name}"
        assert (path / "metadata.yaml").exists()
        tree = ast.parse((path / "main.py").read_text(encoding="utf-8-sig"))
        assert any(isinstance(node, ast.ClassDef) for node in tree.body)
        assert not any(
            isinstance(node, ast.Attribute) and node.attr == "llm_tool" for node in ast.walk(tree)
        )


@pytest.mark.parametrize("value", ["", "   ", "deepseek-flash"])
def test_bootstrap_never_passes_empty_model_to_api(monkeypatch, value):
    spec = importlib.util.spec_from_file_location(
        "niuniu_bootstrap_test", ROOT / "deploy/astrbot_bootstrap.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("USTC_LLM_MODEL", value)
    assert module.configured_chat_model() == "deepseek-flash"


def test_activation_uses_real_group_operator_and_live_growth():
    env = {"NAPCAT_ACCOUNT": "100000031"}
    configure_persona_suite(env, ["100000021"], owner="100000011")
    for name in PERSONA_MODULES:
        prefix = "DSH_" + name.upper().replace("-", "_")
        assert env[prefix] == "1"
        assert env[prefix + "_GROUPS"] == "100000021"
        assert env[prefix + "_OWNER"] == "100000011"
        assert env[prefix + "_SHADOW"] == "0"
    assert env["DSH_MIND_MODE"] == "observe"
    assert env["DSH_SLANG_AUTO"] == "1"
    assert env["DSH_PROACTIVE_SELF_ID"] == "100000031"
    for name in ("DECIDE", "CLARIFY", "INITIATE"):
        assert env[f"DSH_{name}_TIMEOUT"] == "120"
    assert "DSH_PROACTIVE_MIN_INTERVAL_MS" not in env  # Keep upstream numeric defaults.
    for key in ("DSH_INITIATE_IDLE", "DSH_INITIATE_COOLDOWN", "DSH_INITIATE_DAY_MAX"):
        assert key not in env
    configure_persona_suite(env, ["100000021"], stage=True)
    for name in ("INITIATE", "PROACTIVE", "EFFECT"):
        assert env["DSH_" + name] == "0"
    assert env["DSH_SLANG_AUTO"] == env["DSH_SLANG_RESEARCH"] == "0"


def test_all_new_commands_recognized_and_owner_only_settings():
    commands = load_persona_commands(VENDOR)
    assert {
        "人格成长",
        "黑话确认",
        "馋什么",
        "情绪状态",
        "自主状态",
        "心智状态",
        "脾气",
    } <= commands
    assert "档案记住" in commands and "记住" not in commands
    assert all(not requires_operator(c) for c in PUBLIC_COMMANDS)
    assert all(
        requires_operator(c)
        for c in commands - PUBLIC_COMMANDS - CHAT_EXTENSION_COMMANDS - GROUP_EXTENSION_COMMANDS
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"niuniu_explicit": True},
        {"niuniu_tool_count": 1},
        {"niuniu_ai": False},
    ],
)
def test_style_never_corrupts_explicit_or_tool_answers(monkeypatch, extra):
    monkeypatch.setenv("NIUNIU_PERSONA_SUITE", "1")
    settings = {"niuniu_ai": True, **extra}
    assert not conversational_output(Event(**settings))


def test_casual_style_is_active_but_factual_tasks_are_protected(monkeypatch):
    monkeypatch.setenv("NIUNIU_PERSONA_SUITE", "1")
    assert conversational_output(Event(niuniu_ai=True))
    assert factual_turn(Event("帮我查昨天战绩"))
    assert not factual_turn(Event("笑死，今天的梗有点多"))
    assert is_synthetic(Event(niuniu_merged=True))


def test_identity_and_capability_are_truthful_not_whale_sample():
    tree = ast.parse((VENDOR / "dsh-factguard/main.py").read_text(encoding="utf-8-sig"))
    facts = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_DEFAULT_FACTS" for t in n.targets)
    )
    assert facts["name"] == "牛牛精灵"
    assert facts["birthday"] == "未设定" and facts["gender"].startswith("未设定")
    assert "美国" not in facts["home"] and "管理员" not in facts["group_role"]
    assert json.loads((VENDOR / "dsh-homophone/homophone_data.json").read_text(encoding="utf-8"))[
        "names"
    ]


def test_claim_evidence_survives_fixed_system_and_merge_uses_full_pipeline():
    source = (VENDOR / "dsh-claimguard/main.py").read_text(encoding="utf-8")
    assert "req.system_prompt =" not in source
    assert "req.extra_user_content_parts.append(TextPart(text=block))" in source
    merge = (VENDOR / "dsh-merge/main.py").read_text(encoding="utf-8")
    assert merge.index('getattr(star_ctx, "niuniu_merge_submit"') < merge.index(
        "star_ctx.llm_generate("
    )
    assert merge.count("if factual_turn(event) or event.get_extra(") == 2
    assert "@filter.after_message_sent(priority=-900)" in (
        VENDOR / "dsh-followup/main.py"
    ).read_text(encoding="utf-8")
