"""Small/Micro Cap engine tests (synthetic data, no network)."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.universe import classification_universe
from RevenueChaser.core import universe_fetch as uf
from RevenueChaser.module_asymmetric_growth import engine as agh


def _financials(country="United States", exchange="NMS") -> dict:
    return {
        "ticker": "TEST",
        "sector": "Healthcare",
        "info": {
            "country": country, "exchange": exchange,
            "industry": "Biotechnology", "longName": "Test Bio Corp",
        },
        "market_cap": 500_000_000.0,
        "current_price": 20.0,
        "annual": [
            {"fiscal_year": "2022", "revenue": 100.0, "gross_profit": 50.0,
             "operating_cash_flow": 30.0, "capex": 10.0,
             "total_debt": 5.0, "cash": 5.0, "total_assets": 200.0,
             "rd_expense": 25.0, "diluted_shares": 100.0},
            {"fiscal_year": "2023", "revenue": 123.0, "gross_profit": 61.5,
             "operating_cash_flow": 40.0, "capex": 10.0,
             "total_debt": 5.0, "cash": 5.0, "total_assets": 220.0,
             "rd_expense": 31.0, "diluted_shares": 100.0},
            {"fiscal_year": "2024", "revenue": 160.0, "gross_profit": 80.0,
             "operating_cash_flow": 50.0, "capex": 10.0,
             "total_debt": 5.0, "cash": 5.0, "total_assets": 250.0,
             "rd_expense": 40.0, "diluted_shares": 100.0},
        ],
    }


def _technical_pass() -> dict:
    return {"dist_52w_pct": 0.005, "vol_ratio": 1.8, "above_sma50": True, "price": 20.0}


def _technical_fail() -> dict:
    return {"dist_52w_pct": 0.05, "vol_ratio": 1.1, "above_sma50": False, "price": 18.0}


def test_full_pass():
    c = agh.score_ticker("TEST", _financials(), _technical_pass())
    assert c is not None
    assert c["total_points"] == 9
    assert c["tech_breakthrough"] is True
    assert c["themes"] == []          # C21 (#383): no pinned medium theme → no label; the points above are the proof
                                      # that dropping the keyword guess moved no signal
    assert c["dilution_warning"] is False


def test_non_us_excluded():
    c = agh.score_ticker("TEST", _financials(country="Canada", exchange="TSE"), _technical_pass())
    assert c is None


def test_partial_points():
    c = agh.score_ticker("TEST", _financials(), _technical_fail())
    assert c is not None
    # growth 3/3, survival 3/3, momentum 1/3 (dist 5% <= 10% geÃ§er) -> total 7
    assert c["total_points"] == 7


def test_institutional():
    totals = {
        "TEST": {
            "2026-03-31": {"holders": 875, "new_holders": 117, "exited_holders": 191,
                           "buy_flow": 80e6, "sell_flow": 92e6},
            "2025-12-31": {"holders": 900, "new_holders": 10, "exited_holders": 20,
                           "buy_flow": 10e6, "sell_flow": 5e6},
        }
    }
    inst = agh._institutional("TEST", totals)
    assert inst["inst_holders"] == 875
    assert inst["inst_new"] == 117
    assert inst["inst_exited"] == 191
    assert inst["inst_buy_flow"] == 80e6
    # bilinmeyen ticker -> None
    missing = agh._institutional("NOPE", totals)
    assert missing["inst_holders"] is None


def test_points_bucket():
    assert agh._points_bucket(9) == "pts_9"
    assert agh._points_bucket(8) == "pts_8_7"
    assert agh._points_bucket(7) == "pts_8_7"
    assert agh._points_bucket(6) == "pts_6_5"
    assert agh._points_bucket(5) == "pts_6_5"
    assert agh._points_bucket(4) == "pts_lt5"


def test_dilution_warning():
    fin = _financials()
    fin["annual"][-1]["diluted_shares"] = 200.0  # +100% YoY
    c = agh.score_ticker("TEST", fin, _technical_pass())
    assert c["dilution_warning"] is True
    assert c["dilution_yoy"] == 1.0


def test_runway_fcf_negative():
    fin = _financials()
    # FCF negatif: ocf 10 - capex 25 = -15; cash 5 -> runway = 5/15 = 0.33y (fail)
    fin["annual"][-1]["operating_cash_flow"] = 10.0
    fin["annual"][-1]["capex"] = 25.0
    c = agh.score_ticker("TEST", fin, _technical_pass())
    runway_sub = c["signals"][1]["subs"][2]
    assert runway_sub["passed"] is False
    assert abs(c["runway_years"] - 5 / 15) < 1e-9


def test_fetch_universe_yahoo(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    # one chunk: fake_screen ignores `query`, so the real 7 MC_CHUNKS would just
    # re-yield the same quotes 7x (and sleep 1s each). page_size=2 still exercises
    # the multi-page path; the rate-limit sleep is real-API politeness, not logic. The SEC floor is
    # MIN_FULL_UNIVERSE too, so it is set to the two listed names the fake file holds.
    _fast_universe(monkeypatch, min_full=2)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "NYSE"), ("ZZZ", "OTC"))

    def fake_screen(query, offset=None, size=None, **kw):
        all_q = [
            {"symbol": "AAA", "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD",
             "marketCap": 100_000_000, "regularMarketPrice": 10.0, "shortName": "AAA Corp",
             "fiftyTwoWeekHigh": 11.0, "fiftyDayAverage": 9.0,
             "averageDailyVolume10Day": 1000, "averageDailyVolume3Month": 500},
            {"symbol": "ZZZ", "quoteType": "EQUITY", "exchange": "PNK", "currency": "USD",
             "marketCap": 80_000_000, "regularMarketPrice": 5.0},  # OTC -> excluded
            {"symbol": "BBB", "quoteType": "EQUITY", "exchange": "NYQ", "currency": "USD",
             "marketCap": 200_000_000, "regularMarketPrice": 20.0, "shortName": "BBB Corp"},
        ]
        start = offset or 0
        return {"total": len(all_q), "quotes": all_q[start:start + size]}

    monkeypatch.setattr(yf, "screen", fake_screen)
    rows = agh.fetch_universe(page_size=2, max_pages=10)
    tickers = [r["ticker"] for r in rows]
    assert "AAA" in tickers and "BBB" in tickers
    assert "ZZZ" not in tickers  # OTC excluded


def test_fetch_universe_records_provenance(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 3)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e12)])
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "NYSE"), ("CCC", "Nasdaq"))

    all_q = [
        {"symbol": "AAA", "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD",
         "marketCap": 1e9, "regularMarketPrice": 10.0, "shortName": "AAA"},
        {"symbol": "BBB", "quoteType": "EQUITY", "exchange": "NYQ", "currency": "USD",
         "marketCap": 1e9, "regularMarketPrice": 10.0, "shortName": "BBB"},
        {"symbol": "CCC", "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD",
         "marketCap": 1e9, "regularMarketPrice": 10.0, "shortName": "CCC"},
    ]

    def fake_screen(query, offset=None, size=None, **kw):
        start = offset or 0
        return {"total": len(all_q), "quotes": all_q[start:start + size]}

    monkeypatch.setattr(yf, "screen", fake_screen)
    rows, stats = agh.fetch_universe_stats(page_size=3, max_pages=5,
                                           refreshed_by="Asymmetric Growth")
    assert len(rows) == 3
    assert stats["universe_refreshed_by"] == "Asymmetric Growth"
    assert stats["universe_generated_at"]
    # provenance persisted to the shared meta file
    assert agh._load_universe_meta()["refreshed_by"] == "Asymmetric Growth"


def test_fetch_universe_provenance_from_cache(rawstore_tmp_db):
    agh._save_universe([{"ticker": "AAA", "sector": "Technology"}])
    agh._save_universe_meta("Swing Potential", "2026-08-24T10:00:00+00:00")

    rows, stats = agh.fetch_universe_stats()
    assert stats["cached"] is True
    assert stats["universe_refreshed_by"] == "Swing Potential"
    assert stats["universe_generated_at"] == "2026-08-24T10:00:00+00:00"
    assert len(rows) == 1


# ── Universe membership = the SEC exchange file (#264) ────────────────────────

def _sec_rows(*pairs):
    """``[(ticker, exchange), ...]`` as the SEC map adapter returns them (name ``"<T> Inc (SEC)"``)."""
    return [{"ticker": t, "cik": str(1000 + i), "name": f"{t} Inc (SEC)", "exchange": ex}
            for i, (t, ex) in enumerate(pairs)]


def _fake_sec_map(monkeypatch, *pairs, status="ok", error=None):
    """Fake ``sec.exchange_map`` (no network); the returned list records each call."""
    from RevenueChaser.core.identity.sources import SourceResult, sec
    calls: list[int] = []
    res = SourceResult("sec_exchange", status, _sec_rows(*pairs) if status == "ok" else [], error)
    monkeypatch.setattr(sec, "exchange_map", lambda: calls.append(1) or res)
    return calls


def _screen_quotes(monkeypatch, quotes):
    """Fake ``yf.screen`` paging over ``quotes``; the returned list records each call."""
    import yfinance as yf
    calls: list[int] = []

    def fake_screen(query, offset=None, size=None, **kw):
        calls.append(1)
        start = offset or 0
        return {"total": len(quotes), "quotes": quotes[start:start + size]}

    monkeypatch.setattr(yf, "screen", fake_screen)
    return calls


def _fast_universe(monkeypatch, min_full=1):
    """One market-cap chunk, no sleeps, no Phase B: the rest of the pull is not under test."""
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", min_full)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e12)])
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: {})


def test_sec_list_defines_universe_membership(monkeypatch, rawstore_tmp_db):
    """Nasdaq, NYSE and CBOE rows are in; OTC and no-exchange rows are out, whatever Yahoo says."""
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "NYSE"), ("CCC", "CBOE"),
                  ("OTCX", "OTC"), ("NOEX", None))
    # Yahoo quotes every one of them on a US exchange: only the SEC list decides membership.
    _screen_quotes(monkeypatch, [_one_quote(t, marketCap=1e9) for t in
                                 ("AAA", "BBB", "CCC", "OTCX", "NOEX")])

    rows, stats = agh.fetch_universe_stats(refresh=True, refreshed_by="Test")

    assert {r["ticker"] for r in rows} == {"AAA", "BBB", "CCC"}
    assert {r["ticker"] for r in rawstore.query("SELECT ticker FROM equity_universe")} == {"AAA", "BBB", "CCC"}
    assert stats["sec_listed"] == 3


def test_otc_and_no_exchange_rows_with_no_quote_stay_out(monkeypatch, rawstore_tmp_db):
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("OTCX", "OTC"), ("NOEX", None))
    _screen_quotes(monkeypatch, [_one_quote("AAA", marketCap=1e9)])

    rows, _ = agh.fetch_universe_stats(refresh=True)

    assert [r["ticker"] for r in rows] == ["AAA"]


def test_a_screener_only_row_is_dropped(monkeypatch, rawstore_tmp_db):
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"))
    _screen_quotes(monkeypatch, [_one_quote("AAA", marketCap=1e9), _one_quote("QQQQ", marketCap=9e9)])

    rows, stats = agh.fetch_universe_stats(refresh=True)

    assert [r["ticker"] for r in rows] == ["AAA"]
    assert rawstore.get("equity_universe", "QQQQ") is None
    assert stats["not_on_list"] == 1


def test_an_sec_only_row_is_saved_with_null_cap_price_quote_and_the_sec_name(monkeypatch, rawstore_tmp_db):
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("LISTED", "NYSE"))
    _screen_quotes(monkeypatch, [_one_quote("AAA", marketCap=1e9)])

    rows, stats = agh.fetch_universe_stats(refresh=True)

    assert {r["ticker"] for r in rows} == {"AAA", "LISTED"}
    listed = rawstore.get("equity_universe", "LISTED")
    assert listed["market_cap"] is None and listed["price"] is None and listed["quote"] is None
    assert listed["name"] == "LISTED Inc (SEC)"
    assert stats["sec_only"] == 1
    aaa = rawstore.get("equity_universe", "AAA")
    assert aaa["market_cap"] == 1e9 and aaa["price"] == 925.57     # the screener quote still joins by ticker
    assert aaa["name"] == "Micron Technology, Inc."                # Yahoo's name wins where there is a quote


def test_an_sec_only_row_keeps_the_stored_cap_and_price(monkeypatch, rawstore_tmp_db):
    """Carry-forward is unchanged: a listed name the screener skipped this time keeps its stored numbers."""
    agh._save_universe([{"ticker": "MU", "market_cap": 1.2e12, "price": 900.0, "name": "Old Micron"}])
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("MU", "Nasdaq"))
    _screen_quotes(monkeypatch, [_one_quote("AAA", marketCap=1e9)])

    agh.fetch_universe_stats(refresh=True)

    mu = rawstore.get("equity_universe", "MU")
    assert (mu["market_cap"], mu["price"]) == (1.2e12, 900.0)
    assert mu["quote"] is None and mu["name"] == "MU Inc (SEC)"


def test_yahoo_and_sec_spellings_meet_on_norm_ticker(monkeypatch, rawstore_tmp_db):
    """Yahoo ``BRK-B`` / ``bf.b`` and the SEC ``BRK-B`` / ``BF-B`` are the same name: one row each."""
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("BRK-B", "NYSE"), ("BF-B", "NYSE"))
    _screen_quotes(monkeypatch, [_one_quote("BRK-B", marketCap=9e11), _one_quote("bf.b", marketCap=2e10)])

    rows, stats = agh.fetch_universe_stats(refresh=True)

    assert sorted(r["ticker"] for r in rows) == ["BF-B", "BRK-B"]
    assert rawstore.get("equity_universe", "BF-B")["market_cap"] == 2e10
    assert rawstore.get("equity_universe", "BRK-B")["market_cap"] == 9e11
    assert stats["sec_only"] == 0 and stats["not_on_list"] == 0


def test_a_screener_row_with_no_name_takes_the_sec_name(monkeypatch, rawstore_tmp_db):
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "NYSE"))
    _screen_quotes(monkeypatch, [_one_quote("AAA", marketCap=1e9, shortName="", longName=""),
                                 _one_quote("BBB", marketCap=2e9, shortName="Bee Corp")])

    rows, _ = agh.fetch_universe_stats(refresh=True)

    names = {r["ticker"]: r["name"] for r in rows}
    assert names == {"AAA": "AAA Inc (SEC)", "BBB": "Bee Corp"}      # Yahoo's name wins where it has one
    assert rawstore.get("equity_universe", "AAA")["name"] == "AAA Inc (SEC)"


def test_the_insert_order_is_screener_rows_in_pull_order_then_sec_only_rows(monkeypatch, rawstore_tmp_db):
    """Swing and Fundamentals shortlist the first 500 rows in store order (no ORDER BY): priced names first."""
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("ONLY1", "NYSE"), ("AAA", "Nasdaq"), ("ONLY2", "Nasdaq"), ("BBB", "NYSE"))
    _screen_quotes(monkeypatch, [_one_quote("BBB", marketCap=2e9), _one_quote("AAA", marketCap=1e9)])

    rows, _ = agh.fetch_universe_stats(refresh=True)

    assert [r["ticker"] for r in rows] == ["BBB", "AAA", "ONLY1", "ONLY2"]
    assert [r["ticker"] for r in agh._load_cached_universe()] == ["BBB", "AAA", "ONLY1", "ONLY2"]


def test_a_failed_sec_map_keeps_the_stored_universe_and_writes_nothing(monkeypatch, rawstore_tmp_db):
    agh._save_universe([{"ticker": "AAA", "market_cap": 1e9, "sector": "Technology"},
                        {"ticker": "BBB", "market_cap": 2e9, "sector": "Technology"}])
    agh._save_universe_meta("Swing Potential", "2026-08-24T10:00:00+00:00")
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, status="error", error="HTTPStatusError: 503")
    screen_calls = _screen_quotes(monkeypatch, [_one_quote("CCC", marketCap=1e9)])
    monkeypatch.setattr(uf, "_apply_identity_master", lambda: pytest.fail("a kept universe must not re-apply"))

    rows, stats = agh.fetch_universe_stats(refresh=True, refreshed_by="Asymmetric Growth")

    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
    assert {r["ticker"] for r in rawstore.query("SELECT ticker FROM equity_universe")} == {"AAA", "BBB"}
    assert screen_calls == [], "no point pulling Yahoo when the member list is unavailable"
    assert "503" in stats["sec_error"]
    assert stats["universe_refreshed_by"] == "Swing Potential"     # the stored provenance, unchanged
    assert agh._load_universe_meta()["generated_at"] == "2026-08-24T10:00:00+00:00"


def test_a_failed_sec_map_with_nothing_stored_writes_nothing(monkeypatch, rawstore_tmp_db):
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, status="error", error="boom")
    _screen_quotes(monkeypatch, [_one_quote("AAA", marketCap=1e9)])

    rows, stats = agh.fetch_universe_stats(refresh=True)

    assert rows == []
    assert rawstore.count("equity_universe") == 0
    assert stats["sec_error"] == "boom"


def test_an_sec_map_with_no_listed_rows_is_a_failure(monkeypatch, rawstore_tmp_db):
    """An SEC map that holds only OTC rows would empty the universe: treated as a failed pull."""
    agh._save_universe([{"ticker": "AAA", "market_cap": 1e9, "sector": "Technology"}])
    _fast_universe(monkeypatch)
    _fake_sec_map(monkeypatch, ("OTCX", "OTC"))
    _screen_quotes(monkeypatch, [_one_quote("AAA", marketCap=1e9)])

    rows, stats = agh.fetch_universe_stats(refresh=True)

    assert {r["ticker"] for r in rows} == {"AAA"}
    assert "Nasdaq" in stats["sec_error"]


def test_a_short_sec_member_list_keeps_the_stored_universe(monkeypatch, rawstore_tmp_db):
    """A file that parses but lists few rows would replace a whole universe with those few: a failed pull.

    The floor is on the SEC side now that SEC defines membership; the screener's own count is no guard
    for it. Five OTC rows next to the two listed ones do not count towards the floor.
    """
    stored = [{"ticker": f"S{i}", "market_cap": 1e9, "sector": "Technology"} for i in range(10)]
    agh._save_universe(stored)
    agh._save_universe_meta("Swing Potential", "2026-08-24T10:00:00+00:00")
    _fast_universe(monkeypatch, min_full=3)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "NYSE"),
                  *[(f"OTC{i}", "OTC") for i in range(5)])
    screen_calls = _screen_quotes(monkeypatch, [_one_quote(t, marketCap=1e9) for t in ("AAA", "BBB", "CCC", "DDD")])
    monkeypatch.setattr(uf, "_apply_identity_master", lambda: pytest.fail("a kept universe must not re-apply"))

    rows, stats = agh.fetch_universe_stats(refresh=True, refreshed_by="Asymmetric Growth")

    assert {r["ticker"] for r in rows} == {s["ticker"] for s in stored}
    assert rawstore.count("equity_universe") == 10
    assert screen_calls == [], "no point pulling Yahoo when the member list is unusable"
    assert "2" in stats["sec_error"] and "3" in stats["sec_error"]          # the count and the floor
    assert stats["universe_refreshed_by"] == "Swing Potential"
    assert agh._load_universe_meta()["generated_at"] == "2026-08-24T10:00:00+00:00"


def test_an_sec_member_list_at_the_floor_is_saved(monkeypatch, rawstore_tmp_db):
    """The floor is inclusive: exactly ``MIN_FULL_UNIVERSE`` listed rows is a healthy file."""
    agh._save_universe([{"ticker": "OLD", "market_cap": 1e9, "sector": "Technology"}])
    _fast_universe(monkeypatch, min_full=3)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "NYSE"), ("CCC", "CBOE"))
    _screen_quotes(monkeypatch, [_one_quote(t, marketCap=1e9) for t in ("AAA", "BBB", "CCC")])

    rows, stats = agh.fetch_universe_stats(refresh=True)

    assert {r["ticker"] for r in rows} == {"AAA", "BBB", "CCC"}
    assert "sec_error" not in stats


def test_the_sec_map_is_fetched_once_per_pull_and_never_on_the_cached_path(monkeypatch, rawstore_tmp_db):
    agh._save_universe([{"ticker": "AAA", "market_cap": 1e9, "sector": "Technology"}])
    _fast_universe(monkeypatch)
    calls = _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"))
    _screen_quotes(monkeypatch, [_one_quote("AAA", marketCap=1e9)])

    agh.fetch_universe_stats()                       # cache-first: no network at all
    assert calls == []
    agh.fetch_universe_stats(refresh=True)
    assert calls == [1]


def test_technical_from_quote():
    q = {"regularMarketPrice": 10.0, "fiftyTwoWeekHigh": 10.1, "fiftyDayAverage": 9.0,
         "averageDailyVolume10Day": 1000, "averageDailyVolume3Month": 500}
    t = agh.technical_from_quote(q)
    assert abs(t["dist_52w_pct"] - 0.1 / 10.1) < 1e-9
    assert t["vol_ratio"] == 2.0
    assert t["above_sma50"] is True


def test_render_dashboard_universe_provenance():
    from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
    result = {
        "asof": "2026-08-23T00:00:00",
        "universe_total": 1,
        "universe_refreshed_by": "Asymmetric Growth",
        "universe_generated_at": "2026-08-24T10:00:00+00:00",
        "summary": {"scanned": 0, "pts_9": 0, "pts_8_7": 0, "pts_6_5": 0, "pts_lt5": 0,
                    "disruptive_themed": 0, "tech_breakthrough": 0},
        "thresholds": {
            "rev_growth": 0.2, "gross_margin": 0.4, "net_debt_assets": 0.1, "runway": 2.0,
            "dist_52w": 0.1, "vol_ratio": 1.4, "rd_intensity": 0.15, "dilution": 0.2,
            "accelerating": True, "fcf": True, "above_sma50": True,
        },
        "cards": [],
    }
    html = render_asymmetric_growth_hunter_html(result)
    assert "Universe refreshed by" in html
    assert "Asymmetric Growth" in html


# ── Universe Phase B (market-cap backfill) — docs/data_health.md ──────────────

class _FakeFastInfo:
    """Minimal ``fast_info`` stand-in; market_cap must never be touched."""

    def __init__(self, shares, last_price):
        self.shares = shares
        self.last_price = last_price

    @property
    def market_cap(self):
        raise AssertionError("fast_info.market_cap must not be read (falls back to .info)")


def _fake_ticker_factory(shares=1.35e9, last_price=925.57, calls=None):
    class _FakeTicker:
        def __init__(self, ticker):
            if calls is not None:
                calls.append(ticker)

        @property
        def fast_info(self):
            return _FakeFastInfo(shares, last_price)

        @property
        def info(self):
            raise AssertionError("Ticker.info must never be called")

    return _FakeTicker


def _one_quote(symbol="MU", **extra):
    q = {"symbol": symbol, "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD",
         "regularMarketPrice": 925.57, "shortName": "Micron Technology, Inc."}
    q.update(extra)
    return q


def _screen_one(quote):
    def fake_screen(query, offset=None, size=None, **kw):
        start = offset or 0
        return {"total": 1, "quotes": [quote][start:start + size]}
    return fake_screen


def test_save_universe_carries_forward_stored_cap(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    agh._save_universe([{"ticker": "MU", "market_cap": 1.2e12, "price": 900.0,
                         "name": "Old Micron", "sector": "Technology"}])
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 1)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e12)])
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    _fake_sec_map(monkeypatch, ("MU", "Nasdaq"))
    monkeypatch.setattr(yf, "screen", _screen_one(_one_quote()))  # no marketCap key

    agh.fetch_universe_stats(refresh=True, refreshed_by="Test")

    mu = rawstore.get("equity_universe", "MU")
    assert mu["market_cap"] == 1.2e12      # carried forward, not wiped
    assert mu["price"] == 925.57           # new non-null price wins
    assert mu["name"] == "Micron Technology, Inc."   # other fields are the new values


def test_save_universe_carries_the_stored_sector_and_industry_forward(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    agh._save_universe([{"ticker": "AAPL", "sector": "Technology", "industry": "Consumer Electronics",
                         "market_cap": 3.0e12}])
    assert rawstore.get("equity_universe", "AAPL")["sector"] == "Technology"
    # the identity master (the single writer) puts its UW-named values on top, and an earnings date next to them
    rawstore.update_columns("equity_universe", "ticker", [
        {"ticker": "AAPL", "sector": "Healthcare", "industry": "Biotechnology", "next_earnings_date": "2026-10-30"}],
        ["sector", "industry", "next_earnings_date"])

    # a screener refetch: same ticker, a vendor sector and industry of its own, a new cap
    agh._save_universe([{"ticker": "AAPL", "sector": "Technology", "industry": "Software - Application",
                         "market_cap": 3.1e12}])

    row = rawstore.get("equity_universe", "AAPL")
    assert (row["sector"], row["industry"]) == ("Healthcare", "Biotechnology"), "the rebuild replaced the stored values"
    assert row["next_earnings_date"] == "2026-10-30", "the rebuild wiped the earnings date"
    assert (row["industry_group"], row["sub_industry"]) == (None, None)
    assert row["market_cap"] == 3.1e12                   # a genuinely new value still wins


def test_save_universe_does_not_convert_a_stored_value_through_the_gics_adapter(rawstore_tmp_db):
    """A stored sector is carried unchanged, whatever vocabulary it is in (the master replaces it on the next apply)."""
    from RevenueChaser.core import rawstore
    agh._save_universe([{"ticker": "OLD", "market_cap": 1e9}])
    rawstore.update_columns("equity_universe", "ticker", [
        {"ticker": "OLD", "sector": "Information Technology", "industry": "Semiconductors",
         "industry_group": "Semiconductors & Semiconductor Equipment", "sub_industry": "Semiconductors"}],
        ["sector", "industry", "industry_group", "sub_industry"])
    agh._save_universe([{"ticker": "OLD", "market_cap": 2e9}])
    row = rawstore.get("equity_universe", "OLD")
    assert (row["sector"], row["industry"]) == ("Information Technology", "Semiconductors")
    assert (row["industry_group"], row["sub_industry"]) == (None, None), "the GICS-only columns are not carried"


def test_save_universe_new_ticker_without_sector_is_unclassified(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    agh._save_universe([{"ticker": "NEWCO", "market_cap": 1.0e9}])
    row = rawstore.get("equity_universe", "NEWCO")
    assert (row["sector"], row["industry"], row["next_earnings_date"]) == ("Unclassified", "", None)


def test_save_universe_new_ticker_takes_a_vendor_sector_only_when_it_is_one_of_the_eleven_names(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    agh._save_universe([
        {"ticker": "MORN", "sector": "Consumer Cyclical", "industry": "Auto Manufacturers"},
        {"ticker": "GICS", "sector": "Consumer Discretionary", "industry": "Automobiles"},
        {"ticker": "QUOTE", "quote": {"industry": "Biotechnology"}}])
    got = {t: (r["sector"], r["industry"]) for t in ("MORN", "GICS", "QUOTE")
           for r in [rawstore.get("equity_universe", t)]}
    assert got["MORN"] == ("Consumer Cyclical", "Auto Manufacturers")
    assert got["GICS"] == ("Unclassified", "Automobiles"), "a GICS name is not a sector here"
    assert got["QUOTE"] == ("Unclassified", "Biotechnology"), "the quote's industry text fills an empty one"


def test_phase_b_backfills_null_cap_and_mu_survives_classification(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 1)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e12)])
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    _fake_sec_map(monkeypatch, ("MU", "Nasdaq"))
    monkeypatch.setattr(yf, "screen", _screen_one(_one_quote(sector="Technology")))
    monkeypatch.setattr(yf, "Ticker", _fake_ticker_factory(1.35e9, 925.57))

    rows, stats = agh.fetch_universe_stats(refresh=True, refreshed_by="Test")

    mu = rawstore.get("equity_universe", "MU")
    assert mu["market_cap"] == pytest.approx(1.35e9 * 925.57)
    assert stats["cap_backfill"] == {"requested": 1, "filled": 1, "skipped": 0,
                                     "failed": 0, "deferred": 0, "rate_limited": False}
    assert "MU" in classification_universe()
    assert "MU" in {r["ticker"] for r in rows}


def test_backfill_skips_when_shares_missing_and_never_reads_info(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    agh._save_universe([{"ticker": "WARR", "sector": "Technology"}])
    calls = []
    monkeypatch.setattr(yf, "Ticker", _fake_ticker_factory(None, 5.0, calls))
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    result = agh.backfill_universe_caps(refreshed_by="Test")

    assert result == {"requested": 1, "filled": 0, "skipped": 1,
                      "failed": 0, "deferred": 0, "rate_limited": False}
    assert rawstore.get("equity_universe", "WARR")["market_cap"] is None
    assert calls == ["WARR"]


def test_backfill_never_deletes_or_shrinks_rows(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    agh._save_universe([
        {"ticker": "CAPPED", "market_cap": 5e9, "price": 50.0, "sector": "Technology"},
        {"ticker": "NULLC", "sector": "Technology"},
    ])
    monkeypatch.setattr(yf, "Ticker", _fake_ticker_factory(1e9, 10.0))
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    agh.backfill_universe_caps()

    assert rawstore.count("equity_universe") == 2
    assert rawstore.get("equity_universe", "CAPPED")["market_cap"] == 5e9
    assert rawstore.get("equity_universe", "NULLC")["market_cap"] == pytest.approx(1e10)


def test_backfill_requests_only_null_cap_tickers(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    agh._save_universe([
        {"ticker": "CAPPED", "market_cap": 5e9, "sector": "Technology"},
        {"ticker": "NULLA", "sector": "Technology"},
        {"ticker": "NULLB", "sector": "Technology"},
    ])
    calls = []
    monkeypatch.setattr(yf, "Ticker", _fake_ticker_factory(1e9, 10.0, calls))
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    result = agh.backfill_universe_caps()

    assert sorted(calls) == ["NULLA", "NULLB"]
    assert result["requested"] == 2 and result["filled"] == 2


def test_backfill_tickers_filter_intersects_null_set(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    agh._save_universe([
        {"ticker": "NULLA", "sector": "Technology"},
        {"ticker": "NULLB", "sector": "Technology"},
    ])
    calls = []
    monkeypatch.setattr(yf, "Ticker", _fake_ticker_factory(1e9, 10.0, calls))
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    result = agh.backfill_universe_caps(tickers=["NULLA"])

    assert calls == ["NULLA"]
    assert result["requested"] == 1 and result["filled"] == 1


def test_full_fetch_writes_both_provenance_keys(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 1)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e12)])
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    _fake_sec_map(monkeypatch, ("MU", "Nasdaq"))
    monkeypatch.setattr(yf, "screen", _screen_one(_one_quote(sector="Technology")))
    monkeypatch.setattr(yf, "Ticker", _fake_ticker_factory(1.35e9, 100.0))

    agh.fetch_universe_stats(refresh=True, refreshed_by="Asymmetric Growth")

    assert agh._load_universe_meta()["refreshed_by"] == "Asymmetric Growth"
    stats_meta = agh._load_stats_meta()
    assert stats_meta["generated_at"]
    assert stats_meta["refreshed_by"] == "Asymmetric Growth"
    assert rawstore.get("equity_universe_provenance", "__meta__") is not None
    assert rawstore.get("equity_universe_provenance", "__stats__") is not None


def test_cached_path_never_calls_ticker(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    agh._save_universe([{"ticker": "AAA", "market_cap": 1e9, "sector": "Technology"}])
    agh._save_universe_meta("Swing Potential", "2026-08-24T10:00:00+00:00")

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("cached path must not call yf.Ticker")

    monkeypatch.setattr(yf, "Ticker", _Boom)

    rows, stats = agh.fetch_universe_stats()

    assert stats["cached"] is True
    assert {r["ticker"] for r in rows} == {"AAA"}


def test_partial_screener_upserts_keeps_the_stored_names_and_skips_backfill(monkeypatch, rawstore_tmp_db):
    """D6 (#146): a chunk that did not finish is a partial pull. It upserts what it got and deletes nothing."""
    import yfinance as yf
    agh._save_universe([
        {"ticker": "AAA", "market_cap": 1e9, "sector": "Technology", "quote": {"symbol": "AAA"}},
        {"ticker": "BBB", "market_cap": 2e9, "sector": "Technology", "quote": {"symbol": "BBB"}},
    ])
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 3)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e12)])
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    # a healthy SEC list that names a third, unstored company
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "Nasdaq"), ("NEWCO", "NYSE"))

    def short_screen(query, offset=None, size=None, **kw):
        # the screener reports 5 names, then answers an empty page: the chunk never finishes
        return {"total": 5, "quotes": [_one_quote("AAA", marketCap=1e9)] if not offset else []}

    monkeypatch.setattr(yf, "screen", short_screen)

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("partial pull must not backfill")

    monkeypatch.setattr(yf, "Ticker", _Boom)

    rows, stats = agh.fetch_universe_stats(refresh=True, page_size=250, retries=0)

    assert stats["partial"] is True and stats["fetched"] == 1 and stats["not_seen"] == 1
    assert {r["ticker"] for r in rows} == {"AAA", "BBB", "NEWCO"}      # BBB kept, the listed NEWCO added
    assert rawstore.get("equity_universe", "BBB")["quote"] == {"symbol": "BBB"}
    assert rawstore.get("equity_universe_provenance", "__fetch__")["refreshed_by"] == "partial"


