import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "configure_astrbot_connection",
    Path(__file__).resolve().parents[1] / "deploy/configure_astrbot_connection.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("value", ["100000031", '"100000031"', "'100000031'"])
def test_connection_config_uses_the_current_account_without_reading_other_env(tmp_path, value):
    (tmp_path / ".env").write_text(
        f"PRIVATE_KEY=not-for-output\nNAPCAT_ACCOUNT={value}\n", encoding="utf-8"
    )
    assert module.account_config_path(tmp_path) == (
        tmp_path / "data/napcat/config/onebot11_100000031.json"
    )


def test_explicit_account_does_not_require_environment_file(tmp_path):
    assert module.account_config_path(tmp_path, "100000031").name == "onebot11_100000031.json"


@pytest.mark.parametrize("value", ["", "../3875475562", "100000031/other", "123", "00123"])
def test_account_paths_cannot_escape_the_config_directory(tmp_path, value):
    with pytest.raises(ValueError, match="Invalid QQ"):
        module.account_config_path(tmp_path, value)


@pytest.mark.parametrize("raw", ["", "NAPCAT_ACCOUNT=100000031\nNAPCAT_ACCOUNT=3875475562\n"])
def test_account_is_required_and_cannot_be_ambiguous(tmp_path, raw):
    (tmp_path / ".env").write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="Exactly one"):
        module.account_config_path(tmp_path)
