"""Review M3 (#197 follow-up): the reaction columns need a year of prices, not 60 sessions.

The window is fetched once (same single OHLC request); the other readers still see the last 60 bars.
Offline: the fake honours ``days`` the way the real client slices (``bars[-days:]``).
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.module_earnings_radar import pipeline as pl
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW


@pytest.fixture(autouse=True)
def _isolate(rawstore_tmp_db):
    return rawstore_tmp_db


def _sessions(n, end=date(2026, 10, 7)):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return [{"date": x.isoformat(), "close": 100.0 + i * 0.1} for i, x in enumerate(reversed(days))]


class _WindowUW(_FakeUW):
    def __init__(self, series, hist, **ov):
        super().__init__(earnings_history=hist, **ov)
        self.series = series
        self.requests = []

    def etf_ohlc(self, ticker, days=60, candle_size="1d"):
        self.requests.append(days)
        return self.series[-days:]


def _prints():
    # four completed prints, one quarter (~63 sessions) apart, all before the 60-session window
    return [{"report_date": d, "actual_eps": 1.0, "street_mean_est": 0.9, "report_time": "premarket"}
            for d in ("2026-09-02", "2026-06-03", "2026-03-04", "2025-12-03")]


def _card(monkeypatch, series=None):
    from RevenueChaser.core import calendar
    now = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(calendar, "_now", lambda: now)
    uw = _WindowUW(series or _sessions(400), _prints())
    p = EarningsPipeline(["AAPL"], uw=uw, cal_meta={"AAPL": {
        "earnings_date": "2026-11-05", "full_name": "Aple", "sector": "Technology",
        "expected_move_perc": 0.05}})
    p._screener_cache = {"AAPL": {"close": "100", "call_volume": "0", "put_volume": "0",
                                  "total_open_interest": "0"}}
    return p._score("AAPL"), uw


def test_all_four_reaction_columns_fill_from_one_year_of_prices(monkeypatch):
    card, _ = _card(monkeypatch)
    assert card.earn_m1_pct and card.earn_m2_pct and card.earn_m3_pct and card.earn_m4_pct


def test_still_one_ohlc_request_and_it_asks_for_the_long_window(monkeypatch):
    _, uw = _card(monkeypatch)
    assert uw.requests.count(pl.REACTION_OHLC_SESSIONS) == 1
    assert pl.REACTION_OHLC_SESSIONS >= 260
    assert max(uw.requests) == pl.REACTION_OHLC_SESSIONS


def test_other_readers_keep_the_last_sixty_bars(monkeypatch):
    from RevenueChaser.module_earnings_radar.history import packet_ohlc
    card, _ = _card(monkeypatch)
    packet = json.loads(card.raw_packet_json)
    assert "ohlc" not in packet           # E4: rebuilt from the year, not stored twice
    assert len(packet_ohlc(packet)) == pl.READER_OHLC_SESSIONS == 60


def test_the_stored_d01_series_serves_the_reaction_window(monkeypatch):
    """E5e (#372): when D01 holds the year and its last bar is the latest complete session, the one OHLC request the
    window used to cost is not made at all."""
    from RevenueChaser.core import calendar, rawstore
    from RevenueChaser.module_earnings_radar import stored_inputs
    series = _sessions(400)
    rawstore.write_rows("price_bars_daily", [
        {"ticker": "AAPL", "session_date": date.fromisoformat(b["date"]), "open": b["close"], "high": b["close"],
         "low": b["close"], "close": b["close"], "volume": 1000.0, "source": "uw"} for b in series])
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc))
    uw = _WindowUW(series, _prints())
    p = EarningsPipeline(["AAPL"], uw=uw, cal_meta={"AAPL": {
        "earnings_date": "2026-11-05", "full_name": "Aple", "sector": "Technology",
        "expected_move_perc": 0.05}})
    p._screener_cache = {"AAPL": {"close": "100", "call_volume": "0", "put_volume": "0",
                                  "total_open_interest": "0"}}
    p._dgs10_cache = {}
    p._ohlc_bars = stored_inputs.ohlc_bars(["AAPL"])

    card = p._score("AAPL")

    assert uw.requests == []
    assert p.input_sources["ohlc"] == {"stored": 1, "live": 0}
    assert card.earn_m1_pct and card.earn_m2_pct and card.earn_m3_pct and card.earn_m4_pct
