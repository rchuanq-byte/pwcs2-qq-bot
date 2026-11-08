import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/install-community-home.py"
SPEC = importlib.util.spec_from_file_location("community_home_install", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture
def paths(tmp_path):
    source, public, backup = (tmp_path / name for name in ("source", "public", "private"))
    for path in (source, public, backup):
        path.mkdir()
    (source / "index.html").write_text(
        "<!doctype html><!-- NIUNIU_COMMUNITY_HOME_V1 --><html>牛牛</html>",
        encoding="utf-8",
    )
    (source / "style.css").write_text("body{}", encoding="utf-8")
    (source / "app.js").write_text("'use strict';", encoding="utf-8")
    (public / "cs2-training.html").write_text("existing game", encoding="utf-8")
    (public / "index.html").write_text("existing TS page", encoding="utf-8")
    return source, public, backup


def test_install_only_owned_assets_and_preserves_snapshot(paths):
    source, public, backup = paths
    destination = public / "niuniu"
    destination.mkdir()
    (destination / "snapshot.json").write_text('{"version":1}', encoding="utf-8")
    result = installer.install(*paths)
    assert result["files"] == 3
    assert (destination / "index.html").read_bytes() == (source / "index.html").read_bytes()
    assert (destination / "snapshot.json").read_text() == '{"version":1}'
    assert (public / "index.html").read_text() == "existing TS page"
    assert (public / "cs2-training.html").read_text() == "existing game"
    assert result["backup"].is_relative_to(backup)


def test_second_install_backs_up_previous_assets_outside_webroot(paths):
    source, public, backup = paths
    installer.install(*paths)
    original = (source / "style.css").read_bytes()
    (source / "style.css").write_text("body{color:red}")
    result = installer.install(*paths)
    assert (result["backup"] / "style.css").read_bytes() == original
    assert (public / "niuniu/style.css").read_text() == "body{color:red}"


def test_installs_reviewed_scripture_assets_without_publishing_ai_corpus(paths):
    source, public, backup = paths
    reviewed = SCRIPT.parents[1] / "src/pwcs2_bot/assets/community_home"
    names = ("scripture.html", "scripture.css", "scripture.js", "scripture.md")
    for name in names:
        (source / name).write_bytes((reviewed / name).read_bytes())
    result = installer.install(source, public, backup)
    assert result["files"] == 7
    assert set(path.name for path in (public / "niuniu").iterdir()) == {
        "index.html", "style.css", "app.js", *names,
    }
    assert (public / "niuniu/scripture.html").read_bytes() == (reviewed / "scripture.html").read_bytes()
    assert not (public / "niuniu/verses.json").exists()


@pytest.mark.parametrize("name", [".env", "bindings.db", "snapshot.json", "private.txt"])
def test_refuses_non_allowlisted_source(paths, name):
    (paths[0] / name).write_text("must not publish")
    with pytest.raises(ValueError):
        installer.install(*paths)
    assert not (paths[1] / "niuniu").exists()


def test_refuses_backup_inside_public_root(paths):
    bad = paths[1] / "backups"
    bad.mkdir()
    with pytest.raises(ValueError):
        installer.install(paths[0], paths[1], bad)


def test_refuses_preexisting_unowned_directory_contents(paths):
    destination = paths[1] / "niuniu"
    destination.mkdir()
    (destination / "other-project.txt").write_text("preserve me")
    with pytest.raises(ValueError):
        installer.install(*paths)
    assert (destination / "other-project.txt").read_text() == "preserve me"


def test_refuses_other_site_even_with_same_asset_names(paths):
    destination = paths[1] / "niuniu"
    destination.mkdir()
    (destination / "index.html").write_text("<!doctype html><html>Other project</html>")
    (destination / "app.js").write_text("'other';")
    with pytest.raises(ValueError):
        installer.install(*paths)
    assert "Other project" in (destination / "index.html").read_text()


def test_refuses_symlink_destination(paths):
    destination = paths[1] / "niuniu"
    try:
        destination.symlink_to(paths[2], target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        installer.install(*paths)


def test_failed_copy_restores_already_changed_assets(paths, monkeypatch):
    installer.install(*paths)
    old = (paths[1] / "niuniu/app.js").read_bytes()
    (paths[0] / "app.js").write_text("'changed';")
    (paths[0] / "style.css").write_text("body{color:red}")
    write = installer.atomic_write
    def fail_css(path, data, mode=0o644):
        if path == paths[1] / "niuniu/style.css":
            raise OSError("simulated")
        return write(path, data, mode)
    monkeypatch.setattr(installer, "atomic_write", fail_css)
    with pytest.raises(OSError):
        installer.install(*paths)
    assert (paths[1] / "niuniu/app.js").read_bytes() == old


def test_units_are_bounded_and_do_not_control_qq():
    root = SCRIPT.parent
    service = (root / "qqbot-community-home.service").read_text()
    timer = (root / "qqbot-community-home.timer").read_text()
    assert "Type=oneshot" in service and "MemoryMax=48M" in service
    assert "PrivateNetwork=true" in service
    assert "--health-kind guard" in service
    assert "docker" not in service and "Restart=" not in service
    assert "OnUnitInactiveSec=60s" in timer
