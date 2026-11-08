from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).parents[1] / "deploy" / "retry_compliance_removals.py"
)
SPEC = importlib.util.spec_from_file_location(
    "retry_compliance_removals",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def record(
    qq_id: str,
    *,
    last_checked: str = "2026-07-29",
) -> MODULE.DueRecord:
    return MODULE.DueRecord(
        qq_id=qq_id,
        first_noncompliant_date=date(2026, 7, 14),
        last_checked_date=date.fromisoformat(last_checked),
        reasons=("unbound",),
    )


def test_review_selects_only_live_ordinary_noncompliant_members() -> None:
    result = MODULE.review_due_records(
        (
            record("100"),
            record("200"),
            record("300"),
            record("400"),
            record("500"),
        ),
        [
            {
                "user_id": 100,
                "role": "member",
                "is_robot": False,
                "level": 2,
            },
            {
                "user_id": 200,
                "role": "member",
                "is_robot": False,
                "level": 2,
            },
            {
                "user_id": 300,
                "role": "admin",
                "is_robot": False,
                "level": 1,
            },
            {
                "user_id": 400,
                "role": "member",
                "is_robot": False,
                "level": 2,
            },
        ],
        bound_ids={"200", "400"},
        bot_id="999",
        today=date(2026, 7, 29),
        target_level=1,
    )

    assert tuple(item.qq_id for item in result.candidates) == ("100",)
    assert result.clearable_ids == ("200", "300", "400", "500")
    assert result.skipped_ids == ()


def test_review_skips_stale_or_incomplete_authority_data() -> None:
    result = MODULE.review_due_records(
        (
            record("100", last_checked="2026-07-27"),
            record("200"),
            record("300"),
        ),
        [
            {
                "user_id": 100,
                "role": "member",
                "is_robot": False,
                "level": 1,
            },
            {"user_id": 200, "role": "member", "level": 1},
            {
                "user_id": 300,
                "role": "member",
                "is_robot": False,
            },
        ],
        bound_ids=set(),
        bot_id="999",
        today=date(2026, 7, 29),
        target_level=1,
    )

    assert result.candidates == ()
    assert result.clearable_ids == ()
    assert result.skipped_ids == ("100", "200", "300")
