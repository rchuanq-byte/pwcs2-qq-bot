#!/usr/bin/env python3
"""Observe QQ container pressure and bot readiness. NEVER restart any service."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HISTORY_MAX_BYTES = 1024 * 1024
HISTORY_BACKUPS = 3
HISTORY_HEARTBEAT_SECONDS = 3600
QQ_STATES = frozenset({
    "online", "starting", "stopped", "disconnected", "reconnecting",
    "login_required", "degraded", "unresponsive", "stale_snapshot",
    "invalid_snapshot", "missing_or_invalid_snapshot", "probe_unavailable", "unknown",
})
CONTAINER_STATES = frozenset({
    "created", "running", "paused", "restarting", "removing", "exited", "dead", "unknown",
})
HISTORY_ALARMS = frozenset({
    *("qq_" + state for state in QQ_STATES),
    "napcat_sustained_high_memory", "napcat_oom_kill_detected",
    "runtime_probe_unavailable", "unrecognized_alarm",
})


def safe_number(value: Any, default: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value) if math.isfinite(value) else default


def memory_sample(root: Path) -> dict[str, int | float]:
    def number(name: str) -> int:
        raw = (root / name).read_text().strip()
        if not raw.isdigit():
            raise ValueError("finite cgroup v2 memory limits required")
        return int(raw)

    stats = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
    events = dict(line.split() for line in (root / "memory.events").read_text().splitlines())
    current, limit = number("memory.current"), number("memory.max")
    swap, swap_limit = number("memory.swap.current"), number("memory.swap.max")
    # Reclaimable file cache is not a leak and must not trigger a high-memory
    # alert. Include swap: QQ can exhaust RAM+swap while its RSS looks modest.
    working = max(0, current - int(stats.get("inactive_file", 0)))
    total_limit = limit + swap_limit
    if total_limit <= 0:
        raise ValueError("invalid memory limit")
    return {
        "working_bytes": working,
        "swap_bytes": swap,
        "total_limit_bytes": total_limit,
        "used_ratio": (working + swap) / total_limit,
        "oom_kills": int(events.get("oom_kill", 0)),
    }


def evaluate(
    previous: dict[str, Any], *, container_id: str, sample: dict[str, Any],
    qq_state: str, now: float,
) -> dict[str, Any]:
    # Require consecutive recent samples, not five sporadic checks over a week.
    consecutive = (
        previous.get("container_id") == container_id
        and 0 <= now - safe_number(previous.get("checked_at")) <= 150
    )
    high = float(sample["used_ratio"]) >= 0.88
    cycles = max(0, int(safe_number(previous.get("high_cycles")))) if consecutive else 0
    cycles = min(cycles + 1, 5) if high else 0
    previous_oom = int(safe_number(previous.get("oom_kills"), sample["oom_kills"]))
    new_oom = previous.get("container_id") == container_id and sample["oom_kills"] > previous_oom
    alarms = []
    if qq_state != "online":
        alarms.append("qq_" + qq_state)
    if cycles >= 5:
        alarms.append("napcat_sustained_high_memory")
    if new_oom:
        alarms.append("napcat_oom_kill_detected")
    return {
        "version": 1, "mode": "observe_only", "checked_at": now,
        "container_id": container_id, "qq_state": qq_state,
        "high_cycles": cycles, **sample, "alarms": alarms,
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return None


def history_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Explicitly allowlist metadata; never persist upstream text or credentials."""
    checked_at = safe_number(state.get("checked_at"))
    if not 0 <= checked_at <= 253402300799:
        checked_at = 0
    qq_state = state.get("qq_state")
    if not isinstance(qq_state, str) or qq_state not in QQ_STATES:
        qq_state = "unknown"
    container_id = state.get("container_id")
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        container_id = None
    container_status = state.get("container_status")
    if not isinstance(container_status, str) or container_status not in CONTAINER_STATES:
        container_status = "unknown"
    alarms = state.get("alarms")
    if not isinstance(alarms, list):
        alarms = ["unrecognized_alarm"]
    safe_alarms = {
        value if isinstance(value, str) and value in HISTORY_ALARMS else "unrecognized_alarm"
        for value in alarms
    }
    result: dict[str, Any] = {
        "version": 1,
        "mode": "observe_only",
        "checked_at": checked_at,
        "recorded_at_utc": datetime.fromtimestamp(checked_at, UTC).isoformat(timespec="seconds"),
        "container_id": container_id,
        "container_status": container_status,
        "container_started_at": _safe_timestamp(state.get("container_started_at")),
        "qq_state": qq_state,
        "alarms": sorted(safe_alarms),
    }
    for key in (
        "working_bytes", "swap_bytes", "total_limit_bytes", "oom_kills",
        "high_cycles", "container_restart_count",
    ):
        if key in state:
            result[key] = int(min(max(safe_number(state[key]), 0), 2**63 - 1))
    if "used_ratio" in state:
        result["used_ratio"] = round(min(max(safe_number(state["used_ratio"]), 0), 1000), 6)
    return result


