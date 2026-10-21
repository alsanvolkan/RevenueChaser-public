"""Financials board -> result history (#385, A3 first writer). Offline; the store is a temp DuckDB.

A Financials Fetch records its report through ``core/history.record_run``: one ``result_runs`` header plus one
``financials_report_history`` row (the ticker's report: sector, modules run, comps peers, the assumptions each model
used, when the data was fetched, the warnings, Quick Remarks and every module's numbers), in one transaction on
history.duckdb. Financials is a run board: every run keeps its row; a module that could not run makes the run
'partial' and the row is still kept (design C rule E).
"""
from __future__ import annotations

import json
import zlib
from datetime import datetime, timezone

import pytest

import RevenueChaser.module_financials.run as fm
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_financials import history as fin_history

BOARD, TABLE = "module_financials", "financials_report_history"
MODULES = ["dcf", "3stmt", "comps", "credit", "sensitivity"]
# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete); 14:00 UTC = 10:00 ET (intraday).
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp()
_PEER_INFO = {"enterpriseValue": 9.0e9, "ebitda": 1.0e9, "totalRevenue": 4.0e9, "trailingPE": 18.0}
# The spine's Energy names the comps ladder picks for TEST (same industry, nearest market cap first).
PEERS = [("XOM", 1.1e9), ("CVX", 0.9e9), ("COP", 1.2e9), ("SLB", 1.3e9), ("EOG", 1.4e9)]


def _energy_row(ticker, market_cap):
    return {"ticker": ticker, "cik": "", "name": ticker, "market_cap": market_cap, "price": 100.0,
            "sector": "Energy", "industry": "Oil & Gas E&P", "industry_group": "", "sub_industry": "",
            "quote": {"symbol": ticker}}



@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    return rawstore_tmp_db


