import ast
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

from pwcs2_bot.persona_topics import (
    TOPIC_BASE_SCORE,
    TOPIC_PATTERNS,
    TOPIC_PROFILE_GUARD,
    retire_food_defaults,
)

ROOT = Path(__file__).resolve().parents[1]


def interest_helpers():
    tree = ast.parse((ROOT / "vendor/dafeiyu/dsh-interest/main.py").read_text(encoding="utf-8"))
    names = {
        "note_group_heat",
        "hot_weight",
        "heat_snapshot",
        "maybe_swap_mood",
        "current_mood",
        "render_interest_block",
    }
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    from collections import deque

    namespace = {
        "TOPIC_PATTERNS": TOPIC_PATTERNS,
        "_GROUP_PARTS": TOPIC_PATTERNS,
        "GROUPS": {"1", "2"},
        "deque": deque,
        "time": SimpleNamespace(time=lambda: 1000),
        "_hot": {},
        "HOT_WINDOW": 300,
        "HOT_HITS": 3,
        "HOT_CAP": 1.6,
        "_state": {"mood": {"items": ["零食"]}, "last_swap": 900},
        "_save_state": lambda: None,
        "in_active_hours": lambda _: True,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "interest-helpers", "exec"), namespace)
    return namespace


def test_real_interest_is_group_scoped_and_old_mood_never_injects():
    h = interest_helpers()
    assert h["current_mood"]() == []
    assert h["render_interest_block"](1000, gid="1") == ""
    h["maybe_swap_mood"](1000)
    assert h["_state"]["mood"] == {}
    for i, text in enumerate(("CS2残局", "CS2道具", "CS2开黑")):
        h["note_group_heat"]("1", text, 1000 + i)
    assert "CS2" in h["render_interest_block"](1003, gid="1")
    assert h["render_interest_block"](1003, gid="2") == ""
    assert h["render_interest_block"](1003) == ""
    assert h["render_interest_block"](1400, gid="1") == ""
    assert h["hot_weight"]("1", "CS2", 1003) > 1


def test_repeating_one_food_message_does_not_create_a_preference():
    h = interest_helpers()
    for i in range(10):
        h["note_group_heat"]("1", "火锅", 1000 + i)
    assert h["heat_snapshot"](1010, gid="1") == []
    assert h["hot_weight"]("1", "美食", 1010) == 1
    h["note_group_heat"]("1", "推荐奶茶", 1011)
    h["note_group_heat"]("1", "夜宵吃什么", 1012)
    block = h["render_interest_block"](1013, gid="1")
    assert "本群近期正在讨论：美食" in block
    assert "馋" not in block and "固定爱好" in block


def test_food_is_one_ordinary_topic_and_not_a_forced_identity():
    assert TOPIC_BASE_SCORE == 5
    assert TOPIC_PATTERNS["美食"].search("今晚吃什么，面条还是米饭")
    assert TOPIC_PATTERNS["CS2"].search("CS2开黑")
    assert TOPIC_PATTERNS["影音"].search("看电影")
    assert "干饭" not in TOPIC_PATTERNS and "深海" not in TOPIC_PATTERNS
    assert "没有预设吃货" in TOPIC_PROFILE_GUARD
    assert "用户正在聊饮食" in TOPIC_PROFILE_GUARD


def test_first_run_model_seed_survives_astrbot_legacy_migration():
    tree = ast.parse((ROOT / "deploy/astrbot_bootstrap.py").read_text(encoding="utf-8"))
    seed = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Dict)
        and any(isinstance(key, ast.Constant) and key.value == "model_config" for key in n.keys)
    )
    config = next(
        v
        for k, v in zip(seed.keys, seed.values, strict=True)
        if isinstance(k, ast.Constant) and k.value == "model_config"
    )
    value = eval(
        compile(ast.Expression(body=config), "seed-model", "eval"),
        {"configured_chat_model": lambda: "deepseek-flash"},
    )
    # This matches migrate_provider_to_source's actual legacy model assignment.
    assert value.get("model", "") == "deepseek-flash"


def test_only_generated_food_state_is_retired_and_repeated_runs_are_noops(tmp_path):
    file = tmp_path / "dsh_interest_state.json"
    file.write_text(
        json.dumps(
            {
                "mood": {"items": ["零食"]},
                "last_swap": 100,
                "hot": {"1": {"CS2": 1.6, "干饭": 1.5, "美食": 1.2}},
                "other_state": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "dsh_desire.db"
    with sqlite3.connect(db) as c:
        c.executescript(
            "CREATE TABLE drive_state(group_id TEXT,drive TEXT,intensity REAL,"
            "baseline REAL,phase TEXT,last_signal TEXT,updated_at REAL);"
            "INSERT INTO drive_state VALUES('1','appetite',99,14,'active','food_topic',10);"
            "INSERT INTO drive_state VALUES('1','curiosity',65,18,'active','question',10);"
            "INSERT INTO drive_state VALUES('2','appetite',95,14,'active','food_topic',10);"
            "CREATE TABLE memories(value TEXT);INSERT INTO memories VALUES('keep');"
        )
    result = retire_food_defaults(tmp_path, ["1"], now=500)
    assert result == {"interest_state": True, "appetite_rows": 1}
    state = json.loads(file.read_text(encoding="utf-8"))
    assert state["mood"] == {} and state["other_state"] == {"keep": True}
    assert state["hot"]["1"] == {"CS2": 1.6, "美食": 1.2}
    with sqlite3.connect(db) as c:
        assert c.execute(
            "SELECT intensity,baseline,phase FROM drive_state "
            "WHERE group_id='1' AND drive='appetite'"
        ).fetchone() == (0, 0, "latent")
        assert (
            c.execute("SELECT intensity FROM drive_state WHERE drive='curiosity'").fetchone()[0]
            == 65
        )
        assert c.execute("SELECT intensity FROM drive_state WHERE group_id='2'").fetchone()[0] == 95
        assert c.execute("SELECT value FROM memories").fetchone()[0] == "keep"
    assert retire_food_defaults(tmp_path, ["1"], now=600) == {
        "interest_state": False,
        "appetite_rows": 0,
    }


def test_legacy_high_appetite_cannot_reenter_model_guidance():
    spec = importlib.util.spec_from_file_location(
        "neutral_desire_logic", ROOT / "vendor/dafeiyu/dsh-desire/desire_logic.py"
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    legacy = {
        **m.default_state("appetite", 1000),
        "intensity": 100,
        "baseline": 90,
        "phase": "active",
    }
    assert m.decay(legacy, 1000)["intensity"] == 0
    assert m.choose_primary([legacy], 1000) is None
    assert m.render_block(legacy) == ""
    assert not any(s.drive == "appetite" for s in m.classify("好饿，今晚火锅零食奶茶吃什么", False))
    assert any(s.drive == "curiosity" for s in m.classify("为什么会这样", False))


def test_proactive_context_is_neutral_not_whale_or_food_template():
    tree = ast.parse((ROOT / "vendor/dafeiyu/dsh-ctxclean/main.py").read_text(encoding="utf-8"))
    value = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_PROACTIVE" for t in n.targets)
    )
    assert not any(word in value for word in ("白米饭", "吃的", "深海", "馋"))
    assert "正在讨论" in value
