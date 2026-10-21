"""Tests for calendar augmentation (watchlist tickers with unknown/estimated earnings)."""
from datetime import date

from RevenueChaser.main import _augment_watchlist


class TestAugmentWatchlist:
    def test_unknown_time_ticker_added(self):
        events, raw_cal = [], {"AAA": {}}
        screener = {
            "QXO": {
                "next_earnings_date": "2026-08-13",
                "er_time": "unknown",
                "sector": "Industrials",
                "full_name": "Silversun Technologies Inc",
                "implied_move_perc": "0.129",
            },
        }
        _augment_watchlist(events, raw_cal, {"QXO"}, screener,
                           date(2026, 8, 10), date(2026, 8, 16))
        assert "QXO" in raw_cal
        ev = raw_cal["QXO"]
        assert ev["earnings_time"] == "Other"
        assert ev["report_time"] == "unknown"
        assert ev["full_name"] == "Silversun Technologies Inc"
        assert ev["expected_move_perc"] == "0.129"
        assert ev["source"] == "screener"
        assert any(e["ticker"] == "QXO" for e in events)

    def test_already_in_calendar_skipped(self):
        events, raw_cal = [], {"QXO": {"existing": True}}
        screener = {"QXO": {"next_earnings_date": "2026-08-13", "er_time": "unknown"}}
        _augment_watchlist(events, raw_cal, {"QXO"}, screener,
                           date(2026, 8, 10), date(2026, 8, 16))
        assert raw_cal["QXO"] == {"existing": True}
        assert events == []

    def test_out_of_range_skipped(self):
        events, raw_cal = [], {}
        screener = {"QXO": {"next_earnings_date": "2026-09-01", "er_time": "unknown"}}
        _augment_watchlist(events, raw_cal, {"QXO"}, screener,
                           date(2026, 8, 10), date(2026, 8, 16))
        assert "QXO" not in raw_cal
        assert events == []

    def test_no_earnings_date_skipped(self):
        events, raw_cal = [], {}
        screener = {"QXO": {"er_time": "unknown"}}
        _augment_watchlist(events, raw_cal, {"QXO"}, screener,
                           date(2026, 8, 10), date(2026, 8, 16))
        assert "QXO" not in raw_cal

    def test_am_pm_time_mapping(self):
        events, raw_cal = [], {}
        screener = {
            "AAA": {"next_earnings_date": "2026-08-13", "er_time": "premarket"},
            "BBB": {"next_earnings_date": "2026-08-13", "er_time": "postmarket"},
        }
        _augment_watchlist(events, raw_cal, {"AAA", "BBB"}, screener,
                           date(2026, 8, 10), date(2026, 8, 16))
        assert raw_cal["AAA"]["earnings_time"] == "BMO"
        assert raw_cal["BBB"]["earnings_time"] == "AMC"
