"""E5/E6 (#372) through ``EarningsPipeline.run()``: with every nightly input stored and fresh, a run asks UW for no
short-interest batch and no year of bars, does not call FRED, and scores R1 from the stored estimates (review S2).
Since G9 R5's short inputs come from the ownership store, and no run asks UW for them.

The slice tests call ``_score`` with the caches set by hand; this one pins that ``run()`` itself warms them.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.rcdata import ops
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_rule1_drivers_e6 import FULL
from RevenueChaser.module_earnings_radar.tests.test_stored_ohlc_e5e import _bars, _prints, _seed as _seed_bars
from RevenueChaser.module_earnings_radar.tests.test_r5_ownership_store_g9 import _write as _write_ownership
from RevenueChaser.module_sec_13f.tests.ownership_store import market  # noqa: F401 - the fixture
from RevenueChaser.module_earnings_radar.tests.options_store import list_name, screener_row, write_series
from RevenueChaser.module_oipd.tests.options_store import write_options
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

SESSION = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)          # Wed 17:00 New York: 10-14 is complete
REPORT = "2026-10-20"                                               # Tue: window Mon 10-19 .. Wed 10-21


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db, market):  # noqa: F811
    return market


class _CountingUW(_FakeUW):
    def __init__(self):
        super().__init__(earnings_history=_prints())        # the screener rows are stored since G9
        self.ohlc_asked, self.shorts_asked = [], []

    def etf_ohlc(self, ticker, days=60, candle_size="1d"):
        self.ohlc_asked.append(ticker)
        return []

    def short_screener(self, tickers):
        self.shorts_asked.append(list(tickers))
        return {}


def _seed_everything(market):
    _seed_bars(_bars(300))
    _write_ownership(market, finra=[("AAPL", date(2026, 9, 30), 1_000_000, 2.0, date(2026, 10, 9))],
                     ibkr=[("AAPL", SESSION, 35.0, -30.0, 120_000, False)])
    rawstore.write_rows("estimate_snapshots", [{"ticker": "AAPL", "session_date": SESSION, "period": "0q",
                                                "source": "yfinance", **FULL}])
    # R4/R5/R8 (G9): AAPL is on the option watch list, with its list rows of the session.
    ops.watch_add("option", "AAPL", by="test")
    list_name("AAPL", SESSION)
    write_options(option_screener_daily=[screener_row("AAPL", SESSION, date.fromisoformat(REPORT))])
    # R6 (G9): the 10-year and the FOMC/CPI days from ``market_series``; both rate calendars span the report.
    write_series([("DGS10", SESSION - timedelta(days=i), 4.0 + i / 100) for i in range(0, 90)]
                 + [(sid, d, 1.0) for sid in ("FOMC_DECISION", "CPI_RELEASE")
                    for d in (date(2026, 1, 28), date(2026, 12, 9))], market=market)


def test_a_run_with_every_input_stored_asks_uw_and_fred_for_none_of_them(monkeypatch, _store):
    from RevenueChaser.module_earnings_radar import macro_watch
    monkeypatch.setattr(macro_watch, "fred_daily_map", lambda *a, **k: pytest.fail("FRED must not be called"))
    _seed_everything(_store)
    fake = _CountingUW()
    p = EarningsPipeline(["AAPL"], uw=fake, cal_meta={"AAPL": {
        "earnings_date": REPORT, "full_name": "Aple", "sector": "Technology", "expected_move_perc": 0.05}})

    (card,) = p.run(use_preselect=False, watchlist={"ZZZZ"})

    assert fake.ohlc_asked == []                                   # the year of bars came from D01
    assert fake.shorts_asked == []                                 # R5 read the stored short row
    assert card.rule_audit["rule1_drivers"]["available"] is True   # run() warmed the stored estimates
    assert p.input_sources["ohlc"] == {"stored": 1, "live": 0}
    assert p.input_sources["short_interest"] == {"stored": 1, "live": 0, "source": "rcdata:ownership",
                                                 "session": "2026-09-30"}
    assert "squeeze fuel (borrow fee 35.0%)" in card.rule_audit["rule5_positioning"]["reasons"]
    assert p.input_sources["estimates"] == {"stored": 1, "missing": 0}
    assert p.input_sources["dgs10"] == "stored"
    assert p.input_sources["rate_events"] == "stored"


def test_the_same_run_with_an_empty_store_asks_uw(monkeypatch):
    """The reference: without the store the same run asks UW for the bars, so the zero above is the store's doing.
    Short interest is never asked of UW since G9: an empty ownership store is no squeeze inputs."""
    from RevenueChaser.module_earnings_radar import macro_watch
    monkeypatch.setattr(macro_watch, "fred_daily_map", lambda *a, **k: {})
    fake = _CountingUW()
    p = EarningsPipeline(["AAPL"], uw=fake, cal_meta={"AAPL": {
        "earnings_date": REPORT, "full_name": "Aple", "sector": "Technology", "expected_move_perc": 0.05}})

    (card,) = p.run(use_preselect=False, watchlist={"ZZZZ"})

    assert fake.ohlc_asked == ["AAPL"]
    assert fake.shorts_asked == []
    assert card.rule_audit["rule1_drivers"]["available"] is False
