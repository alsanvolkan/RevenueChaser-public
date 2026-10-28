"""Opus review M1 (PR 6): "Loading universe…" asks the universe the boards read, not the old table.

``_universe_loading`` and Asymmetric Growth's no-card Refresh probed ``equity_universe`` directly. On the spine that
table is empty and stays empty (its refresh is retired), so opening a board while its own Fetch ran showed "Loading
universe… the board fills in when the universe job finishes" over a loaded spine, and an AG Refresh before the
first Fetch said "no stored universe". Both now ask ``universe.has_universe()``.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard, jobs
from RevenueChaser.core import rawstore, universe
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names


@pytest.fixture
def spine(monkeypatch, built):
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    assert universe.universe_source() == "spine"
    assert not rawstore.query("SELECT 1 FROM equity_universe LIMIT 1")      # the host's state: the old table empty


@pytest.mark.usefixtures("spine")
def test_a_board_fetch_on_a_loaded_spine_is_not_loading_the_universe(monkeypatch):
    monkeypatch.setattr(jobs, "has_active", lambda modules: True)
    assert universe.has_universe() is True
    assert dashboard._universe_loading("module_swing_potential") is False


@pytest.mark.usefixtures("spine")
def test_an_ag_refresh_before_any_fetch_says_no_scan_not_no_universe():
    assert engine.stored_result()["no_data_reason"] == "no_scan_stored"


def test_the_old_store_still_says_loading_while_it_is_empty(monkeypatch):
    monkeypatch.setenv(universe.SOURCE_ENV, "store")
    monkeypatch.setattr(jobs, "has_active", lambda modules: True)
    assert universe.has_universe() is False
    assert dashboard._universe_loading("module_swing_potential") is True


@pytest.mark.usefixtures("spine")
@pytest.mark.parametrize("modname", ["RevenueChaser.module_fundamentals.pipeline",
                                     "RevenueChaser.module_swing_potential.pipeline"])
def test_a_fetch_on_the_spine_never_pulls_the_old_universe(monkeypatch, modname):
    """The third place of M1 (``test_scan_refresh_145.test_fetch_with_no_stored_universe_still_loads_it``): with no
    spine the old store is the universe, so an empty one makes a Fetch pull it. On the spine the universe is the
    spine's: an empty ``equity_universe`` is no reason to call the Yahoo screener (the old refresh is retired), and
    the board never says "no stored universe"."""
    import importlib

    import yfinance
    mod = importlib.import_module(modname)
    screened: list[int] = []
    monkeypatch.setattr(yfinance, "screen", lambda *a, **k: screened.append(1) or {"quotes": [], "total": 0})
    from RevenueChaser.core import universe_fetch       # as the store-path test: a pull would reach the screener
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(universe_fetch, "_sec_member_list", lambda: ([{"ticker": "AAA", "name": "AAA Inc"}], None))
    monkeypatch.setattr(universe_fetch, "MIN_FULL_UNIVERSE", 1)
    monkeypatch.setattr(universe_fetch, "backfill_universe_caps", lambda **kw: {})
    payload = mod.run_scan()
    assert not screened
    assert payload.get("no_data_reason") != "no_universe_stored"
