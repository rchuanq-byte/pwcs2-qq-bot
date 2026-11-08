"""Regression for real AstrBot's early random-reply ProviderRequest path."""

import ast
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pwcs2_bot.astrbot_runtime import (
    NIUNIU_ROUTE_PRIORITY,
    automatic_turn_claimed,
    defer_framework_auto_reply,
)
from pwcs2_bot.chat_profiles import mention_only
from pwcs2_bot.dafeiyu_persona import is_synthetic

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "astrbot_plugins/astrbot_plugin_niuniu/main.py"


class Event:
    def __init__(self, **extra):
        self.extra = extra
        self.stopped = False
        self._has_send_oper = False

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_group_id(self):
        return "100000021"

    def stop_event(self):
        self.stopped = True


def handler(module, name):
    return SimpleNamespace(handler_module_path=module, handler_name=name)


def plugin_method(name, **namespace):
    """Run the actual hook body without requiring a local AstrBot installation."""
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = copy.deepcopy(next(node for node in cls.body if getattr(node, "name", "") == name))
    method.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace["AstrMessageEvent"] = Event
    namespace.setdefault("mention_only", mention_only)
    exec(compile(module, str(PLUGIN), "exec"), namespace)
    return namespace[name]


def test_route_precedes_builtin_llm_and_collectors_run_before_random_reply():
    assert NIUNIU_ROUTE_PRIORITY == sys.maxsize
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    route = next(node for node in ast.walk(tree) if getattr(node, "name", "") == "route")
    assert any(
        kw.arg == "priority"
        and isinstance(kw.value, ast.Name)
        and kw.value.id == "NIUNIU_ROUTE_PRIORITY"
        for dec in route.decorator_list
        for kw in dec.keywords
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "defer_framework_auto_reply"
        for node in ast.walk(route)
    )
    route_handler = handler("niuniu", "route")
    builtin = handler("astrbot.builtin_stars.astrbot.main", "on_message")
    persist = handler("astrbot.builtin_stars.astrbot.main", "persist_group_message")
    memory = handler("dsh-memory", "collect")
    interest = handler("dsh-proactive", "collect")
    unrelated = handler("other-plugin", "on_message")
    handlers = [route_handler, persist, builtin, memory, interest, unrelated]
    event = Event(activated_handlers=handlers)
    seen = []
    # Match StarRequestSubStage: its iterator is already running on this list.
    for current in handlers:
        seen.append(current)
        if current is route_handler:
            defer_framework_auto_reply(event)
    assert event.get_extra("activated_handlers") is handlers
    assert seen == [route_handler, persist, memory, interest, unrelated, builtin]
    defer_framework_auto_reply(event)
    assert handlers == seen  # Stable and idempotent; no duplicates/dropped collectors.
    defer_framework_auto_reply(Event())


@pytest.mark.parametrize("claim", [{"block_proactive": True}, {"already_replied": True}])
async def test_claimed_input_cannot_spawn_a_second_random_reply(claim):
    event = Event(niuniu_ai=True, **{"dsh.link.v1.turn_claim": {"version": 1, **claim}})
    await run_gate(event, expect_observed=False)
    assert event.stopped and event.get_extra("niuniu_suppress")


async def run_gate(event, *, expect_observed=True, reserve=True):
    observed, reservations = [], []

    async def observe(current, request):
        observed.append(current)

    def reserve_turn(engine, gid):
        reservations.append(gid)
        return reserve

    main = SimpleNamespace(
        engine=SimpleNamespace(
            companion=SimpleNamespace(
                release=lambda gid: None, _state=lambda gid: SimpleNamespace(revision=7)
            )
        ),
        automatic_requests=0,
        _observe_images=observe,
    )
    gate = plugin_method(
        "gate_request",
        reserve_automatic_turn=reserve_turn,
        automatic_turn_claimed=automatic_turn_claimed,
        is_synthetic=is_synthetic,
    )
    await gate(main, event, SimpleNamespace())
    assert bool(observed) is expect_observed
    return main, gate, reservations


async def test_non_at_authorized_turn_reaches_model_and_counts_once():
    event = Event(niuniu_ai=True)  # No @/wake name/command/explicit flag.
    main, gate, reservations = await run_gate(event)
    assert not event.stopped and reservations == ["100000021"]
    assert event.get_extra("niuniu_auto_reserved") and event.get_extra("niuniu_revision") == 7
    assert main.automatic_requests == 1
    await gate(main, event, SimpleNamespace())
    assert main.automatic_requests == 1


async def test_synthetic_turn_can_use_its_own_claim_but_not_bypass_quiet():
    extra = {"version": 1, "owner": "dsh-proactive", "block_proactive": True}
    event = Event(niuniu_ai=True, dsh_proactive=True, **{"dsh.link.v1.turn_claim": extra})
    await run_gate(event)
    assert not event.stopped
    quiet = Event(niuniu_ai=True, dsh_proactive=True)
    await run_gate(quiet, reserve=False, expect_observed=False)
    assert quiet.stopped


async def test_real_send_blocks_duplicate_but_explicit_questions_are_not_lost():
    event = Event(niuniu_ai=True)
    event._has_send_oper = True
    await run_gate(event, expect_observed=False)
    explicit = Event(niuniu_ai=True, niuniu_explicit=True)
    explicit._has_send_oper = True
    main, _, reservations = await run_gate(explicit)
    assert not explicit.stopped and not reservations and main.automatic_requests == 0


@pytest.mark.parametrize("extra", [{}, {"niuniu_ai": True, "niuniu_notice_only": True}])
async def test_unrouted_and_notice_events_stay_blocked(extra):
    event = Event(**extra)
    _, _, reservations = await run_gate(event, expect_observed=False)
    assert event.stopped and not reservations


async def test_sent_metric_counts_actual_automatic_send_only_once():
    async def sent(event):
        pass

    main = SimpleNamespace(automatic_replies_sent=0, parallel=SimpleNamespace(sent=sent))
    remember = plugin_method("remember_sent")
    for event in (Event(niuniu_explicit=True), Event(niuniu_ai=True, niuniu_suppress=True)):
        await remember(main, event)
    assert main.automatic_replies_sent == 0
    event = Event(niuniu_ai=True, niuniu_auto_reserved=True)
    await remember(main, event)
    await remember(main, event)
    assert main.automatic_replies_sent == 1
