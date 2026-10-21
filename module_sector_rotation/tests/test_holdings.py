"""Tests for ETF holdings + SPY-relative strength (engine, cache, fragment, route)."""
from datetime import date

from RevenueChaser.module_sector_rotation.engine import holdings_with_rs, _normalize_ohlc, _closes_from
from RevenueChaser.module_sector_rotation.report import render_holdings_fragment


class TestNormalizeOhlc:
    def test_sorts_descending_weekly_ascending(self):
        # UW weekly endpoint returns newest-first with 3 rows/week (pr/r/po)
        rows = [
            {"date": "2026-08-10", "close": "167.76", "market_time": "pr"},
            {"date": "2026-08-10", "close": "167.37", "market_time": "r"},
            {"date": "2026-08-10", "close": "167.37", "market_time": "po"},
            {"date": "2026-08-03", "close": "160.0", "market_time": "r"},
            {"date": "2026-04-13", "close": "147.25", "market_time": "pr"},
        ]
        norm = _normalize_ohlc(rows)
        dates = [r["date"] for r in norm]
        assert dates == sorted(dates), "sorted ascending by date"
        assert len(norm) == 3, "one row per date after dedupe"

    def test_prefers_regular_close(self):
        rows = [
            {"date": "2026-08-10", "close": "167.76", "market_time": "pr"},
            {"date": "2026-08-10", "close": "167.37", "market_time": "r"},
            {"date": "2026-08-10", "close": "167.40", "market_time": "po"},
        ]
        norm = _normalize_ohlc(rows)
        assert norm[0]["close"] == "167.37", "regular 'r' close preferred"

    def test_closes_last_is_latest(self):
        rows = [
            {"date": "2026-04-13", "close": "147.25", "market_time": "pr"},
            {"date": "2026-08-10", "close": "167.37", "market_time": "r"},
        ]
        closes = _closes_from(rows)
        assert closes[-1] == 167.37, "last close is the latest date, not the oldest"


class _FakeUW:
    def __init__(self, holdings=None, spy_ohlc=None, ohlc=None):
        self._holdings = holdings or []
        self._spy_ohlc = spy_ohlc or []
        self._ohlc = ohlc or {}

    def etf_holdings(self, ticker):
        return self._holdings

    def etf_ohlc(self, ticker, days=90, candle_size="1d"):
        if ticker == "SPY":
            return self._spy_ohlc
        return self._ohlc.get(ticker, [])


def _ohlc(closes):
    return [{"date": f"2026-0{1+i//20}-{1+i%20:02d}", "close": c} for i, c in enumerate(closes)]


class TestHoldingsWithRs:
    def test_drops_no_ohlc_listing_and_rs(self):
        # spy flat; holding rising => positive RS
        spy = _ohlc([100.0] * 70)
        up = _ohlc([100.0 + i for i in range(70)])   # rising
        down = _ohlc([200.0 - i for i in range(70)])  # falling
        uw = _FakeUW(
            holdings=[
                {"ticker": "BIG", "name": "Big", "weight": "5.0"},
                {"ticker": "FOREIGN", "name": "Foreign", "weight": "9.0"},  # no OHLC -> skipped
                {"ticker": "DN", "name": "Down", "weight": "4.0"},
            ],
            spy_ohlc=spy,
            ohlc={"BIG": up, "DN": down},  # FOREIGN has no OHLC entry
        )
        rows = holdings_with_rs("XLK", uw)
        tickers = [r["ticker"] for r in rows]
        assert "FOREIGN" not in tickers, "listings with no OHLC data are excluded"
        big = [r for r in rows if r["ticker"] == "BIG"][0]
        assert big["rs_1m"] > 0, "rising stock vs flat SPY => positive RS"
        assert big["close"] == 169.0, "close comes from the last OHLC close"
        dn = [r for r in rows if r["ticker"] == "DN"][0]
        assert dn["rs_1m"] < 0, "falling stock vs flat SPY => negative RS"

    def test_caps_at_max_holdings(self):
        spy = _ohlc([100.0] * 70)
        up = _ohlc([100.0 + i for i in range(70)])
        holdings = [{"ticker": f"T{i}", "name": f"T{i}", "weight": f"{100 - i}"}
                    for i in range(15)]
        uw = _FakeUW(holdings=holdings, spy_ohlc=spy, ohlc={f"T{i}": up for i in range(15)})
        rows = holdings_with_rs("XLK", uw, max_holdings=10)
        assert len(rows) == 10, "capped at max_holdings"
        weights = [r["weight"] for r in rows]
        assert weights == sorted(weights, reverse=True), "sorted by weight desc"

    def test_empty_holdings(self):
        assert holdings_with_rs("XLK", _FakeUW([])) == []


class TestRenderHoldingsFragment:
    def test_fragment(self):
        rows = [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2, "close": 150.0,
                 "rs_1w": 2.1, "rs_1m": 5.3, "rs_3m": 9.8}]
        frag = render_holdings_fragment(rows)
        assert "NVDA" in frag
        assert "Top holdings" in frag
        assert "RS 1W" in frag and "RS 1M" in frag and "RS 3M" in frag
        assert "14.20%" in frag
        assert "Weight (14.2%)" in frag, "header should show Weight (sum%)"

    def test_fragment_column_order(self):
        rows = [{"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2, "close": 150.0,
                 "rs_1w": 2.1, "rs_1m": 5.3, "rs_3m": 9.8}]
        frag = render_holdings_fragment(rows)
        idx_weight = frag.find("Weight (")
        idx_r1 = frag.find("RS 1W")
        idx_price = frag.find(">Price<")
        assert idx_weight < idx_r1 < idx_price, "columns: Weight then RS then Price"

    def test_fragment_empty(self):
        frag = render_holdings_fragment([])
        assert "No holdings data available" in frag


class TestHoldingsCache:
    def test_roundtrip_today(self, rawstore_tmp_db, monkeypatch):
        import RevenueChaser.module_sector_rotation.store as rs
        monkeypatch.setattr(rs, "date", date)  # ensure date.today() used
        rs.save_holdings_cache("XLK", [{"ticker": "NVDA"}])
        rows = rs.load_holdings_cache("XLK")
        assert rows == [{"ticker": "NVDA"}]

    def test_stale_cache_none(self, rawstore_tmp_db):
        from RevenueChaser.core import rawstore
        rawstore.write_rows("rotation_holdings", [{
            "ticker": "XLK", "date": "2020-01-01", "rows": [{"ticker": "NVDA"}],
        }])
        import RevenueChaser.module_sector_rotation.store as rs
        assert rs.load_holdings_cache("XLK") is None
