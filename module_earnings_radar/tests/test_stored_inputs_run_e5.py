"""E5/E6 (#372) through ``EarningsPipeline.run()``: with every nightly input stored and fresh, a run asks UW for no
short-interest batch and no year of bars, does not call FRED, and scores R1 from the stored estimates (review S2).

The slice tests call ``_score`` with the caches set by hand; this one pins that ``run()`` itself warms them.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core import calendar, rawstore
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_rule1_drivers_e6 import FULL
from RevenueChaser.module_earnings_radar.tests.test_stored_ohlc_e5e import _bars, _prints, _seed as _seed_bars
from RevenueChaser.module_earnings_radar.tests.test_stored_short_e5c import LIVE, _seed as _seed_short
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

NY = ZoneInfo("America/New_York")
SESSION = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc)          # Wed 17:00 New York: 10-14 is complete
REPORT = "2026-10-20"                                               # Tue: window Mon 10-19 .. Wed 10-21


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def _store(rawstore_tmp_db):
    return rawstore_tmp_db


class _CountingUW(_FakeUW):
    def __init__(self):
        super().__init__(earnings_history=_prints(),
                         screener_batch={"AAPL": {"close": "100", "call_volume": "1000", "put_volume": "1000",
                                                  "total_open_interest": "2000"}})
        self.ohlc_asked, self.shorts_asked = [], []

    def etf_ohlc(self, ticker, days=60, candle_size="1d"):
        self.ohlc_asked.append(ticker)
        return []

    def short_screener(self, tickers):
        self.shorts_asked.append(list(tickers))
        return {}


def _seed_everything():
    _seed_bars(_bars(300))
    _seed_short({"AAPL": LIVE["AAPL"]})
    rawstore.write_rows("estimate_snapshots", [{"ticker": "AAPL", "session_date": SESSION, "period": "0q",
                                                "source": "yfinance", **FULL}])
    rawstore.write_rows("macro_series", [
        {"series_id": "DGS10", "obs_date": SESSION - timedelta(days=i), "source": "fred", "value": 4.0 + i / 100}
        for i in range(1, 90)])
    rawstore.write_rows("economic_calendar", [
        {"session_date": SESSION, "event_ts": datetime(d.year, d.month, d.day, 8, 30, tzinfo=NY).timestamp(),
         "event": "GDP", "type": "report", "source": "uw"} for d in (date(2026, 10, 15), date(2026, 10, 23))])


def test_a_run_with_every_input_stored_asks_uw_and_fred_for_none_of_them(monkeypatch):
    from RevenueChaser.module_earnings_radar import macro_watch
    monkeypatch.setattr(macro_watch, "fred_daily_map", lambda *a, **k: pytest.fail("FRED must not be called"))
    _seed_everything()
    fake = _CountingUW()
    p = EarningsPipeline(["AAPL"], uw=fake, cal_meta={"AAPL": {
        "earnings_date": REPORT, "full_name": "Aple", "sector": "Technology", "expected_move_perc": 0.05}})

    (card,) = p.run(use_preselect=False, watchlist={"ZZZZ"})

    assert fake.ohlc_asked == []                                   # the year of bars came from D01
    assert fake.shorts_asked == []                                 # R5 read the stored short row
    assert card.rule_audit["rule1_drivers"]["available"] is True   # run() warmed the stored estimates
    assert p.input_sources["ohlc"] == {"stored": 1, "live": 0}
    assert p.input_sources["short_interest"] == {"stored": 1, "live": 0, "session": SESSION.isoformat()}
    assert p.input_sources["estimates"] == {"stored": 1, "missing": 0}
    assert p.input_sources["dgs10"] == "stored"
    assert p.input_sources["rate_events"] == "stored"


def test_the_same_run_with_an_empty_store_asks_uw(monkeypatch):
    """The reference: without the store the same run asks UW for both, so the zeros above are the store's doing."""
    from RevenueChaser.module_earnings_radar import macro_watch
    monkeypatch.setattr(macro_watch, "fred_daily_map", lambda *a, **k: {})
    fake = _CountingUW()
    p = EarningsPipeline(["AAPL"], uw=fake, cal_meta={"AAPL": {
        "earnings_date": REPORT, "full_name": "Aple", "sector": "Technology", "expected_move_perc": 0.05}})

    (card,) = p.run(use_preselect=False, watchlist={"ZZZZ"})

    assert fake.ohlc_asked == ["AAPL"]
    assert fake.shorts_asked == [["AAPL"]]
    assert card.rule_audit["rule1_drivers"]["available"] is False
