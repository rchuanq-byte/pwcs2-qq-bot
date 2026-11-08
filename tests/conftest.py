from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from nanobot.config.loader import get_config_path, set_config_path

# All tests use isolated synthetic identities, not the operator's local .env.
os.environ["NIUNIU_OPERATOR_QQ"] = "100000011"
os.environ["NIUNIU_SCRIPTURE_GROUP_ID"] = "100000021"
os.environ["NIUNIU_SCRIPTURE_URL"] = "https://example.test/niuniu/scripture.html"


@pytest.fixture(autouse=True)
def isolated_nanobot_config(tmp_path: Path) -> Iterator[None]:
    """Keep Nanobot runtime files inside each test's writable temporary directory."""
    previous = get_config_path()
    set_config_path(tmp_path / "nanobot-config.json")
    try:
        yield
    finally:
        set_config_path(previous)