@pytest.fixture
def results(store, tmp_path, monkeypatch, financials):
    """What a real Financials run returns for TEST: every module, peers from the spine sector, no network."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    rawstore.replace_all("equity_universe", [_energy_row("TEST", 1e9)] +
                         [_energy_row(t, cap) for t, cap in PEERS])
    monkeypatch.setattr(fm.cache_manager, "get_or_fetch", lambda t, fn, use_cache=True: dict(financials))
    qr = fm.quick_remarks_engine
    monkeypatch.setattr(qr, "run_quick_remarks", lambda ticker, no_fetch=False: qr._from_card(ticker, {}, "yfinance"))
    monkeypatch.setattr(fm.comps_engine, "_default_fetch", lambda t: dict(_PEER_INFO))
    return fm.run("TEST", modules=MODULES, output="none", run_id="job-q")["results"]


def _record(results, run_id="r1", run_at=COMPLETE, **kw):
    kw.setdefault("modules", MODULES)
    return fin_history.record(results, ticker="TEST", run_id=run_id, run_at=run_at, **kw)


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ? ORDER BY ticker", [run_id], db="history")


def _unpack(cell):
    """A compressed JSON cell as the raw query returns it (zlib bytes) or already decoded."""
    if isinstance(cell, (bytes, bytearray)):
        return json.loads(zlib.decompress(cell))
    return cell if not isinstance(cell, str) else json.loads(cell)


def test_a_run_records_a_header_and_one_report_row(results):
    assert _record(results) == {TABLE: 1}
    (head,) = _headers()
    assert head["quality"] == "complete" and head["error"] is None and head["quality_note"] is None
    assert json.loads(head["row_counts"]) == {TABLE: 1}
    (row,) = _rows("r1")
    assert row["ticker"] == "TEST" and row["sector"] == "Energy"


def test_the_row_carries_the_modules_run_and_their_numbers(results):
    _record(results)
    (row,) = _rows("r1")
    assert json.loads(row["modules_run"]) == ["dcf", "three_statement", "comps", "credit", "sensitivity"]
    modules = _unpack(row["modules"])
    assert set(modules) == {"dcf", "three_statement", "comps", "credit", "sensitivity"}
    assert modules["dcf"]["valuation"] == json.loads(json.dumps(results["modules"]["dcf"]["valuation"]))
    assert modules["comps"]["multiples"]["ev_ebitda"]["p50"] == 9.0


def test_the_row_carries_the_comps_peers_and_each_models_assumptions(results):
    _record(results)
    (row,) = _rows("r1")
    assert json.loads(row["peers"]) == ["XOM", "CVX", "COP", "SLB", "EOG"]
    assumptions = json.loads(row["assumptions"])
    assert set(assumptions) == {"dcf", "three_statement"}
    assert assumptions["dcf"] == json.loads(json.dumps(results["modules"]["dcf"]["assumptions"]))
    assert assumptions["three_statement"]["tax_rate"] == results["modules"]["three_statement"]["assumptions"]["tax_rate"]


def test_the_row_carries_the_data_date_the_warnings_and_quick_remarks(results):
    results["financials"]["fetched_at_iso"] = "2026-10-06T21:30:00+00:00"
    results["warnings"].append("dcf: something to note")
    _record(results)
    (row,) = _rows("r1")
    assert row["data_fetched_at"] == "2026-10-06T21:30:00+00:00"
    assert "dcf: something to note" in json.loads(row["warnings"])
    qr = json.loads(row["quick_remarks"])
    assert qr["ticker"] == "TEST" and qr["source"] == "yfinance"


def test_the_header_carries_model_version_params_and_context(results):
    _record(results)
    (head,) = _headers()
    assert head["model_version"] == fin_history.model_version() and head["model_version"].startswith("1+")
    params = json.loads(head["params"])
    assert params["modules"] == MODULES and params["defaults"]["TERMINAL_GROWTH"] == 0.025
    ctx = json.loads(head["context"])
    assert ctx["ticker"] == "TEST" and ctx["sector"] == "Energy"


def test_model_version_changes_with_a_default_assumption(monkeypatch):
    before = fin_history.model_version()
    from RevenueChaser.module_financials import config
    monkeypatch.setattr(config, "TERMINAL_GROWTH", 0.03)
    assert fin_history.model_version() != before


@pytest.mark.parametrize("module, name, value", [
    ("credit_capacity_engine", "DEBT_EBITDA_SAFE", 2.5),
    ("credit_capacity_engine", "DEBT_EBITDA_STRESSED", 4.5),
    ("credit_capacity_engine", "INTEREST_COVERAGE_SAFE", 3.5),
    ("credit_capacity_engine", "INTEREST_COVERAGE_STRESSED", 1.5),
    ("sensitivity_engine", "WACC_DELTAS", [-0.01, 0.0, 0.01]),
    ("sensitivity_engine", "GROWTH_DELTAS", [-0.02, 0.0, 0.02]),
])
def test_model_version_changes_with_a_credit_threshold_or_a_sensitivity_axis(monkeypatch, module, name, value):
    """Review F2: the credit thresholds and the sensitivity axes shape the stored numbers too."""
    import importlib
    engine = importlib.import_module(f"RevenueChaser.module_financials.engines.{module}")
    before = fin_history.model_version()
    monkeypatch.setattr(engine, name, value)
    assert fin_history.model_version() != before


def test_every_run_keeps_its_row_two_runs_of_a_session_both_have_it(results):
    _record(results, run_id="r1")
    _record(results, run_id="r2", run_at=COMPLETE + 3600)
    assert len(_rows("r1")) == 1 and len(_rows("r2")) == 1


def test_an_intraday_run_keeps_its_row_and_says_intraday(results):
    _record(results, run_id="early", run_at=INTRADAY)
    (head,) = _headers()
    assert head["quality"] == "intraday" and len(_rows("early")) == 1


def test_recording_the_same_run_id_twice_writes_nothing_more(results):
    _record(results)
    _record(results)
    assert len(_headers()) == 1 and len(_rows("r1")) == 1


# ── a degraded report: kept, partial, with its row (design C rule E) ──

def test_a_module_that_could_not_run_makes_the_run_partial_and_keeps_the_row(results):
    results["modules"]["dcf"] = {"error": "insufficient financial data", "warnings": []}
    _record(results)
    (head,) = _headers()
    assert head["quality"] == "partial" and "dcf: insufficient financial data" in head["quality_note"]
    assert len(_rows("r1")) == 1 and json.loads(head["row_counts"]) == {TABLE: 1}


def test_every_failed_module_is_named(results):
    results["modules"]["dcf"] = {"error": "no revenue in latest year"}
    results["modules"]["comps"] = {"error": "target financial data missing"}
    _record(results)
    (head,) = _headers()
    assert "dcf: no revenue in latest year" in head["quality_note"]
    assert "comps: target financial data missing" in head["quality_note"]


def test_a_report_that_was_not_written_is_partial(results):
    _record(results, report_written=False)
    (head,) = _headers()
    assert head["quality"] == "partial" and "report file was not written" in head["quality_note"]


def test_a_run_without_results_gets_a_partial_header_and_no_row(store):
    assert _record({}) == {TABLE: 0}
    (head,) = _headers()
    assert head["quality"] == "partial" and "no report" in head["quality_note"]
    assert _rows("r1") == []


def test_a_non_json_value_or_a_nan_does_not_stop_the_record(results):
    from datetime import date
    results["modules"]["dcf"]["valuation"]["ev_gordon"] = float("nan")
    results["modules"]["dcf"]["base_year"]["fiscal_year"] = date(2025, 12, 31)
    assert _record(results) == {TABLE: 1}
    (row,) = _rows("r1")
    assert _unpack(row["modules"])["dcf"]["base_year"]["fiscal_year"] == "2025-12-31"
