from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "deploy" / "install_mihomo.py"
SPEC = importlib.util.spec_from_file_location("install_mihomo", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_asset_name_uses_compatible_amd64_build() -> None:
    assert MODULE.asset_name_for_machine("x86_64") == (
        f"mihomo-linux-amd64-compatible-v{MODULE.VERSION}.gz"
    )


def test_unknown_machine_is_rejected() -> None:
    with pytest.raises(MODULE.InstallationError, match="CPU"):
        MODULE.asset_name_for_machine("mips")


def test_parse_args_accepts_offline_archive() -> None:
    args = MODULE.parse_args(["--archive", "mihomo.gz"])
    assert args.archive == Path("mihomo.gz")


def test_docker_context_excludes_downloaded_archives() -> None:
    assert "*" in MODULE.DOCKERIGNORE
    assert "!mihomo" in MODULE.DOCKERIGNORE
    assert "*.gz" not in MODULE.DOCKERIGNORE


def test_find_release_asset_requires_exact_official_asset() -> None:
    name = MODULE.asset_name_for_machine("x86_64")
    url = (
        f"https://github.com/MetaCubeX/mihomo/releases/download/{MODULE.TAG}/{name}"
    )
    result = MODULE.find_release_asset(
        {
            "tag_name": MODULE.TAG,
            "assets": [
                {
                    "name": name,
                    "browser_download_url": url,
                    "digest": f"sha256:{'a' * 64}",
                    "size": 1234,
                }
            ],
        },
        name,
    )

    assert result == (url, "a" * 64, 1234)


@pytest.mark.parametrize(
    "payload",
    [
        {"tag_name": "v0.0.0", "assets": []},
        {"tag_name": MODULE.TAG, "assets": []},
        {
            "tag_name": MODULE.TAG,
            "assets": [
                {
                    "name": MODULE.asset_name_for_machine("x86_64"),
                    "browser_download_url": "https://evil.example/mihomo.gz",
                    "digest": f"sha256:{'a' * 64}",
                    "size": 1234,
                }
            ],
        },
    ],
)
def test_find_release_asset_rejects_untrusted_metadata(payload: object) -> None:
    with pytest.raises(MODULE.InstallationError):
        MODULE.find_release_asset(
            payload,
            MODULE.asset_name_for_machine("x86_64"),
        )