def test_fetch_universe_stats_applies_the_identity_master(monkeypatch, rawstore_tmp_db):
    from RevenueChaser.core.identity import apply as identity_apply
    calls = []
    monkeypatch.setattr(identity_apply, "apply_to_universe", lambda: calls.append(1) or {})
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 2)
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "NYSE"))

    def fake_screen(query, offset=None, size=None, **kw):
        return {"total": 2, "quotes": [
            {"symbol": "AAA", "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD",
             "marketCap": 1e9, "regularMarketPrice": 10.0, "shortName": "AAA"},
            {"symbol": "BBB", "quoteType": "EQUITY", "exchange": "NYQ", "currency": "USD",
             "marketCap": 1e9, "regularMarketPrice": 10.0, "shortName": "BBB"}]}

    import yfinance as yf
    monkeypatch.setattr(yf, "screen", fake_screen)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e12)])          # one chunk, like :119
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: {})  # Phase B, not this test

    agh.fetch_universe_stats(page_size=2, max_pages=1, refreshed_by="test")
    assert calls == [1], "a full universe save must re-apply the identity master (R3 #86)"


@pytest.mark.parametrize("path", ["full", "first_load"])
def test_a_universe_rebuild_leaves_the_master_classification_in_place(path, monkeypatch, rawstore_tmp_db):
    """R3 (#86): the rebuild rewrites every row from vendor strings, then re-applies ``security_master``.

    Both call sites (a full pull, and a partial pull with nothing cached yet) are covered. The SIC backfill
    is gone: a stand-in that raises proves nothing calls it.
    """
    from RevenueChaser.core import sic_registry
    from RevenueChaser.core.identity import apply as identity_apply

    calls = []
    real = identity_apply.apply_to_universe
    monkeypatch.setattr(identity_apply, "apply_to_universe", lambda: calls.append(1) or real())

    def _no_backfill():
        raise AssertionError("the SIC backfill must no longer run")

    monkeypatch.setattr(sic_registry, "backfill_universe_gics", _no_backfill, raising=False)
    monkeypatch.setattr(uf, "MIN_FULL_UNIVERSE", 2 if path == "full" else 100)
    monkeypatch.setattr(uf, "MC_CHUNKS", [(0, 1e12)])
    # first_load: a healthy SEC file (100 listed rows, the floor) next to a screener that returns only 2 (partial)
    fillers = [] if path == "full" else [(f"F{i}", "Nasdaq") for i in range(98)]
    _fake_sec_map(monkeypatch, ("AAA", "Nasdaq"), ("BBB", "NYSE"), *fillers)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(uf, "backfill_universe_caps", lambda **kw: {})
    rawstore.write_rows("security_master", [
        {"ticker": "AAA", "sector": "Healthcare", "industry": "Biotechnology"}])

    def fake_screen(query, offset=None, size=None, **kw):
        def q(sym):
            return {"symbol": sym, "quoteType": "EQUITY", "exchange": "NMS", "currency": "USD", "sector": "Technology",
                    "marketCap": 1e9, "regularMarketPrice": 10.0, "shortName": sym}
        return {"total": 2, "quotes": [q("AAA"), q("BBB")]}

    import yfinance as yf
    monkeypatch.setattr(yf, "screen", fake_screen)

    agh.fetch_universe_stats(page_size=2, max_pages=1, refresh=True, refreshed_by="test")

    assert calls == [1]
    aaa, bbb = rawstore.get("equity_universe", "AAA"), rawstore.get("equity_universe", "BBB")
    assert (aaa["sector"], aaa["industry"], aaa["industry_group"], aaa["sub_industry"]) == (
        "Healthcare", "Biotechnology", None, None)
    # a name the master does not know keeps what the rebuild carried (here the vendor's Morningstar name): never blanked
    assert (bbb["sector"], bbb["industry"], bbb["industry_group"], bbb["sub_industry"]) == ("Technology", "", None, None)



