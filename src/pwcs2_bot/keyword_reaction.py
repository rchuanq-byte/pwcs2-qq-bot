"""Passive, packaged-image reactions for ordinary QQ group chat."""

from __future__ import annotations

import os
import re
from pathlib import Path

_XH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:xhnb|xh)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def is_xh_reaction_trigger(text: str) -> bool:
    """Match standalone ASCII keywords in ordinary, non-command chat text."""
    normalized = text.strip()
    if not normalized or normalized.startswith(("#", "/")):
        return False
    without_urls = _URL_PATTERN.sub("", normalized)
    return _XH_PATTERN.search(without_urls) is not None


def xh_reaction_image_path() -> Path:
    local_image = os.environ.get("NIUNIU_XH_REACTION_IMAGE", "").strip()
    if local_image:
        return Path(local_image)
    return Path(__file__).with_name("assets") / "reactions" / "xh-defeat.png"