def _last_history_event(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            lines = stream.read().splitlines()
        value = json.loads(lines[-1]) if lines else {}
        return value if isinstance(value, dict) and value.get("version") == 1 else {}
    except (OSError, ValueError):
        return {}


def append_history(
    state_dir: Path, state: dict[str, Any], *,
    max_bytes: int = HISTORY_MAX_BYTES, backups: int = HISTORY_BACKUPS,
) -> bool:
    """Append a bounded status change or hourly heartbeat under the caller's lock."""
    path = state_dir / "history.jsonl"
    previous = _last_history_event(path)
    event = history_snapshot(state)
    reasons = []
    if not previous:
        reasons.append("first_sample")
    else:
        for key, reason in (
            ("qq_state", "qq_state_changed"),
            ("alarms", "alarms_changed"),
            ("container_id", "container_changed"),
            ("container_status", "container_status_changed"),
            ("container_started_at", "container_started"),
            ("container_restart_count", "container_restart_count_changed"),
        ):
            if event.get(key) != previous.get(key):
                reasons.append(reason)
        elapsed = event["checked_at"] - safe_number(previous.get("checked_at"))
        if elapsed < 0:
            reasons.append("clock_changed")
        elif not reasons and elapsed >= HISTORY_HEARTBEAT_SECONDS:
            reasons.append("heartbeat")
    if not reasons:
        return False
    event["reasons"] = reasons
    line = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
    if max_bytes < len(line) or not 0 <= backups <= 10:
        raise ValueError("invalid history rotation bounds")
    state_dir.mkdir(parents=True, exist_ok=True)
    separator = b""
    if path.exists() and path.stat().st_size:
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                # A previous interrupted write must not swallow the next event.
                separator = b"\n"
    if path.exists() and path.stat().st_size + len(separator) + len(line) > max_bytes:
        if backups:
            (state_dir / f"history.{backups}.jsonl").unlink(missing_ok=True)
            for index in range(backups - 1, 0, -1):
                old = state_dir / f"history.{index}.jsonl"
                if old.exists():
                    old.replace(state_dir / f"history.{index + 1}.jsonl")
            path.replace(state_dir / "history.1.jsonl")
        else:
            path.unlink()
        separator = b""
    # Limit local diagnostics to the service account, including on umask 000.
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(separator + line)
    return True


def record_history_safely(state_dir: Path, state: dict[str, Any]) -> None:
    try:
        append_history(state_dir, state)
    except Exception:
        # Full disks, permission failures or corrupt history must not stop probes.
        # Do not log exception text, which can contain sensitive upstream data.
        print('{"mode":"observe_only","history_error":"write_unavailable"}')


def command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=12, check=True)
    return result.stdout.strip()


def qq_health(bot: str) -> str:
    # Execute as the bot's unprivileged user. No host read of its secrets or DB.
    code = (
        "import json; from pathlib import Path; "
        "from pwcs2_bot.healthcheck import read_problem; "
        "p=Path('/data/runtime/napcat-health.json'); "
        "problem=read_problem(p); "
        "d=json.loads(p.read_bytes()) if problem in (None,'qq_not_ready') else {}; "
        "print(d.get('state') or problem or 'unknown')"
    )
    try:
        state = command(["docker", "exec", bot, "python", "-c", code])
    except (OSError, subprocess.SubprocessError):
        return "probe_unavailable"
    return state if state in QQ_STATES else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="pwcs2-qq-bot-napcat-1")
    parser.add_argument("--bot", default="pwcs2-qq-bot-bot-1")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/qqbot-runtime-guard"))
    args = parser.parse_args()
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", n)
           for n in (args.container, args.bot)):
        parser.error("invalid container name")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    # systemd already serializes oneshot runs; flock also covers manual checks.
    import fcntl

    with (args.state_dir / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        state_path = args.state_dir / "status.json"
        try:
            previous = json.loads(state_path.read_text())
            if not isinstance(previous, dict) or previous.get("version") != 1:
                previous = {}
        except (OSError, ValueError):
            previous = {}
        container: dict[str, Any] = {}
        try:
            details = command([
                "docker", "inspect", "--format",
                "{{.Id}} {{.State.Status}} {{.State.StartedAt}} {{.RestartCount}}", args.container,
            ]).split()
            container_id, running, started_at, restart_count = details
            if (
                not re.fullmatch(r"[0-9a-f]{64}", container_id)
                or running not in CONTAINER_STATES
                or not restart_count.isdecimal()
            ):
                raise ValueError("invalid container state")
            container = {
                "container_id": container_id,
                "container_status": running,
                "container_started_at": _safe_timestamp(started_at),
                "container_restart_count": int(restart_count),
            }
            if running != "running":
                raise ValueError("container not running")
            # Current production uses Docker/systemd unified cgroup v2. If
            # another host differs, report unavailable; never guess another cgroup.
            root = Path('/sys/fs/cgroup/system.slice') / f"docker-{container_id}.scope"
            sample = memory_sample(root)
            state = evaluate(previous, container_id=container_id, sample=sample,
                             qq_state=qq_health(args.bot), now=time.time())
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            state = {
                "version": 1, "mode": "observe_only", "checked_at": time.time(),
                "alarms": ["runtime_probe_unavailable"],
            }
        state.update(container)
        # Log transitions, plus one reminder per hour, without flooding journal.
        changed = state["alarms"] != previous.get("alarms")
        last_log = safe_number(previous.get("last_log_at"))
        due = not last_log or state["checked_at"] - last_log >= 3600
        state["last_log_at"] = state["checked_at"] if changed or due else last_log
        atomic_json(state_path, state)
        record_history_safely(args.state_dir, state)
        if changed or due:
            public = {k: v for k, v in state.items() if k not in ("container_id", "last_log_at")}
            print(json.dumps(public, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