def test_render_uses_the_shared_filter_kit():
    """#383 C1c: the universal filters are the rc_filters bar, not the old Sector cascade / MC / EWEEK / INDEX controls."""
    from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
    result = {
        "asof": "", "summary": {}, "cards": [],
        "thresholds": {
            "rev_growth": 0.2, "gross_margin": 0.4, "net_debt_assets": 0.1, "runway": 2.0,
            "dist_52w": 0.1, "vol_ratio": 1.4, "rd_intensity": 0.15, "dilution": 0.2,
            "accelerating": True, "fcf": True, "above_sma50": True,
        },
    }
    html = render_asymmetric_growth_hunter_html(result)
    for frag in ('id="rc-filters"', "RCFilters.mount", 'data-pt="all"'):
        assert frag in html, f"missing shared filter fragment: {frag}"
    for gone in ("sectorOk(", "mcRange(", 'id="mc-tabs"', 'id="eweek-tabs"', 'id="index-tabs"',
                 'id="theme"', 'id="sector"', 'id="industry"',
                 'id="industry-group"', 'id="sub-industry"', "IGROUP", "ISUB",
                 "industry_group", "sub_industry"):
        assert gone not in html, f"the old filter markup is gone, found: {gone}"


# ── Phase B rate-limit survival — docs/data_health.md ─────────────────────────

