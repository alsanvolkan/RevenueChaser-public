"""OIPD board -> result history (#382, A3). Offline; the store is a temp DuckDB.

An OIPD Fetch records its run through ``core/history.record_run``: one ``result_runs`` header, the chain the fit used
in ``oipd_chain_history`` (the mirror of ``oipd_chain``, numbered by ``row_no``) and one ``oipd_fit_history`` row per
fitted expiry. OIPD is a run board: every run keeps its rows; a partial chain is kept and the run says 'partial'.
"""
from __future__ import annotations

import json
import zlib
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_oipd import config, engine, history as oipd_history
from RevenueChaser.module_oipd.tests.test_engine import _TRUE, _FakeUW, _chain_for, fixed_today  # noqa: F401

BOARD, CHAIN, FITS = "module_oipd", "oipd_chain_history", "oipd_fit_history"
# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp()


class _PartialUW(_FakeUW):
    """The second expiry's page 2 failed: its contracts are the ones that arrived (half of them)."""

    def option_contracts_result(self, ticker, expiry=None, **kw):
        rows = _chain_for(expiry)
        if expiry == self.expiries[1]:
            return ProviderResult("partial", rows[: len(rows) // 2], {"page": 2, "status": 500}, "")
        return ProviderResult("ok", rows, None, "")


@pytest.fixture
def store(rawstore_tmp_db, fixed_today):  # noqa: F811
    ledger.take_notes()
    return rawstore_tmp_db


def _fetch(uw=None, ticker="TST"):
    capture: dict = {}
    payload = engine.run(uw or _FakeUW(), ticker, capture=capture)
    return payload, capture


def _record(payload, capture, run_id="r1", run_at=COMPLETE, **kw):
    return oipd_history.record(payload, capture, run_id=run_id, run_at=run_at, **kw)


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _rows(table, run_id="r1", order="row_no"):
    return rawstore.query(f"SELECT * FROM {table} WHERE run_id = ? ORDER BY {order}", [run_id], db="history")


def _unpack(cell):
    if isinstance(cell, (bytes, bytearray)):
        return json.loads(zlib.decompress(cell))
    return cell if not isinstance(cell, str) else json.loads(cell)


# ── the engine's capture ──

def test_a_fetch_captures_the_chain_it_saved_and_the_svi_of_each_fitted_expiry(store):
    payload, capture = _fetch()
    assert capture["status"] == "ok" and capture["errors"] == []
    stored = rawstore.query("SELECT * FROM oipd_chain WHERE ticker = 'TST' ORDER BY option_symbol")
    assert sorted(capture["chain"], key=lambda r: r["option_symbol"]) == stored
    assert sorted(capture["svi"]) == [e["date"] for e in payload["expiries"]]
    fitted = capture["svi"][payload["expiries"][0]["date"]]
    assert set(fitted) == {"a", "b", "rho", "m", "sigma"} and fitted["rho"] == pytest.approx(_TRUE["rho"], abs=0.05)


def test_the_payload_the_board_renders_is_unchanged_by_the_capture(store):
    with_capture, _ = _fetch()
    without = engine.run(_FakeUW(), "TST")
    for p in (with_capture, without):
        p.pop("generated_at"), p.pop("fetched_at")
    assert with_capture == without


def test_a_refresh_fills_no_capture(store):
    _fetch()
    capture: dict = {}
    engine.run(ticker="TST", no_fetch=True, capture=capture)
    assert capture == {}


# ── the record ──

def test_a_run_records_a_header_the_chain_and_one_fit_row_per_expiry(store):
    payload, capture = _fetch()
    n_chain, n_fits = len(capture["chain"]), len(payload["expiries"])
    assert _record(payload, capture) == {CHAIN: n_chain, FITS: n_fits} and n_fits == 4
    (head,) = _headers()
    assert head["quality"] == "complete" and head["quality_note"] is None and head["error"] is None
    chain = _rows(CHAIN)
    assert [r["row_no"] for r in chain] == list(range(n_chain))
    assert {k: v for k, v in chain[0].items() if k not in ("run_id", "row_no")} == capture["chain"][0]


_DENSITY = ("g_min_window", "clipped_pct", "min_var", "density_flag", "density_note", "density_g_min_shown")

def test_a_fit_row_carries_the_fit_its_greeks_svi_curve_and_smile(store):
    payload, capture = _fetch()
    _record(payload, capture)
    rows = _rows(FITS, order="expiry")
    e = payload["expiries"][0]
    r = rows[0]
    assert r["ticker"] == "TST" and str(r["expiry"]) == e["date"] and r["as_of"] == "2026-09-23"
    assert r["spot"] == 100.0 and r["risk_free"] == config.RISK_FREE_RATE and r["horizon_days"] == config.MAX_DTE
    assert r["n_contracts"] == len(_chain_for(e["date"])) and r["n_strikes"] == e["n"] and r["dte"] == e["dte"]
    assert r["t_years"] == e["T"] and r["forward"] == e["forward"] and r["atm_iv"] == e["atm_iv"]
    assert r["median"] == e["quantiles"]["median"] and r["p05"] == e["quantiles"]["p05"]
    assert r["prob_below_spot"] == e["prob_below_spot"] and r["p_up10"] == e["p_up10"]
    assert json.loads(r["greeks"]) == e["greeks"] and json.loads(r["svi"]) == capture["svi"][e["date"]]
    assert _unpack(r["curve"]) == e["curve"] and _unpack(r["smile"]) == e["smile"]


def test_a_fit_row_carries_the_density_verdict_the_board_shows(store):
    payload, capture = _fetch()
    _record(payload, capture)
    for e, r in zip(payload["expiries"], _rows(FITS, order="expiry")):
        assert {k: r[k] for k in _DENSITY} == {k: e[k] for k in _DENSITY}
        assert r["g_min"] == e["g_min"] and isinstance(r["density_flag"], bool) and r["density_note"]


def test_the_header_carries_model_version_params_and_context(store):
    payload, capture = _fetch()
    _record(payload, capture)
    (head,) = _headers()
    assert head["model_version"] == oipd_history.model_version() and head["model_version"].startswith("1+")
    assert json.loads(head["params"])["MAX_EXPIRIES"] == config.MAX_EXPIRIES
    ctx = json.loads(head["context"])
    assert ctx["ticker"] == "TST" and ctx["expiries_fitted"] == 4 and ctx["chain_status"] == "ok"
    assert ctx["n_contracts"] == len(capture["chain"])


@pytest.mark.parametrize("name, value", [("MAX_EXPIRIES", 8), ("MIN_CONTRACTS", 10), ("PENALTY_WEIGHT", 10.0),
                                         ("RISK_FREE_RATE", 0.05), ("G_FLOOR", 0.001)])
def test_model_version_changes_with_a_setting(monkeypatch, name, value):
    before = oipd_history.model_version()
    monkeypatch.setattr(config, name, value)
    assert oipd_history.model_version() != before


def test_every_run_keeps_its_rows(store):
    payload, capture = _fetch()
    _record(payload, capture, run_id="r1")
    _record(payload, capture, run_id="r2", run_at=COMPLETE + 3600)
    assert len(_rows(FITS, "r1", "expiry")) == len(_rows(FITS, "r2", "expiry")) == 4


def test_an_intraday_run_keeps_its_rows(store):
    payload, capture = _fetch()
    _record(payload, capture, run_at=INTRADAY)
    (head,) = _headers()
    assert head["quality"] == "intraday" and len(_rows(FITS, order="expiry")) == 4


def test_recording_the_same_run_id_twice_writes_nothing_more(store):
    payload, capture = _fetch()
    _record(payload, capture)
    _record(payload, capture)
    assert len(_headers()) == 1 and len(_rows(FITS, order="expiry")) == 4


# ── a degraded run: kept, partial (design C rule E) ──

def test_a_partial_chain_is_not_saved_but_its_rows_are_kept_and_the_run_is_partial(store):
    payload, capture = _fetch(_PartialUW())
    assert capture["status"] == "partial" and rawstore.count("oipd_chain") == 0
    counts = _record(payload, capture)
    (head,) = _headers()
    assert head["quality"] == "partial" and "the chain is partial (2026-11-22" in head["quality_note"]
    assert counts[CHAIN] == len(capture["chain"]) > 0 and counts[FITS] == len(payload["expiries"])


def test_a_run_that_fitted_nothing_is_partial_with_the_board_error(store):
    class EmptyUW(_FakeUW):
        def option_contracts(self, ticker, expiry=None, **kw):
            return []

    payload, capture = _fetch(EmptyUW(), "APPL")
    assert _record(payload, capture) == {CHAIN: 0, FITS: 0}
    (head,) = _headers()
    assert head["quality"] == "partial" and "no option chain for APPL" in head["quality_note"]


def test_failed_uw_calls_make_the_run_partial(store):
    payload, capture = _fetch()
    _record(payload, capture, uw_health={"calls": 5, "failed": 1})
    (head,) = _headers()
    assert head["quality"] == "partial" and "1 of 5 Unusual Whales calls failed" in head["quality_note"]
    assert len(_rows(FITS, order="expiry")) == 4


def test_the_board_warnings_ride_on_the_header(store):
    class NoSpot(_FakeUW):
        def ohlc_latest(self, ticker):
            return {}

    payload, capture = _fetch(NoSpot())
    _record(payload, capture)
    (head,) = _headers()
    assert any("No spot price" in w for w in json.loads(head["warnings"])) and head["quality"] == "complete"
