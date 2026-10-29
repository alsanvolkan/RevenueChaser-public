"""G9 (build plan PR 19): Implied Probability reads the options store, for SPX and the option watch list only.

* the chain is ``option_eod`` of the name's newest session and the spot is that session's ``option_daily`` close;
* any other name shows "not on the option watch list" and gets no fit, even when the store has rows for it;
* a Fetch fits what is stored, exactly as a Refresh does, and calls no provider; it queues ``uw/option_list`` once
  that dataset has had its first run (D1);
* the old ``oipd_chain`` table is neither read nor written.

Fixture serving files in the test's own ``MARKET_DIR``; offline.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_oipd import config, engine
from RevenueChaser.module_oipd.tests.options_store import daily_row, eod_rows, write_options
from RevenueChaser.module_oipd.tests.test_engine import _ASOF, _SPOT, _chain_for, _expiries, _occ
from RevenueChaser.rcdata import ops, read

SESSION = date.fromisoformat(_ASOF)


def _store(ticker="TST", session=SESSION, close=_SPOT, expiries=None):
    rows = [c for e in (expiries or _expiries()) for c in _chain_for(e)]
    if ticker != "TST":
        rows = [{**r, "option_symbol": r["option_symbol"].replace("TST", ticker, 1)} for r in rows]
    write_options(option_eod=eod_rows(ticker, session, rows), option_daily=[daily_row(ticker, session, close)])


class _NoProvider:
    def __getattr__(self, name):
        raise AssertionError(f"the OIPD board must not call a provider ({name})")


def _medians(payload):
    return [e["quantiles"]["median"] for e in payload["expiries"]]


def test_a_list_name_is_fitted_from_the_options_store():
    ops.watch_add("option", "TST", by="test")
    _store()
    payload = engine.run(_NoProvider(), "tst")
    assert payload["error"] is None and payload["ticker"] == "TST"
    assert len(payload["expiries"]) == 4 and payload["spot"] == _SPOT and payload["asof"] == _ASOF
    assert payload["source"] == engine.SOURCE and payload["on_list"] is True


def test_the_newest_session_is_fitted_with_its_own_close():
    ops.watch_add("option", "TST", by="test")
    _store(session=SESSION - timedelta(days=1), close=90.0)
    _store()
    payload = engine.run(None, "TST", no_fetch=True)
    assert payload["asof"] == _ASOF and payload["spot"] == _SPOT
    assert payload["n_contracts"] == len([c for e in _expiries() for c in _chain_for(e)])


def test_a_fetch_and_a_refresh_draw_the_same_board():
    ops.watch_add("option", "TST", by="test")
    _store()
    capture: dict = {}
    fetched = engine.run(_NoProvider(), "TST", capture=capture)
    refreshed = engine.run(None, "TST", no_fetch=True)
    assert fetched["n_contracts"] == refreshed["n_contracts"] and _medians(fetched) == _medians(refreshed)
    assert capture["status"] == "ok" and capture["errors"] == [] and capture["candle_error"] is None
    assert len(capture["chain"]) == fetched["n_contracts"] and sorted(capture["svi"]) == _expiries()


def test_a_name_off_the_list_gets_the_label_and_no_fit_even_with_rows():
    _store()
    for no_fetch in (False, True):
        payload = engine.run(None, "TST", no_fetch=no_fetch)
        assert payload["expiries"] == [] and payload["n_contracts"] == 0 and payload["on_list"] is False
        assert payload["error"].startswith("TST is not on the option watch list")
        assert "SPX" in payload["error"]          # the list it is not on


def test_spx_is_on_the_list_without_being_added():
    _store("SPX")
    payload = engine.run(None, "SPX")
    assert payload["error"] is None and len(payload["expiries"]) == 4


def test_a_list_name_with_no_stored_chain_says_when_it_fills():
    ops.watch_add("option", "TST", by="test")
    payload = engine.run(None, "TST")
    assert payload["expiries"] == [] and payload["on_list"] is True
    assert "No stored option chain for TST yet" in payload["error"] and "uw/option_list" in payload["error"]


def test_no_spot_still_fits_from_the_chain():
    ops.watch_add("option", "TST", by="test")
    _store(close=None)
    payload = engine.run(None, "TST")
    assert payload["spot"] is None and len(payload["expiries"]) == 4
    assert any("No spot price" in w for w in payload["warnings"])


def test_the_expiries_are_the_dte_window():
    """0DTE and past the one-year horizon are left out, as the old expiry pick did."""
    ops.watch_add("option", "TST", by="test")
    _store()
    first = _expiries()[0]
    for outside in (SESSION.isoformat(), (SESSION + timedelta(days=400)).isoformat()):
        moved = [{**r, "option_symbol": r["option_symbol"].replace(first[2:4] + first[5:7] + first[8:10],
                                                                   outside[2:4] + outside[5:7] + outside[8:10])}
                 for r in _chain_for(first)]
        write_options(option_eod=eod_rows("TST", SESSION, moved))
    assert len({r["expiry"] for r in read.option_chain("TST")}) == 6
    contracts, meta = engine.store_chain("TST")
    assert sorted({c["expiry"] for c in contracts}) == _expiries()
    assert meta["expiry_count"] == 4 and meta["as_of"] == _ASOF


def test_past_the_cap_the_highest_open_interest_expiries_win(monkeypatch):
    ops.watch_add("option", "TST", by="test")
    rows = []
    for e, oi in zip(_expiries(), (10, 500, 20, 400)):
        rows += [{**r, "open_interest": oi} for r in _chain_for(e)]
    write_options(option_eod=eod_rows("TST", SESSION, rows), option_daily=[daily_row("TST", SESSION, _SPOT)])
    monkeypatch.setattr(config, "MAX_EXPIRIES", 2)
    contracts, _meta = engine.store_chain("TST")
    assert sorted({c["expiry"] for c in contracts}) == [_expiries()[1], _expiries()[3]]


def test_the_default_ticker_is_spx_and_a_refresh_keeps_the_last_ticker():
    """Default chosen, change if avo prefers: SPX, always on the list, replaces SPY."""
    assert config.DEFAULT_TICKER == "SPX"
    assert engine.run(None, None, no_fetch=True)["ticker"] == "SPX"
    config.OIPD_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.OIPD_JSON.write_text('{"ticker": "TST"}', encoding="utf-8")
    assert engine.run(None, None, no_fetch=True)["ticker"] == "TST"


def test_the_old_chain_table_is_neither_read_nor_written(rawstore_tmp_db):
    ops.watch_add("option", "TST", by="test")
    rawstore.write_rows("oipd_chain", [{"ticker": "TST", "option_symbol": _occ("TST", _expiries()[0], "C", 100),
                                        "expiry": _expiries()[0], "option_type": "C", "strike": 100.0,
                                        "fetched_at": "2026-09-23T20:00:00+00:00"}])
    rawstore.write_rows("oipd_meta", [{"ticker": "TST", "as_of": _ASOF, "spot": _SPOT,
                                       "fetched_at": "2026-09-23T20:00:00+00:00"}])
    assert "No stored option chain" in engine.run(None, "TST", no_fetch=True)["error"]
    _store()
    engine.run(None, "TST", capture={})
    assert rawstore.count("oipd_chain") == 1


# ── the runner: a Fetch is a queued build request ───────────────────────────────────────────────────────────────

@pytest.fixture
def board(tmp_path, monkeypatch):
    from RevenueChaser.app import runners
    from RevenueChaser.core import ledger
    for mod in (runners, config):
        monkeypatch.setattr(mod, "OIPD_LATEST", tmp_path / "oipd_latest.html")
        monkeypatch.setattr(mod, "OIPD_JSON", tmp_path / "oipd" / "oipd_latest.json")
    ledger.take_notes()
    ops.watch_add("option", "TST", by="test")
    _store()
    return runners


def test_the_fetch_queues_uw_option_list_only_after_its_first_run(board, monkeypatch):
    board.run_oipd(None, "TST")
    assert ops.queue_view()["active"] == []
    assert "uw/option_list not queued: it has not had its first run" in board.OIPD_LATEST.read_text("utf-8")
    monkeypatch.setattr(ops, "ever_built", lambda name: name == "uw/option_list")
    board.run_oipd(None, "TST")
    board.run_oipd(None, "TST")
    assert [(r["dataset"], r["requested_by"]) for r in ops.queue_view()["active"]] == [("uw/option_list", "oipd")]
    assert "Builder request: uw/option_list already queued." in board.OIPD_LATEST.read_text("utf-8")


def test_a_refresh_queues_nothing(board, monkeypatch):
    monkeypatch.setattr(ops, "ever_built", lambda name: True)
    board.run_oipd_cached(ticker="TST")
    assert ops.queue_view()["active"] == []


def test_the_board_job_needs_no_unusual_whales_client():
    from RevenueChaser.app import dashboard, jobs  # noqa: F401 - registers the jobs
    assert jobs._REQUIRES_UW["module_oipd"] is False
