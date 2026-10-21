"""E5c (#372): R5 reads the ``short_interest`` rows the nightly D10 sweep stored and asks UW's ``/short_screener``
only for the tickers the store does not cover.

Offline: a temp store seeded with the stored rows, a fake client counting its batch calls. The stored row must give
the very same R5 inputs (and so the same R5 read) as the live one, because the reader parses it with the client's
own ``_parse_short_row``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_earnings_radar import stored_inputs
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.rules import rule5_positioning
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

SESSION = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)          # 17:00 New York: the session is complete

LIVE = {
    "AAPL": {"si_float": "0.35", "fee_rate": "10", "short_interest": "1000000",
             "short_shares_available": "100000", "days_to_cover": "8", "total_float": "3000000",
             "rebate_rate": "1.5"},
    "MSFT": {"si_float": "0.03", "fee_rate": "1", "short_interest": "100000",
             "short_shares_available": "5000000", "days_to_cover": "1", "total_float": "9000000",
             "rebate_rate": "4.0"},
}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


def _seed(rows, session=SESSION):
    rawstore.write_rows("short_interest", [{"ticker": t, "session_date": session, "source": "uw_short_screener",
                                            **vals} for t, vals in rows.items()])


class _ShortUW(_FakeUW):
    """Serves the live rows and counts the batches it was asked for (mirrors ``test_pipeline_score``)."""

    def __init__(self, rows=None, **over):
        super().__init__(**over)
        self.rows = LIVE if rows is None else rows
        self.asked = []

    def short_screener(self, tickers):
        self.asked.append(list(tickers))
        return {t: self.rows[t] for t in tickers if t in self.rows}

    def _parse_short_row(self, row):
        return UWClient._parse_short_row(row)


def _pipeline(fake, tickers=("AAPL", "MSFT")):
    cal = {t: {"earnings_date": "2026-10-20", "full_name": t, "sector": "Technology",
               "expected_move_perc": 0.05} for t in tickers}
    p = EarningsPipeline(list(tickers), uw=fake, cal_meta=cal)
    p._screener_cache = {}
    rule5_positioning.reset_peer_stats()
    return p


def test_stored_rows_give_the_same_inputs_and_rule5_result():
    live_p = _pipeline(_ShortUW())
    live_p._warm_short_interest()            # the store is still empty: this is the live read
    _seed(LIVE)
    stored_p = _pipeline(_ShortUW())
    stored_p._warm_short_interest()

    assert stored_p._shorts_cache == live_p._shorts_cache
    assert stored_p._shorts_cache["AAPL"]["si_pct"] == pytest.approx(35.0)
    assert stored_p._shorts_cache["AAPL"]["utilization"] == pytest.approx(100.0 * 1_000_000 / 1_100_000)

    a = stored_p._score("AAPL").rule_audit["rule5_positioning"]
    b = live_p._score("AAPL").rule_audit["rule5_positioning"]
    assert (a["score"], a["available"], a["reasons"]) == (b["score"], b["available"], b["reasons"])


def test_all_stored_makes_no_short_screener_call():
    _seed(LIVE)
    fake = _ShortUW()
    p = _pipeline(fake)

    p._warm_short_interest()

    assert fake.asked == []
    assert sorted(p._shorts_cache) == ["AAPL", "MSFT"]
    assert p.input_sources["short_interest"] == {"stored": 2, "live": 0, "session": "2026-10-14"}


def test_only_the_missing_ticker_is_asked_for():
    _seed({"AAPL": LIVE["AAPL"]})
    fake = _ShortUW()
    p = _pipeline(fake)

    p._warm_short_interest()

    assert fake.asked == [["MSFT"]]
    assert sorted(p._shorts_cache) == ["AAPL", "MSFT"]
    assert p.input_sources["short_interest"] == {"stored": 1, "live": 1, "session": "2026-10-14"}


def test_a_session_older_than_the_lag_reads_live():
    _seed(LIVE, session=date(2026, 10, 1))               # 4 sessions back, MAX_LAG is 3
    fake = _ShortUW()
    p = _pipeline(fake)

    p._warm_short_interest()

    assert fake.asked == [["AAPL", "MSFT"]]
    assert p.input_sources["short_interest"] == {"stored": 0, "live": 2, "session": None}


def test_a_yfinance_row_is_used_as_stored():
    """The D10 sweep falls back to yfinance (``si_float`` and ``total_float`` only): it is still a stored row."""
    rawstore.write_rows("short_interest", [{"ticker": "AAPL", "session_date": SESSION, "source": "yfinance",
                                            "si_float": 0.35, "total_float": 3000000}])
    fake = _ShortUW()
    p = _pipeline(fake)

    p._warm_short_interest()

    assert fake.asked == [["MSFT"]]
    assert p._shorts_cache["AAPL"]["si_pct"] == pytest.approx(35.0)
    assert p._shorts_cache["AAPL"]["days_to_cover"] is None


def test_short_rows_filters_to_the_asked_tickers():
    _seed(LIVE)
    rows, session = stored_inputs.short_rows(["MSFT"])
    assert session == SESSION
    assert list(rows) == ["MSFT"]
    assert rows["MSFT"]["fee_rate"] == 1.0
    assert "market_date" not in rows["MSFT"]              # not an R5 input


def test_short_rows_drops_null_cells():
    rawstore.write_rows("short_interest", [{"ticker": "AAA", "session_date": SESSION, "source": "yfinance",
                                            "si_float": 0.35, "days_to_cover": None}])
    rows, _ = stored_inputs.short_rows(["AAA"])
    assert rows == {"AAA": {"si_float": 0.35}}


def test_short_rows_of_a_broken_store_is_empty(monkeypatch):
    _seed(LIVE)
    monkeypatch.setattr(rawstore, "query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy")))
    assert stored_inputs.short_rows(["AAPL"]) == ({}, None)
