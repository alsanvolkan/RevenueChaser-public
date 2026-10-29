"""The board's staleness limits come from the need catalog, not from a copy of their own (avo, 29 Oct 2026).

``MAX_LAG`` is hashed into the board's ``model_version``, so a number kept in two places would let the catalog and
the board drift apart silently while the hash claims nothing changed. The catalog is the single owner; a table that
is still the old store's, with no need of its own, keeps the board's own number until it migrates.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_earnings_radar import history, stored_inputs
from RevenueChaser.rcdata import api


def test_every_limit_with_a_need_is_the_needs_own_number():
    for table, need_name in stored_inputs.NEED_OF_TABLE.items():
        need = api.catalog.NEEDS[need_name]
        assert stored_inputs.MAX_LAG[table] == need.max_lag, f"{table}: not the catalog's number"
        assert need.max_lag_unit == "sessions", f"{need_name}: MAX_LAG counts sessions"


def test_the_legacy_tables_keep_their_own_number_and_say_so():
    legacy = set(stored_inputs.MAX_LAG) - set(stored_inputs.NEED_OF_TABLE)
    assert legacy == {"estimate_snapshots"}, "a table gained or lost a need without this test being told"
    assert stored_inputs.MAX_LAG["estimate_snapshots"] == 5


def test_moving_a_catalog_number_moves_the_board(monkeypatch):
    """The point of one owner: a catalog change reaches the hash instead of being swallowed."""
    before = history.model_version()
    need = api.catalog.NEEDS["earnings.next"]
    monkeypatch.setitem(api.catalog.NEEDS, "earnings.next", type(need)(**{**need.__dict__, "max_lag": need.max_lag + 7}))
    assert stored_inputs.max_lag_map()["earnings_calendar"] == need.max_lag + 7
    assert history.model_version() != before


def test_the_calendars_limit_is_one_session():
    assert stored_inputs.MAX_LAG["earnings_calendar"] == 1
