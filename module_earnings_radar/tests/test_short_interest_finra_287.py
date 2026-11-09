"""#287 (owner's B, 9 Nov 2026): R5 scores FINRA's twice-monthly settlement short interest from the ownership store.

``store_reads.short_interest`` reads ``finra_short_interest`` (shares short, days to cover), the float from the prices
store and the borrow fee from IBKR's daily snapshot. The age limit is in calendar days of the settlement date, the
series' own terms, not trading sessions; nothing is asked of UW.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.module_earnings_radar import store_reads, stored_inputs
from RevenueChaser.rcdata import read
from RevenueChaser.rcdata.needs import catalog

TODAY = date(2026, 11, 9)
SETTLED = date(2026, 10, 15)
FEE_SESSION = date(2026, 11, 6)        # the Friday before Monday 9 Nov: the latest complete session at the open


def _finra(ticker, day, short, dtc, symbol=None):
    return {"ticker": ticker, "symbol": symbol or ticker, "settlement_date": day, "current_short": short,
            "days_to_cover": dtc}


@pytest.fixture
def store(monkeypatch):
    """Fakes for the four ``rcdata.read`` calls; each fake honours the bounds it is asked for."""
    s = {"finra": [], "ids": {}, "floats": [], "fees": [], "asked": {}}

    def finra(tickers, start=None, end=None):
        s["asked"]["finra"] = (sorted(tickers), start, end)
        return [r for r in s["finra"] if r["ticker"] in set(tickers)
                and (start is None or r["settlement_date"] >= start) and (end is None or r["settlement_date"] <= end)]

    monkeypatch.setattr(read, "finra_short_interest", finra)
    monkeypatch.setattr(read, "security_ids", lambda tickers: {t: s["ids"][t] for t in tickers if t in s["ids"]})
    monkeypatch.setattr(read, "shares", lambda ids, measure=None, start=None, end=None:
                        [r for r in s["floats"] if r["security_id"] in set(ids) and measure in (None, r["measure"])])
    monkeypatch.setattr(read, "shortable", lambda tickers=None, session=None:
                        [r for r in s["fees"] if r["ticker"] in set(tickers)])
    monkeypatch.setattr(read, "dataset_status", lambda dataset: "ok")
    monkeypatch.setattr(calendar, "latest_complete_session", lambda *a, **k: FEE_SESSION)
    return s


def test_the_newest_settlement_gives_si_of_float_days_to_cover_and_the_borrow_fee(store):
    store["finra"] = [_finra("AAPL", SETTLED - timedelta(days=15), 1_000_000, 2.0),
                      _finra("AAPL", SETTLED, 3_000_000, 4.5)]
    store["ids"] = {"AAPL": "sec:1"}
    store["floats"] = [{"security_id": "sec:1", "measure": "float", "as_of": date(2026, 9, 1), "value": 20_000_000.0},
                       {"security_id": "sec:1", "measure": "float", "as_of": date(2026, 10, 1), "value": 10_000_000.0}]
    store["fees"] = [{"ticker": "AAPL", "session_date": FEE_SESSION, "fee_rate": 0.25}]

    rows, note = store_reads.short_interest(["aapl"], today=TODAY)

    assert rows == {"AAPL": {"si_pct": pytest.approx(30.0), "days_to_cover": 4.5, "borrow_fee": 0.25,
                             "settlement_date": "2026-10-15"}}
    assert note["stored"] is True
    assert note["source_as_of"] == "2026-10-15"
    assert note["source"] == store_reads.SHORT_SOURCE
    assert "settlement 2026-10-15" in note["label"]


def test_a_settlement_older_than_the_age_limit_is_not_read(store):
    limit = store_reads.MAX_AGE_DAYS["short_interest"]
    store["finra"] = [_finra("AAPL", TODAY - timedelta(days=limit), 1, 1.0),
                      _finra("MSFT", TODAY - timedelta(days=limit + 1), 1, 1.0)]

    rows, note = store_reads.short_interest(["AAPL", "MSFT"], today=TODAY)

    assert list(rows) == ["AAPL"]
    assert store["asked"]["finra"] == (["AAPL", "MSFT"], TODAY - timedelta(days=limit), TODAY)
    assert "1 of 2" in note["label"]


def test_no_float_leaves_si_out_but_keeps_days_to_cover(store):
    store["finra"] = [_finra("AAPL", SETTLED, 3_000_000, 4.5)]

    rows, _ = store_reads.short_interest(["AAPL"], today=TODAY)

    assert rows["AAPL"]["si_pct"] is None
    assert rows["AAPL"]["days_to_cover"] == 4.5
    assert rows["AAPL"]["borrow_fee"] is None


def test_a_borrow_fee_from_an_old_snapshot_is_not_read(store):
    store["finra"] = [_finra("AAPL", SETTLED, 3_000_000, 4.5)]
    store["fees"] = [{"ticker": "AAPL", "session_date": date(2026, 10, 1), "fee_rate": 9.0}]

    rows, _ = store_reads.short_interest(["AAPL"], today=TODAY)

    assert rows["AAPL"]["borrow_fee"] is None


def test_the_row_of_the_ticker_s_own_symbol_wins_a_tie(store):
    store["finra"] = [_finra("BRK-B", SETTLED, 5, 1.0, symbol="BRK.B"), _finra("BRK-B", SETTLED, 7, 2.0)]

    rows, _ = store_reads.short_interest(["BRK-B"], today=TODAY)

    assert rows["BRK-B"]["days_to_cover"] == 2.0


def test_a_store_error_is_not_stored_and_never_a_live_read(store, monkeypatch):
    monkeypatch.setattr(read, "finra_short_interest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy")))

    rows, note = store_reads.short_interest(["AAPL"], today=TODAY)

    assert rows == {}
    assert note["stored"] is False
    assert "read failed" in note["label"]


def test_the_need_counts_calendar_days_of_the_settlement_date():
    need = catalog.NEEDS["ownership.short_interest"]
    assert (need.max_lag, need.max_lag_unit) == (35, catalog.DAYS)
    assert store_reads.MAX_AGE_DAYS["short_interest"] == 35


def test_the_old_nightly_table_has_no_reader_on_this_board():
    assert not hasattr(stored_inputs, "short_rows")
    assert "short_interest" not in stored_inputs.MAX_LAG


# ── the board's warm step ──

class _NoShortsUW:
    def short_screener(self, tickers):                   # pragma: no cover - the failure this test is about
        raise AssertionError("the board must not ask UW for short interest (#287 B)")


def test_the_run_warms_r5_from_the_ownership_store_and_asks_uw_nothing(store):
    from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline

    store["finra"] = [_finra("AAPL", SETTLED, 3_000_000, 4.5)]
    p = EarningsPipeline(["AAPL", "MSFT"], uw=_NoShortsUW(), cal_meta={})
    p._spine_gics = {}

    cache = p._warm_short_interest()

    assert cache["AAPL"]["days_to_cover"] == 4.5
    assert p.input_sources["short_interest"] == {"stored": 1, "missing": 1, "source_as_of": "2026-10-15"}
    assert "settlement 2026-10-15" in p.input_sources["short_interest_note"]


# ── the card says how old the figure is, in the series' own terms ──

def test_the_squeeze_reason_names_the_settlement_day():
    from RevenueChaser.module_earnings_radar.rules import rule5_positioning

    rule5_positioning.reset_peer_stats()
    r = rule5_positioning.score(short_interest_pct_float=25.0, short_as_of="2026-10-15")
    assert r.reasons == ["high short interest (25.0%) · FINRA settled 15 Oct"]
    assert r.raw_metrics["si_as_of"] == "2026-10-15"

    r = rule5_positioning.score(short_interest_pct_float=25.0)
    assert r.reasons == ["high short interest (25.0%)"]
    assert "si_as_of" not in r.raw_metrics
