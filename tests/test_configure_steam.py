from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[1] / "deploy" / "configure_steam.py"
SPEC = importlib.util.spec_from_file_location("configure_steam", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_read_env_value() -> None:
    assert MODULE.read_env_value("A=1\nPERFECTWORLD_MY_STEAM_ID=7656119\n", "A") == "1"
    assert MODULE.read_env_value("A=1\n", "MISSING") == ""


def test_update_env_text_preserves_other_secrets_and_removes_duplicates() -> None:
    result = MODULE.update_env_text(
        "PERFECTWORLD_TOKEN=keep\n"
        "STEAM_WEB_API_KEY=old\n"
        "STEAM_WEB_API_KEY=duplicate\n",
        "a" * 32,
    )

    assert result == (
        "PERFECTWORLD_TOKEN=keep\n"
        f"STEAM_WEB_API_KEY={'a' * 32}\n"
    )
