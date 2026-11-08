#!/usr/bin/env python3
"""Install build-system and runtime requirements as a cacheable Docker layer."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path


def _requirements(*, only_optional_group: str = "") -> list[str]:
    with Path("pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    if only_optional_group:
        groups = config["project"].get("optional-dependencies", {})
        if only_optional_group not in groups:
            raise SystemExit(f"unknown optional dependency group: {only_optional_group}")
        return list(groups[only_optional_group])
    return [
        *config["build-system"]["requires"],
        *config["project"]["dependencies"],
        *config["project"].get("optional-dependencies", {}).get("legacy", []),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-optional-group", default="")
    args = parser.parse_args()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            *_requirements(only_optional_group=args.only_optional_group),
        ],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
