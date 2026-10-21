"""UW sector adapter: market-cap band fetching, splitting of full bands, missing key."""
from __future__ import annotations

from RevenueChaser.core.identity.sources import uw_sector
from RevenueChaser.core.identity.tests.helpers import fixture_json
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.core.providers.result import ProviderResult


class FakeUW:
    """Stands in for UWClient: serves ``universe`` rows filtered by the requested cap band."""

    active = True

    def __init__(self, universe, fail_on_call=None, limit_rows=500):
        self.universe = universe
        self.calls = []
        self.fail_on_call = fail_on_call
        self.limit_rows = limit_rows

    def get_result(self, ep, params=None):
        self.calls.append((ep, dict(params or {})))
        if self.fail_on_call and len(self.calls) == self.fail_on_call:
            return ProviderResult("failed", {}, {"status": 429, "error": "HTTP 429"}, ep)
        lo, hi = float(params["min_marketcap"]), float(params["max_marketcap"])
        hits = [r for r in self.universe if lo <= float(r["marketcap"]) <= hi]
        return ProviderResult("ok", {"data": hits[: int(params["limit"])]}, None, ep)


def test_happy_path_uses_recorded_rows():
    rows = fixture_json("uw_rows.json")
    r = uw_sector.sectors(client=FakeUW(rows), sleep=lambda s: None)
    assert r.source == "uw_sector" and r.status == "ok"
    by = {x["ticker"]: x for x in r.rows}
    assert {"AAPL", "GOOGL"} <= set(by)
    assert by["AAPL"]["sector"] and by["AAPL"]["security_type"]
    assert "industry" in by["AAPL"] and isinstance(by["AAPL"]["market_cap"], float)


def test_rows_carry_the_next_earnings_date_as_text_or_none():
    universe = [{"ticker": "AAA", "sector": "Technology", "industry_type": "Software - Application",
                 "issue_type": "Common Stock", "marketcap": "2000000000", "next_earnings_date": "2026-11-19"},
                {"ticker": "BBB", "sector": "Energy", "industry_type": "Oil & Gas E&P", "issue_type": "Common Stock",
                 "marketcap": "2100000000", "next_earnings_date": None},
                {"ticker": "CCC", "sector": "Energy", "industry_type": "Oil & Gas E&P", "issue_type": "ETF",
                 "marketcap": "2200000000", "next_earnings_date": " "},
                {"ticker": "DDD", "sector": "Utilities", "industry_type": "Utilities - Regulated Electric",
                 "issue_type": "Common Stock", "marketcap": "2300000000"}]            # the field is absent
    by = {r["ticker"]: r for r in uw_sector.sectors(client=FakeUW(universe), sleep=lambda s: None).rows}
    assert by["AAA"]["next_earnings_date"] == "2026-11-19"
    assert all(by[t]["next_earnings_date"] is None for t in ("BBB", "CCC", "DDD"))
    assert by["AAA"]["industry"] == "Software - Application", "the existing fields are unchanged"


def test_every_request_is_a_banded_screener_call_without_a_page_param():
    rows = fixture_json("uw_rows.json")
    uw = FakeUW(rows)
    uw_sector.sectors(client=uw, sleep=lambda s: None)
    assert uw.calls
    for ep, params in uw.calls:
        assert ep == "/screener/stocks"
        assert "page" not in params and params["limit"] == 500
        assert float(params["min_marketcap"]) < float(params["max_marketcap"])


def test_a_band_that_returns_the_limit_is_split_until_nothing_is_lost():
    universe = [{"ticker": f"T{i:04d}", "sector": "Financials", "industry_type": "Banks",
                 "issue_type": "Common Stock", "marketcap": str(1.0e9 + i * 1.0e6)} for i in range(700)]
    uw = FakeUW(universe)
    r = uw_sector.sectors(client=uw, sleep=lambda s: None)
    assert r.status == "ok" and len(r.rows) == 700
    assert len(uw.calls) > len(uw_sector.EDGES) - 1            # extra calls = band splits
    assert r.error is None


def test_a_band_that_cannot_be_split_further_is_reported_not_hidden():
    universe = [{"ticker": f"S{i:04d}", "sector": "Energy", "industry_type": "x", "issue_type": "ETF",
                 "marketcap": "1500000000"} for i in range(520)]     # identical cap: unsplittable
    r = uw_sector.sectors(client=FakeUW(universe), sleep=lambda s: None)
    assert r.status == "ok" and len(r.rows) == 500
    assert "truncated" in r.error


def test_ticker_seen_in_two_bands_is_kept_once():
    uni = [{"ticker": "EDGE", "sector": "Utilities", "industry_type": "x", "issue_type": "Common Stock",
            "marketcap": "300000000"}]                            # exactly on a band edge
    r = uw_sector.sectors(client=FakeUW(uni), sleep=lambda s: None)
    assert [x["ticker"] for x in r.rows] == ["EDGE"]


def test_provider_failure_stops_with_an_error_and_keeps_what_it_has():
    rows = fixture_json("uw_rows.json")
    r = uw_sector.sectors(client=FakeUW(rows, fail_on_call=3), sleep=lambda s: None)
    assert r.status == "error" and "429" in r.error


def test_bands_are_paced():
    sleeps = []
    uw_sector.sectors(client=FakeUW([]), sleep=sleeps.append)
    assert sleeps and all(s >= 0.5 for s in sleeps)


def test_missing_key_is_skipped_and_makes_no_call(monkeypatch, http):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    r = uw_sector.sectors(sleep=lambda s: None)
    assert r.status == "skipped" and r.rows == []
    assert "UNUSUAL_WHALES_API_KEY" in r.error
    assert not http.calls


def test_base_url_override_reaches_the_real_client(monkeypatch):
    """The default client reads RC_UW_BASE (via providers.unusual_whales.BASE)."""
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key-value")
    monkeypatch.setattr(unusual_whales, "BASE", "http://127.0.0.1:9/api")
    seen = []

    class _Resp:
        status_code = 200
        text = "{}"
        headers = {}

        def json(self):
            return {"data": []}

    def fake_get(self, url, params=None):
        seen.append(url)
        return _Resp()

    monkeypatch.setattr(unusual_whales.httpx.Client, "get", fake_get)
    r = uw_sector.sectors(sleep=lambda s: None)
    assert r.status == "ok"
    assert seen and all(u == "http://127.0.0.1:9/api/screener/stocks" for u in seen)
