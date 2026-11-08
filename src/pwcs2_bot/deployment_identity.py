"""Read the optional operator identity from local deployment settings."""

from __future__ import annotations

import os
from collections.abc import Mapping


def operator_qq(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    value = values.get("NIUNIU_OPERATOR_QQ", "").strip()
    if 1 <= len(value) <= 20 and value.isascii() and value.isdecimal() and int(value):
        return value
    return ""