def _failing_ticker_factory(behavior, shares=1e9, last_price=10.0):
    """Fake ``yf.Ticker`` whose construction can raise per ``behavior(ticker, idx)``.

    ``behavior`` returns an exception instance to raise on that ticker's ``idx``-th
    construction, or ``None`` to succeed. ``fast_info``/``info`` guards match the
    existing fakes so a stray ``.info`` / ``fast_info.market_cap`` read fails loudly.
    """
    calls = []

    class _FakeTicker:
        def __init__(self, ticker):
            idx = sum(1 for c in calls if c == ticker)
            calls.append(ticker)
            exc = behavior(ticker, idx)
            if exc is not None:
                raise exc

        @property
        def fast_info(self):
            return _FakeFastInfo(shares, last_price)

        @property
        def info(self):
            raise AssertionError("Ticker.info must never be called")

    return _FakeTicker, calls


def test_backfill_retries_rate_limit_with_backoff_then_fills(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    agh._save_universe([{"ticker": "RATEY", "sector": "Technology"}])

    def behavior(ticker, idx):
        return YFRateLimitError() if idx < 2 else None

    fake_ticker, _calls = _failing_ticker_factory(behavior)
    sleeps = []
    monkeypatch.setattr(yf, "Ticker", fake_ticker)
    monkeypatch.setattr("time.sleep", lambda s, *a, **k: sleeps.append(s))

    result = agh.backfill_universe_caps(refreshed_by="Test")

    assert result["filled"] == 1
    assert result["rate_limited"] is False
    assert result["deferred"] == 0
    assert rawstore.get("equity_universe", "RATEY")["market_cap"] == pytest.approx(1e10)
    assert 5.0 in sleeps and 20.0 in sleeps  # both backoff steps were requested


def test_backfill_stops_early_after_rate_limit_streak(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError

    tickers = [f"RL{i:02d}" for i in range(25)]
    agh._save_universe([{"ticker": t, "sector": "Technology"} for t in tickers])

    def behavior(ticker, idx):
        return YFRateLimitError()

    fake_ticker, calls = _failing_ticker_factory(behavior)
    monkeypatch.setattr(yf, "Ticker", fake_ticker)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    result = agh.backfill_universe_caps(refreshed_by="Test")

    assert result["filled"] == 0
    assert result["failed"] == 0
    assert result["rate_limited"] is True
    assert result["deferred"] >= 1
    # the run stopped early: the tail of the work list was never requested
    assert len(calls) < len(tickers)


def test_backfill_non_rate_limit_failure_counts_failed_without_abort(
        monkeypatch, rawstore_tmp_db):
    import yfinance as yf

    agh._save_universe([
        {"ticker": "GOOD", "sector": "Technology"},
        {"ticker": "BOOM", "sector": "Technology"},
        {"ticker": "ALSO", "sector": "Technology"},
    ])

    def behavior(ticker, idx):
        return ValueError("bad quote") if ticker == "BOOM" else None

    fake_ticker, _calls = _failing_ticker_factory(behavior)
    monkeypatch.setattr(yf, "Ticker", fake_ticker)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    result = agh.backfill_universe_caps(refreshed_by="Test")

    assert result["failed"] == 1
    assert result["filled"] == 2
    assert result["rate_limited"] is False
    assert rawstore.get("equity_universe", "BOOM")["market_cap"] is None
    assert rawstore.get("equity_universe", "GOOD")["market_cap"] == pytest.approx(1e10)
    assert rawstore.get("equity_universe", "ALSO")["market_cap"] == pytest.approx(1e10)


def test_backfill_limit_caps_the_work_list(monkeypatch, rawstore_tmp_db):
    import yfinance as yf
    agh._save_universe([
        {"ticker": "NULLA", "sector": "Technology"},
        {"ticker": "NULLB", "sector": "Technology"},
        {"ticker": "NULLC", "sector": "Technology"},
    ])
    calls = []
    monkeypatch.setattr(yf, "Ticker", _fake_ticker_factory(1e9, 10.0, calls))
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    result = agh.backfill_universe_caps(limit=2)

    assert result["requested"] == 2
    assert result["filled"] == 2
    assert len(calls) == 2
    assert rawstore.get("equity_universe", "NULLC")["market_cap"] is None
