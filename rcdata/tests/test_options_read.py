"""The options read API (``rcdata.read``): what a board gets from ``options.duckdb``, with provenance and status, built
from the UW mock at the HTTP boundary and the fake IBKR gateway."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import ibkr_tws, unusual_whales
from RevenueChaser.rcdata import ops, read
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.options import ibkr_iv, uw_list, uw_screener

from .ibkr_fixture import FakeGateway
from .options_fixture import SESSION, UWOptions

AFTER_CLOSE = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)
NEXT = calendar.next_trading_day(SESSION)
LIQUID = {"call_volume": 400, "put_volume": 200, "total_open_interest": 5000, "call_premium": 80_000,
          "put_premium": 40_000}


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    monkeypatch.setattr(calendar, "_now", lambda: AFTER_CLOSE)


@pytest.fixture
def built(http_mock, monkeypatch):
    """SPX on the list; GOOD reports the next session and passes the gates; the IBKR year of SPX."""
    mock = UWOptions()
    mock.calendar[(NEXT, "premarket")] = ["GOOD"]
    mock.screener["GOOD"] = dict(LIQUID)
    http_mock.add("uw.test/api", mock)
    gw = FakeGateway(last_bar=SESSION)
    monkeypatch.setattr(ibkr_tws, "CONNECT", gw.connect)
    for name in (uw_list.NAME, uw_screener.NAME, ibkr_iv.NAME):
        assert base.run_request({"dataset": name, "params": {}}, sleep=lambda s: None)["result"] != "failed"
    return mock


def test_every_options_table_has_a_dataset_and_the_options_kind():
    from RevenueChaser.rcdata.build.options import tables
    for name in tables.TABLES:
        assert read.KIND_OF_TABLE[name] == "options"
        assert read.DATASET_OF_TABLE[name] in (uw_list.NAME, uw_screener.NAME)


def test_no_options_file_yet_reads_as_no_rows():
    assert read.option_chain("SPX") == [] and read.option_screener(SESSION) == []


def test_the_chain_of_the_newest_session_comes_with_provenance_and_status(built):
    rows = read.option_chain("spx")
    assert rows and {r["session_date"] for r in rows} == {SESSION} and {r["ticker"] for r in rows} == {"SPX"}
    first = rows[0]
    for col in ("source", "source_as_of", "fetched_at", "run_id", "observed", "status"):
        assert col in first
    assert first["status"] == "changed" and first["observed"] is True
    assert [(r["expiry"], r["strike"], r["option_type"]) for r in rows] == sorted(
        (r["expiry"], r["strike"], r["option_type"]) for r in rows)
    assert read.option_chain("SPX", SESSION) == rows


def test_the_earnings_set_rows_carry_the_screener_dataset_s_status(built):
    ops.record_check(uw_screener.NAME, "failed", note="test")
    strikes = read.option_strikes("GOOD", SESSION, set_name="earnings")
    assert strikes and {r["status"] for r in strikes} == {"failed"}
    assert {r["status"] for r in read.option_strikes("SPX", SESSION)} == {"changed"}
    er = read.earnings_options(tickers=["good"])
    assert [(r["ticker"], r["report_date"], r["status"]) for r in er] == [("GOOD", NEXT, "failed")]


def test_iv_history_tells_the_observed_day_from_the_backfilled_year(built):
    rows = read.iv_history(["SPX"], days=30)
    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    assert {r["observed"] for r in by_source["uw"]} == {True} and len(by_source["uw"]) == 1
    assert {r["observed"] for r in by_source["ibkr"]} == {False} and len(by_source["ibkr"]) == 252
    assert {r["status"] for r in by_source["ibkr"]} == {"changed"}
    only = read.iv_history(["SPX"], start=SESSION, end=SESSION, days=30, source="ibkr")
    assert [(r["session_date"], r["source"]) for r in only] == [(SESSION, "ibkr")]


def test_prints_daily_expiries_and_the_screener(built):
    prints = read.option_prints("SPX", SESSION)
    assert len(prints) == 50 and prints[0]["executed_at"] > prints[-1]["executed_at"]
    assert {p["side"] for p in prints} == {"ask"}
    assert read.option_expiries("SPX", SESSION) and read.option_daily(["SPX"])[0]["ticker"] == "SPX"
    screener = read.option_screener(SESSION)
    assert [(r["ticker"], r["passes_gates"]) for r in screener] == [("GOOD", True)]
    assert read.option_screener(SESSION, passing_only=True) == screener


def test_each_read_is_counted_in_the_read_log(built):
    ops.read_log.flush()
    read.option_chain("SPX")
    read.iv_history(["SPX"])
    read.iv_history(["SPX"])
    ops.read_log.flush()
    with ops.connect() as con:
        reads = dict(con.execute("SELECT table_name, reads FROM read_log WHERE process = 'test'").fetchall())
    assert reads["option_eod"] == 1 and reads["iv_daily"] == 2
