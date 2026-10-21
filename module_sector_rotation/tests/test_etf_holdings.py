"""Tests for the multi-source ETF holdings fallback chain."""
from RevenueChaser.module_sector_rotation.etf_holdings import (
    get_etf_holdings,
    fetch_from_unusual_whales,
    fetch_from_fmp,
    _norm_weight,
)


class _FakeUW:
    def __init__(self, rows):
        self._rows = rows

    def etf_holdings(self, ticker):
        return self._rows


class TestNormWeight:
    def test_float(self):
        assert _norm_weight("5.5") == 5.5

    def test_invalid(self):
        assert _norm_weight(None) == 0.0
        assert _norm_weight("abc") == 0.0


class TestUnusualWhales:
    def test_normalizes_and_aliases(self):
        uw = _FakeUW([{"ticker": "NVDA", "name": "NVIDIA", "weight": "14.2"},
                      {"symbol": "AAPL", "name": "", "weight": 12.1}])
        rows = fetch_from_unusual_whales("XLK", uw)
        assert rows[0]["ticker"] == "NVDA"
        assert rows[1]["ticker"] == "AAPL", "symbol alias should be accepted"


class TestFmp:
    def test_no_key_returns_empty(self):
        assert fetch_from_fmp("SKYY", "") == []

    def test_weight_over_100_scaled(self, monkeypatch):
        import RevenueChaser.module_sector_rotation.etf_holdings as eh
        monkeypatch.setattr("httpx.get", lambda *a, **k: _FmpResp())
        rows = fetch_from_fmp("SKYY", "fakekey")
        assert rows[0]["weight"] == 5.25, "weight > 100 should be /100"


class _FmpResp:
    def raise_for_status(self):
        pass

    def json(self):
        return [{"asset": "NTNX", "name": "Nutanix", "weightPercentage": 525.0}]


class TestChain:
    def test_uw_first(self):
        uw = _FakeUW([{"ticker": "A", "name": "", "weight": 5.0}])
        assert get_etf_holdings("X", uw)[0]["ticker"] == "A"

    def test_yfinance_fallback_when_uw_empty(self, monkeypatch):
        import RevenueChaser.module_sector_rotation.etf_holdings as eh
        uw = _FakeUW([])
        monkeypatch.setattr(eh, "fetch_from_yfinance", lambda t: [{"ticker": "NTNX", "name": "Nutanix", "weight": 4.29}])
        rows = get_etf_holdings("SKYY", uw)
        assert rows[0]["ticker"] == "NTNX"
        assert rows[0]["weight"] == 4.29
