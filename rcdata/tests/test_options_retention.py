"""The raw option retention hook: built, its value left unset (the Settings warning stays on), and once avo sets one
it removes only the per-session raw option folders older than that, never Parquet, the calendar or the IBKR year."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops, retention
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.options import ibkr_iv, uw_list, uw_screener

from .options_fixture import SESSION, UWOptions

AFTER_CLOSE = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)
OLD = SESSION - timedelta(days=400)
RECENT = SESSION - timedelta(days=10)


@pytest.fixture(autouse=True)
def uw_env(monkeypatch, http_mock):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)
    monkeypatch.delenv(retention.ENV, raising=False)
    http_mock.add("uw.test/api", UWOptions())


def _raw(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x1f\x8b")
    return path


def _old_and_recent():
    files = {
        "list_old": _raw(uw_list.raw_path(OLD, "AAPL")),
        "list_recent": _raw(uw_list.raw_path(RECENT, "AAPL")),
        "er_old": _raw(uw_screener.er_path(OLD, "GOOD")),
        "calendar_old": _raw(uw_screener.calendar_path(OLD, OLD + timedelta(days=1), "premarket")),
        "screener_old": _raw(uw_screener.screener_path(OLD, 0)),
        "ibkr": _raw(ibkr_iv.raw_path("SPX")),
    }
    # A marker in an old Parquet partition (not a part file, so the build does not read it): Parquet is never removed.
    files["parquet_old"] = _raw(base.parquet_root() / "uw" / "option_list" / "option_eod" / f"date={OLD}" / "keep")
    return files


def _daily():
    return base.run_request({"dataset": uw_list.NAME, "params": {}})


def test_unset_the_hook_removes_nothing():
    files = _old_and_recent()
    row = _daily()
    assert row["result"] != "failed"
    assert all(p.exists() for p in files.values())
    assert retention.days() == (None, None)


def test_set_it_removes_the_old_session_folders_of_the_option_raw_only(monkeypatch):
    files = _old_and_recent()
    monkeypatch.setenv(retention.ENV, "365")
    row = _daily()
    gone = {k for k, p in files.items() if not p.exists()}
    assert gone == {"list_old", "er_old"}
    assert not uw_list.raw_path(OLD, "AAPL").parent.exists(), "the emptied session folder goes too"
    assert "raw retention 365 days: 2 files of 2 sessions removed" in row["note"]


def test_a_dataset_schedule_row_sets_it_too():
    ops.schedule_set(retention.ROW, "365", None)
    assert retention.days() == (365, f"dataset_schedule row {retention.ROW!r}")


def test_a_value_that_is_not_a_whole_number_of_days_leaves_it_unset(monkeypatch):
    monkeypatch.setenv(retention.ENV, "0")
    assert retention.days() == (None, None), "zero would remove the session just fetched"
    monkeypatch.setenv(retention.ENV, "a year")
    assert retention.days() == (None, None)


def test_the_settings_card_reads_the_same_rule(monkeypatch):
    from RevenueChaser.module_settings import newdata
    assert (newdata.RETENTION_ENV, newdata.RETENTION_ROW) == (retention.ENV, retention.ROW)
