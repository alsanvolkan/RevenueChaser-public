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
        assert isinstance(need.max_lag_sessions, int) and need.max_lag_sessions >= 0, f"{name}: no max_lag_sessions"
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
