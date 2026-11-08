import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "runtime_guard", Path(__file__).parents[1] / "deploy" / "runtime_guard.py"
)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def sample(ratio=0.90, oom=0):
    return {"used_ratio": ratio, "oom_kills": oom}


def test_memory_subtracts_reclaimable_cache_but_includes_swap(tmp_path):
    for name, content in {
        "memory.current": "580", "memory.max": "600", "memory.swap.current": "150",
        "memory.swap.max": "200", "memory.stat": "inactive_file 180\nanon 390\n",
        "memory.events": "oom 3\noom_kill 2\n",
    }.items():
        (tmp_path / name).write_text(content)
    result = guard.memory_sample(tmp_path)
    assert result["working_bytes"] == 400
    assert result["used_ratio"] == 550 / 800
    assert result["oom_kills"] == 2
    (tmp_path / "memory.max").write_text("max")
    with pytest.raises(ValueError, match="finite"):
        guard.memory_sample(tmp_path)


def test_high_memory_requires_five_consecutive_samples_and_recovers():
    previous = {}
    for i in range(5):
        previous = guard.evaluate(
            previous, container_id="abc", sample=sample(), qq_state="online", now=1000 + i * 60,
        )
        assert previous["mode"] == "observe_only"
        assert previous["high_cycles"] == i + 1
        assert bool(previous["alarms"]) == (i == 4)
    assert previous["alarms"] == ["napcat_sustained_high_memory"]
    recovered = guard.evaluate(
        previous, container_id="abc", sample=sample(0.2), qq_state="online", now=1300,
    )
    assert recovered["alarms"] == []
    assert recovered["high_cycles"] == 0


@pytest.mark.parametrize("container_id,now", [("new", 1300), ("abc", 1500), ("abc", 900)])
def test_missing_cycles_new_container_or_clock_change_resets_streak(container_id, now):
    previous = {"container_id": "abc", "high_cycles": 4, "checked_at": 1240}
    state = guard.evaluate(
        previous, container_id=container_id, sample=sample(), qq_state="online", now=now,
    )
    assert state["high_cycles"] == 1
    assert state["alarms"] == []


def test_oom_delta_and_qq_login_have_read_only_alerts():
    previous = {"container_id": "abc", "oom_kills": 1}
    state = guard.evaluate(
        previous, container_id="abc", sample=sample(0.2, 2),
        qq_state="login_required", now=1000,
    )
    assert state["alarms"] == ["qq_login_required", "napcat_oom_kill_detected"]
    state = guard.evaluate(
        state, container_id="abc", sample=sample(0.2, 2), qq_state="online", now=1060,
    )
    assert state["alarms"] == []


def test_corrupt_old_counters_do_not_prevent_fresh_probe():
    state = guard.evaluate(
        {"container_id": "abc", "checked_at": "bad", "high_cycles": None, "oom_kills": []},
        container_id="abc", sample=sample(), qq_state="online", now=1000,
    )
    assert state["high_cycles"] == 1
    assert state["alarms"] == []
    assert guard.safe_number(float("nan")) == 0
    assert guard.safe_number(True) == 0


def test_qq_probe_does_not_leak_upstream_response_or_invoke_restart(monkeypatch):
    commands = []

    def command(args):
        commands.append(args)
        return "unexpected secret response"

    monkeypatch.setattr(guard, "command", command)
    assert guard.qq_health("test-bot") == "unknown"
    assert commands[0][:5] == ["docker", "exec", "test-bot", "python", "-c"]
    assert "read_problem" in commands[0][5]
    assert all(word not in commands[0] for word in ("restart", "stop", "kill"))


def test_snapshot_writes_atomically_without_temporary_files(tmp_path):
    path = tmp_path / "state" / "status.json"
    guard.atomic_json(path, {"version": 1, "mode": "observe_only"})
    assert json.loads(path.read_text())["mode"] == "observe_only"
    assert list(path.parent.iterdir()) == [path]


def history_state(now=1000, *, qq="online", container_id="a" * 64, alarms=None):
    return {
        "version": 1, "mode": "observe_only", "checked_at": now,
        "container_id": container_id, "container_status": "running",
        "container_started_at": "2026-10-21T00:00:00Z", "container_restart_count": 0,
        "qq_state": qq, "alarms": alarms or [], "working_bytes": 400,
        "swap_bytes": 100, "total_limit_bytes": 800, "used_ratio": 0.625,
        "oom_kills": 0, "high_cycles": 0,
    }


def history_events(directory):
    return [json.loads(line) for line in (directory / "history.jsonl").read_text().splitlines()]


def test_history_records_only_transitions_and_hourly_heartbeat(tmp_path):
    assert guard.append_history(tmp_path, history_state())
    assert history_events(tmp_path)[0]["reasons"] == ["first_sample"]
    # Resource fluctuations and ordinary per-minute checks do not flood history.
    assert not guard.append_history(
        tmp_path, {**history_state(1060), "working_bytes": 500, "used_ratio": 0.75},
    )
    assert not guard.append_history(tmp_path, history_state(4599))
    assert guard.append_history(tmp_path, history_state(4600))
    assert history_events(tmp_path)[-1]["reasons"] == ["heartbeat"]
    assert guard.append_history(tmp_path, history_state(4660, qq="login_required"))
    assert history_events(tmp_path)[-1]["reasons"] == ["qq_state_changed"]
    assert guard.append_history(
        tmp_path, history_state(4720, qq="login_required", alarms=["qq_login_required"]),
    )
    assert history_events(tmp_path)[-1]["reasons"] == ["alarms_changed"]
    assert len(history_events(tmp_path)) == 4


