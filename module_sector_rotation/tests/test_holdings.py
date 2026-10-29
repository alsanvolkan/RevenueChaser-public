"""Tests for ETF holdings + SPY-relative strength (engine, cache, fragment, route)."""
from datetime import date, timedelta

from RevenueChaser.module_sector_rotation.engine import holdings_rs_rows
from RevenueChaser.module_sector_rotation.report import render_holdings_fragment

# The holdings RS is a prices-store read now (``bar_points`` -> ``holdings_rs_rows``): the UW candle path and its
# normalizers are gone, so the rules below are pinned on the stored ``(session, close)`` points.
AS_OF = date(2026, 8, 14)
SESSIONS = [AS_OF - timedelta(days=69 - i) for i in range(70)]


def _points(closes: list[float]) -> list[tuple]:
    return list(zip(SESSIONS, closes))


FLAT_SPY = _points([100.0] * 70)
UP = _points([100.0 + i for i in range(70)])
DOWN = _points([200.0 - i for i in range(70)])


class TestHoldingsRs:
    def test_a_holding_with_no_stored_bar_keeps_its_row_with_no_rs(self):
        """It used to be dropped silently when UW had no candle; the store path shows it as "n/a" instead."""
        holdings = [{"ticker": "BIG", "name": "Big", "weight": "5.0"},
                    {"ticker": "FOREIGN", "name": "Foreign", "weight": "9.0"},
                    {"ticker": "DN", "name": "Down", "weight": "4.0"}]
        rows = holdings_rs_rows(holdings, {"BIG": UP, "DN": DOWN}, FLAT_SPY, AS_OF)
        by = {r["ticker"]: r for r in rows}
        assert by["FOREIGN"]["close"] is None and by["FOREIGN"]["rs_1m"] is None
        assert by["BIG"]["rs_1m"] > 0                       # rising against a flat SPY
        assert by["BIG"]["close"] == 169.0
        assert by["DN"]["rs_1m"] < 0

    def test_capped_at_max_holdings_and_sorted_by_weight(self):
        holdings = [{"ticker": f"T{i}", "name": f"T{i}", "weight": f"{100 - i}"} for i in range(15)]
        rows = holdings_rs_rows(holdings, {f"T{i}": UP for i in range(15)}, FLAT_SPY, AS_OF, max_holdings=10)
        assert len(rows) == 10
        assert [r["weight"] for r in rows] == sorted((r["weight"] for r in rows), reverse=True)

    def test_no_holdings_is_no_rows(self):
        assert holdings_rs_rows([], {}, FLAT_SPY, AS_OF) == []

    def test_a_short_spy_series_computes_nothing(self):
        assert holdings_rs_rows([{"ticker": "BIG", "weight": "5.0"}], {"BIG": UP}, FLAT_SPY[:10], AS_OF) == []


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
