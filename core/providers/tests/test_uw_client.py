"""Tests that UWClient methods build the correct endpoint paths (no network)."""
import pytest

from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient


def _ok(fake):
    """A ``get_result`` stand-in answering ``fake(ep, params)`` as an ok result (B5: the endpoint
    methods read through ``get_result``, not ``_get``)."""
    return lambda ep, params=None: ProviderResult("ok", fake(ep, params), None, ep)


@pytest.fixture
def client_and_calls(monkeypatch):
    calls = []

    def fake_get(ep, params=None):
        calls.append((ep, params))
        return {"data": []}

    uw = UWClient(key="test")
    monkeypatch.setattr(uw, "get_result", _ok(fake_get))
    return uw, calls


class TestUWClientPaths:
    def test_gex_levels_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.gex_levels("QXO")
        assert calls[0][0] == "/stock/QXO/gex-levels"

    def test_greek_exposure_path_and_timeframe(self, client_and_calls):
        uw, calls = client_and_calls
        uw.greek_exposure("QXO", timeframe="1M")
        assert calls[0][0] == "/stock/QXO/greek-exposure"
        assert calls[0][1] == {"timeframe": "1M"}

    def test_greek_exposure_by_strike_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.greek_exposure_by_strike("QXO")
        assert calls[0][0] == "/stock/QXO/greek-exposure/strike"

    def test_greek_exposure_by_expiry_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.greek_exposure_by_expiry("QXO")
        assert calls[0][0] == "/stock/QXO/greek-exposure/expiry"

    def test_dark_pool_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.dark_pool("QXO")
        assert calls[0][0] == "/darkpool/QXO"

    def test_greeks_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.greeks("QXO")
        assert calls[0][0] == "/stock/QXO/greeks"

    def test_short_interest_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.short_interest("QXO")
        assert calls[0][0] == "/shorts/QXO/interest-float/v2"

    def test_short_interest_dict_row(self, monkeypatch):
        """V2 returns a single object (schema) — must not do items[-1] on a dict."""
        uw = UWClient(key="test")
        monkeypatch.setattr(uw, "get_result", _ok(lambda ep, params=None: {"data": {"symbol": "QXO", "si_float": "0.05"}}))
        assert uw.short_interest("QXO") == {"symbol": "QXO", "si_float": "0.05"}

    def test_short_interest_list_row(self, monkeypatch):
        """Defensive: a list payload reads the first (newest) element."""
        uw = UWClient(key="test")
        monkeypatch.setattr(uw, "get_result", _ok(lambda ep, params=None: {"data": [{"symbol": "QXO", "si_float": "0.05"}]}))
        assert uw.short_interest("QXO") == {"symbol": "QXO", "si_float": "0.05"}

    def test_short_screener_path_and_keying(self, client_and_calls):
        uw, calls = client_and_calls
        uw.short_screener(["AAA", "BBB"])
        assert calls[0][0] == "/short_screener"
        assert calls[0][1]["tickers"] == "AAA,BBB"

    def test_short_screener_chunks_at_100(self, monkeypatch):
        uw = UWClient(key="test")
        seen = []

        def fake_get(ep, params=None):
            seen.append(params["tickers"].split(","))
            return {"data": [{"symbol": t, "si_float": "0.05"} for t in params["tickers"].split(",")]}

        monkeypatch.setattr(uw, "get_result", _ok(fake_get))
        result = uw.short_screener([f"T{i:03d}" for i in range(150)])
        assert len(result) == 150
        assert seen[0][0] == "T000" and seen[0][-1] == "T099"
        assert seen[1][0] == "T100" and seen[1][-1] == "T149"

    def test_parse_short_row_maps_fields_and_percent(self):
        uw = UWClient(key="test")
        parsed = uw._parse_short_row({
            "si_float": "0.017647058", "fee_rate": "0.2782",
            "short_interest": "75000", "short_shares_available": "10000000",
            "days_to_cover": "2.1",
        })
        assert parsed["si_pct"] == pytest.approx(1.7647058)
        assert parsed["borrow_fee"] == pytest.approx(0.2782)
        assert parsed["days_to_cover"] == pytest.approx(2.1)
        # utilization = SI / (SI + available) * 100
        assert parsed["utilization"] == pytest.approx(100.0 * 75000 / (75000 + 10000000))

    def test_parse_short_row_missing_fields_are_none(self):
        uw = UWClient(key="test")
        parsed = uw._parse_short_row({})
        assert parsed == {"si_pct": None, "borrow_fee": None, "utilization": None, "days_to_cover": None}
        parsed2 = uw._parse_short_row(None)
        assert parsed2 == parsed

    def test_insider_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.insider("QXO")
        assert calls[0][0] == "/insider/QXO"

    def test_fundamentals_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.fundamentals("QXO")
        assert calls[0][0] == "/stock/QXO/financials"

    def test_etf_holdings_path(self, client_and_calls):
        uw, calls = client_and_calls
        uw.etf_holdings("XLK")
        assert calls[0][0] == "/etfs/XLK/holdings"

    def test_etf_holdings_dedup_and_sort(self, monkeypatch):
        uw = UWClient(key="test")
        data = {"data": [
            {"ticker": "NVDA", "weight": "12.0", "updated": "2026-06-30"},
            {"ticker": "NVDA", "weight": "14.0", "updated": "2026-08-12"},
            {"ticker": "AAPL", "weight": "12.1", "updated": "2026-08-12"},
        ]}
        monkeypatch.setattr(uw, "get_result", _ok(lambda ep, params=None: data))
        rows = uw.etf_holdings("XLK")
        tickers = [r["ticker"] for r in rows]
        assert tickers == ["NVDA", "AAPL"], "duplicates removed, sorted by weight desc"
        nvda = [r for r in rows if r["ticker"] == "NVDA"][0]
        assert nvda["weight"] == "14.0", "latest updated wins dedup"


class TestAggregateFlowNoFallback:
    def test_empty_flow_recent_returns_empty_flow(self, monkeypatch):
        """aggregate_flow must not fall back to market-wide option-trades."""
        uw = UWClient(key="test")
        monkeypatch.setattr(uw, "flow_recent", lambda ticker: [])
        result = uw.aggregate_flow("QXO")
        assert result["inst_call"] == 0.0
        assert result["inst_put"] == 0.0
        assert result["inst_net"] == 0.0
        assert result["sweep"] == 0.0
        assert result["block"] == 0.0


class TestAggregateFlowUnknownClassification:
    """A missing size is not a small lot; an unknown type is not a put."""

    def test_missing_size_is_unknown_not_retail(self):
        uw = UWClient(key="test")
        r = uw.aggregate_flow("X", [{"premium": 100.0, "option_type": "call"}])
        assert r["retail_call"] == 0.0
        assert r["inst_call"] == 0.0
        assert r["unknown"] == 100.0

    def test_unknown_option_type_is_unknown_not_put(self):
        uw = UWClient(key="test")
        r = uw.aggregate_flow("X", [{"premium": 50.0, "size": 5, "option_type": ""}])
        assert r["retail_put"] == 0.0
        assert r["unknown"] == 50.0

    def test_known_size_and_type_are_classified(self):
        uw = UWClient(key="test")
        r = uw.aggregate_flow("X", [
            {"premium": 100.0, "size": 5, "option_type": "call"},
            {"premium": 200.0, "size": 20, "option_type": "put"},
        ])
        assert r["retail_call"] == 100.0
        assert r["inst_put"] == 200.0
        assert r["unknown"] == 0.0
