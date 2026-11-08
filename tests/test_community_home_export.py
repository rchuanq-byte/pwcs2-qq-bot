import importlib.util
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "community_home_export", Path(__file__).parents[1] / "deploy" / "export-community-home.py"
)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)

NOW = 1_800_000_000.0


def make_database(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE bindings (group_id TEXT, qq_id TEXT, steam_id TEXT);
            CREATE TABLE group_match_source_notifications
                (group_id TEXT, data_source INTEGER, match_id TEXT);
            CREATE TABLE group_match_notifications (group_id TEXT, match_id TEXT);
            CREATE TABLE match_notifications (group_id TEXT, steam_id TEXT, match_id TEXT);
            CREATE TABLE private_chat (content TEXT);
            INSERT INTO bindings VALUES ('100', 'secret-qq', 'secret-steam');
            INSERT INTO bindings VALUES ('100', 'secret-qq-2', 'secret-steam-2');
            INSERT INTO bindings VALUES ('200', 'foreign-qq', 'foreign-steam');
            INSERT INTO group_match_source_notifications VALUES
                ('100', 1, 'same-match'), ('100', 1, 'same-match'),
                ('100', 3, 'same-match'), ('100', 1, 'another-match'),
                ('200', 1, 'foreign-match');
            INSERT INTO group_match_notifications VALUES
                ('100', 'same-match'), ('100', 'legacy-only'), ('200', 'foreign-legacy');
            INSERT INTO match_notifications VALUES
                ('100', 'member-1', 'per-member-only'),
                ('100', 'member-2', 'per-member-only');
            INSERT INTO private_chat VALUES ('secret-private-chat');
        """)
    return path


def health_file(tmp_path, *, kind="transport", **changes):
    if kind == "guard":
        state = {
            "version": 1, "checked_at": NOW - 2, "mode": "observe_only",
            "container_status": "running", "qq_state": "online",
        }
    else:
        state = {
            "version": 1, "checked_at": NOW - 2, "state": "online",
            "connected": True, "online": True,
        }
    state.update(changes)
    path = tmp_path / "health.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_snapshot_is_group_scoped_deduplicated_and_allowlisted(tmp_path):
    database = make_database(tmp_path / "private.db")
    health = health_file(tmp_path, secret_token="never-export-this")
    catalog = tmp_path / "catalog.py"
    catalog.write_text("BADGES = ('one', 'two', 'two')\n", encoding="utf-8")
    before = database.read_bytes(), database.stat().st_mtime_ns
    result = exporter.build_snapshot(
        database=database, health=health, catalog=catalog, group_id="100", now=NOW,
    )
    assert result == {
        "version": 1, "updated_at": exporter.iso_utc(NOW),
        "bot": {"status": "online", "checked_at": exporter.iso_utc(NOW - 2)},
        "stats": {"bound_players": 2, "reported_matches": 4, "available_titles": 2},
    }
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before
    serialized = json.dumps(result)
    for private in ("secret", "foreign", "member", "same-match", "100", str(tmp_path)):
        assert private not in serialized
    assert not list(tmp_path.glob("private.db-*"))


def test_existing_empty_group_is_zero_not_unknown(tmp_path):
    db = make_database(tmp_path / "private.db")
    assert exporter.database_counts(db, "300") == {"bound_players": 0, "reported_matches": 0}


@pytest.mark.parametrize("tables,expected", [
    ("", {"bound_players": None, "reported_matches": None}),
    ("CREATE TABLE bindings (group_id TEXT);", {"bound_players": 0, "reported_matches": None}),
    ("CREATE TABLE bindings (wrong_column TEXT);",
     {"bound_players": None, "reported_matches": None}),
    ("CREATE TABLE group_match_notifications (group_id TEXT, match_id TEXT);"
     "INSERT INTO group_match_notifications VALUES ('100','a'),('100','a'),('200','b');",
     {"bound_players": None, "reported_matches": 1}),
    ("CREATE TABLE group_match_source_notifications "
     "(group_id TEXT, data_source INTEGER, match_id TEXT);"
     "INSERT INTO group_match_source_notifications VALUES ('100',1,'a'),('100',1,'a');",
     {"bound_players": None, "reported_matches": 1}),
])
def test_missing_schema_and_legacy_compatibility(tmp_path, tables, expected):
    path = tmp_path / "private.db"
    with sqlite3.connect(path) as db:
        db.executescript(tables)
    assert exporter.database_counts(path, "100") == expected


def test_missing_or_damaged_database_stays_unknown_without_creating_files(tmp_path):
    path = tmp_path / "missing.db"
    expected = {"bound_players": None, "reported_matches": None}
    assert exporter.database_counts(path, "100") == expected
    assert not path.exists()
    path.write_bytes(b"not a sqlite database")
    before = path.read_bytes()
    assert exporter.database_counts(path, "100") == expected
    assert path.read_bytes() == before


def test_exclusive_database_lock_is_bounded_and_unknown(tmp_path):
    path = make_database(tmp_path / "private.db")
    with sqlite3.connect(path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        result = exporter.database_counts(path, "100")
        elapsed = time.monotonic() - started
        writer.rollback()
    assert result == {"bound_players": None, "reported_matches": None}
    assert elapsed < 1.0


def test_connection_is_read_only_and_query_only(tmp_path, monkeypatch):
    path = make_database(tmp_path / "private.db")
    real_connect = sqlite3.connect
    connections = []

    class Connection(sqlite3.Connection):
        def close(self):
            assert self.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                self.execute("INSERT INTO bindings VALUES ('100','bad','bad')")
            super().close()

    def capture(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro")
        assert kwargs["uri"] is True
        assert kwargs["timeout"] <= 0.1
        connection = real_connect(database_uri, factory=Connection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(exporter.sqlite3, "connect", capture)
    assert exporter.database_counts(path, "100")["bound_players"] == 2
    assert len(connections) == 1


def test_execution_budget_aborts_count_without_reporting_zero(tmp_path, monkeypatch):
    path = make_database(tmp_path / "private.db")
    monkeypatch.setattr(exporter, "SQLITE_BUDGET_SECONDS", -1)
    monkeypatch.setattr(exporter, "SQLITE_PROGRESS_STEPS", 1)
    assert exporter.database_counts(path, "100") == {
        "bound_players": None, "reported_matches": None,
    }


def test_live_wal_counts_include_committed_rows_without_checkpointing(tmp_path):
    path = make_database(tmp_path / "private.db")
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("INSERT INTO bindings VALUES ('100', 'wal-qq', 'wal-steam')")
        writer.commit()
        wal = Path(str(path) + "-wal")
        before = path.read_bytes(), wal.read_bytes()
        assert exporter.database_counts(path, "100")["bound_players"] == 3
        assert (path.read_bytes(), wal.read_bytes()) == before
    finally:
        writer.close()


def test_wal_without_sidecars_does_not_create_private_files(tmp_path):
    path = make_database(tmp_path / "private.db")
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.close()
    assert not list(tmp_path.glob("private.db-*"))
    exporter.database_counts(path, "100")
    assert not list(tmp_path.glob("private.db-*"))


@pytest.mark.parametrize("changes", [
    {"checked_at": NOW - 96}, {"checked_at": NOW + 0.01},
    {"checked_at": True}, {"checked_at": "1800000000"},
    {"checked_at": float("inf")}, {"checked_at": float("nan")},
    {"checked_at": -1}, {"version": True}, {"version": 1.0},
    {"online": "true"}, {"online": 1}, {"connected": "false"},
    {"state": "surprise"},
])
def test_invalid_or_stale_transport_health_never_becomes_online(tmp_path, changes):
    path = health_file(tmp_path, **changes)
    assert exporter.read_bot_status(path, kind="transport", now=NOW)["status"] == "unknown"


@pytest.mark.parametrize("changes", [
    {"state": "login_required", "online": False},
    {"state": "disconnected", "connected": False, "online": False},
    {"state": "degraded"}, {"state": "online", "online": False},
])
def test_known_transport_failure_is_offline(tmp_path, changes):
    path = health_file(tmp_path, **changes)
    assert exporter.read_bot_status(path, kind="transport", now=NOW)["status"] == "offline"


@pytest.mark.parametrize("changes,expected", [
    ({}, "online"), ({"checked_at": NOW - 150}, "online"),
    ({"checked_at": NOW - 151}, "unknown"), ({"checked_at": NOW + 1}, "unknown"),
    ({"mode": "active"}, "unknown"), ({"qq_state": "login_required"}, "offline"),
    ({"qq_state": "disconnected"}, "offline"), ({"qq_state": "probe_unavailable"}, "unknown"),
    ({"qq_state": "stale_snapshot"}, "unknown"), ({"container_status": "exited"}, "offline"),
    ({"container_status": "unknown"}, "unknown"), ({"qq_state": True}, "unknown"),
    ({"alarms": ["runtime_probe_unavailable"]}, "unknown"),
])
def test_guard_health_requires_explicit_fresh_observation(tmp_path, changes, expected):
    path = health_file(tmp_path, kind="guard", **changes)
    assert exporter.read_bot_status(path, kind="guard", now=NOW)["status"] == expected
    assert exporter.read_bot_status(path, kind="transport", now=NOW)["status"] == "unknown"


@pytest.mark.parametrize("content", [b"{", b"[]", b"null", b" " * 8193])
def test_invalid_health_is_unknown(tmp_path, content):
    path = tmp_path / "health.json"
    path.write_bytes(content)
    assert exporter.read_bot_status(path, kind="transport", now=NOW) == {
        "status": "unknown", "checked_at": None,
    }


def test_missing_health_is_unknown(tmp_path):
    assert exporter.read_bot_status(tmp_path / "missing", kind="guard", now=NOW) == {
        "status": "unknown", "checked_at": None,
    }


def test_catalog_is_literal_only_and_does_not_execute_source(tmp_path):
    marker = tmp_path / "must-not-exist"
    path = tmp_path / "catalog.py"
    path.write_text(
        f"raise RuntimeError('must never execute')\n"
        f"BADGES = ('one', 'two')\nopen({str(marker)!r}, 'w').close()\n", encoding="utf-8",
    )
    assert exporter.count_titles(path) == 2
    assert not marker.exists()
    path.write_text("BADGES = tuple(['dynamic'])\n", encoding="utf-8")
    assert exporter.count_titles(path) is None
    assert exporter.count_titles(None) is None


def test_repository_catalog_is_parseable_without_import():
    path = Path(__file__).parents[1] / "src" / "pwcs2_bot" / "community.py"
    assert exporter.count_titles(path) > 0


def test_atomic_public_output_keeps_private_database_unchanged(tmp_path, monkeypatch):
    database = make_database(tmp_path / "private.db")
    database.chmod(0o600)
    before = database.read_bytes(), database.stat().st_mtime_ns, database.stat().st_mode
    output = tmp_path / "public" / "status.json"
    original_replace = os.replace
    calls = []

    def checked_replace(source, target):
        assert Path(source).parent == output.parent
        assert json.loads(Path(source).read_text()) == {"version": 1}
        calls.append((source, target))
        original_replace(source, target)

    monkeypatch.setattr(exporter.os, "replace", checked_replace)
    exporter.write_snapshot(output, {"version": 1}, database=database)
    assert json.loads(output.read_text()) == {"version": 1}
    assert len(calls) == 1
    assert not list(output.parent.glob(".community-home-*"))
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert (database.read_bytes(), database.stat().st_mtime_ns, database.stat().st_mode) == before


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_output_cannot_replace_database_or_its_sidecars(tmp_path, suffix):
    database = make_database(tmp_path / "private.db")
    before = database.read_bytes()
    with pytest.raises(ValueError):
        exporter.write_snapshot(Path(str(database) + suffix), {}, database=database)
    assert database.read_bytes() == before


def test_output_cannot_replace_health_or_catalog(tmp_path):
    health = health_file(tmp_path)
    before = health.read_bytes()
    with pytest.raises(ValueError):
        exporter.write_snapshot(health, {}, database=tmp_path / "private.db", health=health)
    assert health.read_bytes() == before


def test_output_symlink_and_hardlink_to_database_are_rejected(tmp_path):
    database = make_database(tmp_path / "private.db")
    link = tmp_path / "public.json"
    try:
        link.symlink_to(database)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        exporter.write_snapshot(link, {}, database=database)
    link.unlink()
    os.link(database, link)
    with pytest.raises(ValueError):
        exporter.write_snapshot(link, {}, database=database)


def test_output_hardlink_to_database_is_rejected(tmp_path):
    database = make_database(tmp_path / "private.db")
    link = tmp_path / "public.json"
    os.link(database, link)
    with pytest.raises(ValueError):
        exporter.write_snapshot(link, {}, database=database)


def test_database_symlink_cannot_hide_protected_sidecars(tmp_path):
    database = make_database(tmp_path / "private.db")
    alias = tmp_path / "alias.db"
    try:
        alias.symlink_to(database)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        exporter.write_snapshot(Path(str(database) + "-wal"), {}, database=alias)


def test_failed_atomic_replace_keeps_previous_snapshot_and_cleans_temp(tmp_path, monkeypatch):
    output = tmp_path / "public.json"
    output.write_text('{"previous":true}\n', encoding="utf-8")

    def fail_replace(source, target):
        raise OSError("injected failure")

    monkeypatch.setattr(exporter.os, "replace", fail_replace)
    with pytest.raises(OSError):
        exporter.write_snapshot(output, {"new": True}, database=tmp_path / "private.db")
    assert json.loads(output.read_text()) == {"previous": True}
    assert not list(tmp_path.glob(".community-home-*"))


def test_cli_prints_only_fixed_summary_and_never_inputs(tmp_path, capsys):
    database = make_database(tmp_path / "secret-private.db")
    health = health_file(tmp_path, kind="guard")
    output = tmp_path / "public.json"
    assert exporter.main([
        "--database", str(database), "--health", str(health), "--health-kind", "guard",
        "--group-id", "100", "--output", str(output),
    ]) == 0
    assert capsys.readouterr().out == "OK community_snapshot_written\n"
    assert set(json.loads(output.read_text())) == {"version", "updated_at", "bot", "stats"}
    assert exporter.main([
        "--database", str(database), "--health", str(health),
        "--group-id", "not-secret-group", "--output", str(output),
    ]) == 1
    assert capsys.readouterr().out == "ERROR community_snapshot_failed\n"
