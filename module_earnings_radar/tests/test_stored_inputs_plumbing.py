"""The plumbing every reader of ``stored_inputs`` shares: an empty table, a store file that is not there yet, and a
store that cannot be read are all "nothing stored", never an exception, and the failure is noted once per table.

This used to be pinned through the two stored market series of D28/D29 (the Cboe volatility indices and the weekly
CFTC E-mini row). Those readers are gone: SPX Gamma reads both from the market store now
(``module_spx_gamma/store_reads.py``, ``vix_family`` and ``cot_tff``), so nothing read the old tables any more. The
plumbing is pinned here against the readers that remain.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.module_earnings_radar import stored_inputs

NOW = datetime(2026, 10, 21, 21, 0, tzinfo=timezone.utc)      # 17:00 New York: Wed 10-21 is complete
LATEST = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


@pytest.fixture(autouse=True)
def _notes(monkeypatch):
    """``_LOGGED`` is one-note-per-process; every test starts with none noted."""
    monkeypatch.setattr(stored_inputs, "_LOGGED", set())


def test_an_empty_table_is_nothing_stored_and_raises_nothing():
    assert stored_inputs.short_rows(["AAPL"]) == ({}, None)
    assert stored_inputs.calendar_items([LATEST]) == ({}, None)
    assert stored_inputs.rate_events() == []


def test_a_table_that_does_not_exist_yet_is_nothing_stored(monkeypatch, tmp_path):
    """Before the first nightly run the store file itself is absent: still not an error."""
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "absent.duckdb")
    assert stored_inputs.short_rows(["AAPL"]) == ({}, None)
    assert stored_inputs.calendar_items([LATEST]) == ({}, None)


def test_a_locked_table_notes_once_and_reads_as_nothing(monkeypatch, caplog):
    def locked(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rawstore, "query", locked)
    # Short interest reads the ownership store since G9, not raw.duckdb: the calendar reads stand in for it here.
    with caplog.at_level(logging.INFO, logger=stored_inputs.__name__):
        assert stored_inputs.calendar_items([LATEST]) == ({}, None)
        assert stored_inputs.rate_events() == []
        assert stored_inputs.calendar_items([LATEST]) == ({}, None)    # a second read adds no second note

    notes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stored inputs:")]
    assert len(notes) == 2
    assert "earnings_calendar" in notes[0] and "database is locked" in notes[0]
    assert "economic_calendar" in notes[1]
