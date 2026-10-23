"""Typed Health: counts are the record, prose is derived (#124)."""
from __future__ import annotations

import ast
import dataclasses
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from RevenueChaser.core import datahealth

TODAY = date(2026, 10, 23)


def _health(**kw) -> datahealth.Health:
    base = dict(covered=4, expected=10, newest=None, expected_as_of=None,
                source_share=None)
    base.update(kw)
    return datahealth.Health(**base)


def _formatted(h: datahealth.Health, today=TODAY) -> list[str]:
    """Every string a caller can show. A percentage must not sneak into any of them."""
    return [
        datahealth.coverage_text(h),
        datahealth.freshness_text(h, today),
        repr(h),
    ]


def test_expected_none_is_na_never_a_percentage():
    """No definable population is n/a, including when covered is 0 — never 0%."""
    for covered in (None, 0, 1, 4999):
        h = _health(covered=covered, expected=None, newest=date(2026, 10, 20))
        assert h.coverage is None
        assert datahealth.coverage_text(h) == "n/a"
        for text in _formatted(h):
            assert "%" not in text, text
            assert "0%" not in text


def test_expected_zero_is_na_never_a_percentage():
    for covered in (None, 0, 5):
        h = _health(covered=covered, expected=0)
        assert h.coverage is None, covered
        assert datahealth.coverage_text(h) == "n/a"
        assert "%" not in datahealth.coverage_text(h)


def test_covered_none_is_na():
    h = _health(covered=None, expected=7689)
    assert h.coverage is None
    assert datahealth.coverage_text(h) == "n/a"
    assert h.consistency_problems() == ()


def test_coverage_is_covered_over_expected():
    h = _health(covered=4999, expected=7689)
    assert h.coverage == 4999 / 7689
    assert datahealth.coverage_text(h) == "65.0% (4,999 of 7,689)"
    assert h.consistency_problems() == ()


def test_percentage_is_one_decimal():
    h = _health(covered=1, expected=3)
    assert datahealth.coverage_text(h) == "33.3% (1 of 3)"


def test_zero_covered_of_a_real_population_is_zero_percent():
    """None covered is 0%, which is not the same fact as 'no population'."""
    h = _health(covered=0, expected=4)
    assert h.coverage == 0.0
    assert datahealth.coverage_text(h) == "0.0% (0 of 4)"


def test_covered_above_expected_stays_visible():
    h = _health(covered=10, expected=8, problems=("kept",))
    assert h.coverage == 10 / 8
    assert h.coverage > 1
    text = datahealth.coverage_text(h)
    assert text == "125.0% (10 of 8)"
    assert "100.0%" not in text
    derived = h.consistency_problems()
    assert any("10" in p and "8" in p for p in derived), derived
    # stored problems are the caller's; the exceedance is derived, not rewritten in
    assert h.problems == ("kept",)
    assert "kept" not in derived


def test_covered_above_a_zero_population_is_named_not_divided():
    h = _health(covered=5, expected=0)
    assert h.coverage is None
    assert datahealth.coverage_text(h) == "n/a"
    assert any("5" in p and "0" in p for p in h.consistency_problems())


def test_freshness_unknown_when_newest_is_missing():
    h = _health(newest=None, expected_as_of=TODAY)
    assert datahealth.freshness_text(h, TODAY) == "unknown"


def test_freshness_date_datetime_and_string_agree():
    newest_values = [
        date(2026, 10, 20),
        datetime(2026, 10, 20, 15, 30),
        datetime(2026, 10, 20, 15, 30, tzinfo=timezone.utc),
        "2026-10-20",
        "2026-10-20T15:30:00",
        "2026-10-20T15:30:00Z",
    ]
    for newest in newest_values:
        h = _health(newest=newest)
        assert datahealth.freshness_text(h, TODAY) == "3 d old", newest


def test_freshness_uses_expected_as_of_ahead_of_today():
    h = _health(newest=date(2026, 10, 1), expected_as_of=date(2026, 10, 10))
    assert datahealth.freshness_text(h, TODAY) == "9 d old"
    h = _health(newest="2026-10-01", expected_as_of="2026-10-10T00:00:00")
    assert datahealth.freshness_text(h, "2026-10-23") == "9 d old"
    h = _health(newest=datetime(2026, 10, 1, 23, 0),
                expected_as_of=datetime(2026, 10, 10, 1, 0))
    assert datahealth.freshness_text(h, TODAY) == "9 d old"


def test_freshness_same_calendar_day_is_zero():
    h = _health(newest=datetime(2026, 10, 23, 23, 59))
    assert datahealth.freshness_text(h, date(2026, 10, 23)) == "0 d old"


def test_unreadable_dates_are_unknown_not_today():
    assert datahealth.freshness_text(_health(newest="not-a-date"), TODAY) == "unknown"
    # a supplied as-of that cannot be read must not quietly fall back to today
    h = _health(newest=date(2026, 10, 20), expected_as_of="nope")
    assert datahealth.freshness_text(h, TODAY) == "unknown"


def test_assignment_raises():
    h = _health(covered=1, expected=2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.covered = 9  # type: ignore[misc]


def test_source_share_is_stored_not_normalised():
    given = {"edgar": 0.25, "uw": 0.25}
    h = _health(source_share=given)
    assert h.source_share == {"edgar": 0.25, "uw": 0.25}
    given["edgar"] = 9.0
    assert h.source_share["edgar"] == 0.25
    assert _health().source_share is None
    assert _health().problems == ()


def test_coverage_is_derived_not_a_field():
    assert "coverage" not in datahealth.Health.__dataclass_fields__


def test_module_does_not_import_store_or_network():
    src = Path(datahealth.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
    banned = [name for name in imported
              if "rawstore" in name or "provider" in name
              or name.split(".")[0] in {"requests", "httpx", "urllib", "socket", "duckdb"}]
    assert banned == [], banned


def test_scoped_runs_a_function_once_per_distinct_call_inside_a_memo_scope_and_always_outside():
    from RevenueChaser.core import datahealth
    calls = []

    @datahealth.scoped
    def summary(x=1):
        calls.append(x)
        return {"x": x, "rows": [x]}

    summary(); summary()
    assert calls == [1, 1]                      # outside a scope: the plain function
    with datahealth.memo_scope():
        a = summary(); b = summary(); summary(2)
        assert calls == [1, 1, 1, 2]            # inside: once per distinct call
        a["rows"].append(99)
        assert b["rows"] == [1] and summary()["rows"] == [1]   # callers get copies, never the cached object
    summary()
    assert calls == [1, 1, 1, 2, 1]             # the cache does not outlive the scope


def test_a_newest_date_after_the_reference_is_named_not_shown_as_a_negative_age():
    from datetime import date
    h = datahealth.Health(covered=1, expected=1, newest=date(2026, 10, 25), expected_as_of=date(2026, 10, 24))
    assert datahealth.freshness_text(h, date(2026, 10, 24)) == "newest date is 1 d after the reference date"
    assert "-" not in datahealth.freshness_text(h, date(2026, 10, 24))
    assert any("newest 2026-10-25 is after expected_as_of 2026-10-24" in p for p in h.consistency_problems())


def test_a_newest_date_on_or_before_the_reference_raises_no_date_problem():
    from datetime import date
    h = datahealth.Health(covered=1, expected=1, newest=date(2026, 10, 24), expected_as_of=date(2026, 10, 24))
    assert h.consistency_problems() == ()
    assert datahealth.freshness_text(h, date(2026, 10, 24)) == "0 d old"