def test_history_keeps_container_replacement_and_restart_evidence(tmp_path):
    guard.append_history(tmp_path, history_state())
    assert guard.append_history(tmp_path, history_state(1060, container_id="b" * 64))
    assert history_events(tmp_path)[-1]["reasons"] == ["container_changed"]
    restarted = {
        **history_state(1120, container_id="b" * 64),
        "container_started_at": "2026-10-21T00:05:00Z", "container_restart_count": 1,
    }
    assert guard.append_history(tmp_path, restarted)
    assert history_events(tmp_path)[-1]["reasons"] == [
        "container_started", "container_restart_count_changed",
    ]
    assert guard.append_history(tmp_path, {**restarted, "checked_at": 1180,
                                          "container_status": "exited"})
    assert history_events(tmp_path)[-1]["reasons"] == ["container_status_changed"]


def test_history_uses_a_strict_allowlist_and_does_not_leak_secrets(tmp_path):
    secret = "SENSITIVE_CHAT_ACCOUNT_TOKEN_ENV"
    state = {
        **history_state(), "chat": secret, "account": secret, "token": secret,
        "env": {"PASSWORD": secret}, "loginError": secret, "mode": secret,
        "container_id": secret, "container_started_at": secret, "container_status": secret,
        "qq_state": secret, "alarms": [secret, {"raw": secret}, "qq_login_required"],
        "working_bytes": secret, "used_ratio": float("nan"),
    }
    assert guard.append_history(tmp_path, state)
    text = (tmp_path / "history.jsonl").read_text()
    assert secret not in text
    event = history_events(tmp_path)[0]
    assert event["mode"] == "observe_only"
    assert event["qq_state"] == "unknown"
    assert event["container_id"] is None
    assert event["container_started_at"] is None
    assert event["alarms"] == ["qq_login_required", "unrecognized_alarm"]
    assert event["working_bytes"] == 0
    assert event["used_ratio"] == 0
    assert set(event).isdisjoint({"chat", "account", "token", "env", "loginError"})


def test_history_rotates_with_a_fixed_total_capacity(tmp_path):
    for index in range(20):
        assert guard.append_history(
            tmp_path,
            history_state(1000 + index * 60, qq="online" if index % 2 else "login_required"),
            max_bytes=1300, backups=2,
        )
    files = sorted(tmp_path.glob("history*.jsonl"))
    assert {path.name for path in files} == {"history.jsonl", "history.1.jsonl", "history.2.jsonl"}
    assert all(path.stat().st_size <= 1300 for path in files)
    assert sum(path.stat().st_size for path in files) <= 3900
    for path in files:
        assert path.read_bytes().endswith(b"\n")
        assert all(
            json.loads(line)["mode"] == "observe_only" for line in path.read_text().splitlines()
        )
    assert history_events(tmp_path)[-1]["checked_at"] == 2140


@pytest.mark.parametrize("old_tail", ["broken history\n", '{"interrupted":'])
def test_history_recovers_from_invalid_tail_and_clock_change(tmp_path, old_tail):
    (tmp_path / "history.jsonl").write_text(old_tail)
    assert guard.append_history(tmp_path, history_state())
    # Only the old invalid line is ignored; the new event is valid and durable.
    assert guard._last_history_event(tmp_path / "history.jsonl")["qq_state"] == "online"
    assert guard.append_history(tmp_path, history_state(900))
    assert guard._last_history_event(tmp_path / "history.jsonl")["reasons"] == ["clock_changed"]


def test_history_write_failure_does_not_break_main_probe(tmp_path, monkeypatch, capsys):
    calls = []

    def command(args):
        calls.append(args)
        return f"{'a' * 64} running 2026-10-21T00:00:00Z 0"

    def failing_history(*args, **kwargs):
        raise OSError("SECRET credential in underlying error")

    monkeypatch.setattr(guard, "command", command)
    monkeypatch.setattr(guard, "memory_sample", lambda root: sample(0.2))
    monkeypatch.setattr(guard, "qq_health", lambda bot: "online")
    monkeypatch.setattr(guard, "append_history", failing_history)
    monkeypatch.setattr(sys, "argv", ["runtime_guard.py", "--state-dir", str(tmp_path)])
    monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None,
    ))

    assert guard.main() == 0

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["qq_state"] == "online"
    assert status["mode"] == "observe_only"
    assert status["alarms"] == []
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert '"history_error":"write_unavailable"' in output
    assert all(args[:2] == ["docker", "inspect"] for args in calls)
    assert all(word not in args for args in calls for word in ("restart", "stop", "kill"))


def test_failed_history_write_is_retried_from_last_saved_event(tmp_path, monkeypatch):
    guard.append_history(tmp_path, history_state())
    original_open = guard.os.open
    changed = history_state(1060, qq="login_required")

    def fail_open(*args, **kwargs):
        raise PermissionError("history inaccessible")

    monkeypatch.setattr(guard.os, "open", fail_open)
    guard.record_history_safely(tmp_path, changed)
    assert len(history_events(tmp_path)) == 1
    monkeypatch.setattr(guard.os, "open", original_open)
    assert guard.append_history(tmp_path, {**changed, "checked_at": 1120})
    assert history_events(tmp_path)[-1]["reasons"] == ["qq_state_changed"]
