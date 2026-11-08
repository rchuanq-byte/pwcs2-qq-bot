from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "deploy" / "configure_perfectworld.py"
SPEC = importlib.util.spec_from_file_location("configure_perfectworld", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parse_login_response() -> None:
    token, steam_id = MODULE.parse_login_response(
        {
            "code": 0,
            "result": {
                "loginResult": {
                    "accountInfo": {
                        "token": "a" * 40,
                        "steamId": 76561198929215155,
                    }
                }
            },
        }
    )

    assert token == "a" * 40
    assert steam_id == "76561198929215155"


def test_parse_login_response_sanitizes_remote_error() -> None:
    with pytest.raises(MODULE.ConfigurationError, match="bad response"):
        MODULE.parse_login_response({"code": 1, "description": "bad\nresponse"})


def test_update_env_text_replaces_keys_and_removes_duplicates() -> None:
    result = MODULE.update_env_text(
        "NAPCAT_ONEBOT_TOKEN=keep\n"
        "PERFECTWORLD_TOKEN=old\n"
        "PERFECTWORLD_TOKEN=duplicate\n",
        {
            "PERFECTWORLD_TOKEN": "new-token",
            "PERFECTWORLD_MY_STEAM_ID": "76561198929215155",
        },
    )

    assert result == (
        "NAPCAT_ONEBOT_TOKEN=keep\n"
        "PERFECTWORLD_TOKEN=new-token\n"
        "\n"
        "PERFECTWORLD_MY_STEAM_ID=76561198929215155\n"
    )
