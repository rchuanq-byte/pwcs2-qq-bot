"""Local deployment bridge: never initiate an automatic turn during user quiet."""
import os
from datetime import UTC, datetime


def automatic_quiet():
    raw = os.environ.get("NIUNIU_QUIET_UNTIL", "")
    if not raw:
        return False
    try:
        return datetime.now(UTC) < datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
