"""The need catalog's contract (PR A of the internal data API): the API wraps ``rcdata.read``, it does not replace it.

These tests need no store: they are what keeps the API from growing into a second source of truth.
"""
from __future__ import annotations

import pathlib
from datetime import date

import pytest

from RevenueChaser.rcdata import api, read


def test_every_need_points_at_a_read_function():
    assert api.catalog.NEEDS, "the catalog is empty"
    for name, need in api.catalog.NEEDS.items():
        assert need.name == name
        assert getattr(read, need.reader_name, None) is need.reader, \
            f"{name}: reader is not rcdata.read.{need.reader_name}"


def test_the_api_package_writes_no_sql():
    root = pathlib.Path(api.__file__).parent
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8").upper()
        for word in ("SELECT ", "INSERT ", "UPDATE ", "DELETE FROM", "CREATE TABLE"):
            assert word not in text, f"{path.name} runs its own SQL ({word.strip()})"


def test_every_need_names_its_freshness_in_one_place():
    for name, need in api.catalog.NEEDS.items():
        assert isinstance(need.max_lag, int) and need.max_lag >= 0, f"{name}: no max_lag"
        assert need.max_lag_unit in api.catalog.UNITS, f"{name}: {need.max_lag_unit!r} is not a unit"
        assert need.kind and need.dataset and need.day_column and need.key_column, f"{name}: an empty catalog field"


def test_unknown_need_is_an_error():
    with pytest.raises(api.UnknownNeed):
        api.get("prices.nope")


def test_fill_is_not_supported_yet():
    with pytest.raises(api.FillNotSupported):
        api.get("identity.universe", fill="if_stale")
    with pytest.raises(api.FillNotSupported):
        api.get("identity.universe", fill="wait")


def test_nothing_built_yet_is_missing_never_an_error():
    ans = api.get("identity.universe")
    assert ans.rows == [] and ans.state == "missing" and ans.fill is None
    assert ans.coverage["served"] == 0 and ans.freshness["as_of_session"] is None
    assert ans.status == read.NEVER_CHECKED


def test_the_lag_is_counted_in_sessions_not_calendar_days():
    # Friday 23 Oct 2026 to Monday 26 Oct: three calendar days, one session. Thanksgiving week: Wed 25 Nov to
    # Friday 27 Nov is two calendar days and one session, because the Thursday is a holiday.
    assert api.lag_sessions(date(2026, 10, 23), date(2026, 10, 26)) == 1
    assert api.lag_sessions(date(2026, 11, 25), date(2026, 11, 27)) == 1
    assert api.lag_sessions(date(2026, 10, 26), date(2026, 10, 26)) == 0
    assert api.lag_sessions(date(2026, 10, 27), date(2026, 10, 26)) == 0   # rows newer than the reference


def test_a_summary_of_a_table_with_no_serving_file_is_empty_never_an_error():
    """Lives here, not beside the store tests: those build a store before the first test runs."""
    assert read.table_summary("security", day_column="source_as_of", key_column="ticker") == {
        "table": "security", "dataset": "spine/security", "row_count": 0, "key_count": 0,
        "newest_day": None, "status": read.NEVER_CHECKED, "sample_rows": []}


def _served_columns() -> dict[str, set[str]]:
    """``{table: its served columns}`` from the build modules that declare them (``SERVED``, else ``SCHEMA``).

    Not every serving table declares one, so this covers what it can; what it covers, it covers exactly.
    """
    import importlib
    import pkgutil

    from RevenueChaser.rcdata import build

    out: dict[str, set[str]] = {}
    for found in pkgutil.walk_packages(build.__path__, build.__name__ + "."):
        try:
            mod = importlib.import_module(found.name)
        except Exception:                      # noqa: BLE001 - a build module that will not import is its own test
            continue
        table = getattr(mod, "TABLE", None)
        cols = getattr(mod, "SERVED", None) or getattr(mod, "SCHEMA", None)
        if isinstance(table, str) and cols:
            out[table] = {c for c, *_ in cols}
    return out


def test_every_needs_day_and_key_column_is_a_real_column():
    """A catalog typo is a wrong answer, not an error: ``max(no_such_column)`` would simply never look fresh."""
    served = _served_columns()
    assert served, "no build module declares its served columns any more"
    checked = 0
    for name, need in api.catalog.NEEDS.items():
        cols = served.get(need.table)
        if not cols:
            continue
        checked += 1
        assert need.day_column in cols, f"{name}: {need.day_column!r} is not a column of {need.table}"
        assert need.key_column in cols, f"{name}: {need.key_column!r} is not a column of {need.table}"
    assert checked >= 5, "the column check stopped covering the needs it used to"


# ── The unit (avo, 29 Oct 2026: one number per need, and the unit it is counted in) ─────────────────────────────

def test_a_calendar_day_lag_counts_calendar_days_not_sessions():
    """A weekend is two days and no sessions: the two units must not answer the same."""
    friday, monday = date(2026, 10, 23), date(2026, 10, 26)
    assert api.lag_in("days", friday, monday) == 3
    assert api.lag_in("sessions", friday, monday) == 1
    assert api.lag_in("days", monday, friday) == 0           # rows newer than the reference
    thanksgiving_week = (date(2026, 11, 25), date(2026, 11, 27))
    assert api.lag_in("days", *thanksgiving_week) == 2 and api.lag_in("sessions", *thanksgiving_week) == 1


def test_a_need_counted_in_days_exists_and_is_the_weekly_build():
    need = api.catalog.NEEDS["fundamentals.estimates"]
    assert need.max_lag_unit == "days" and need.max_lag == 14, "PR 285 measured 14 calendar days"


def test_an_unknown_unit_is_refused_when_the_catalog_is_built():
    with pytest.raises(ValueError):
        api.catalog.Need("x", "universe", "security", "source_as_of", "ticker", 1, max_lag_unit="fortnights")
