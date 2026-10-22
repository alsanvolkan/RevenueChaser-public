"""#150 (U13): AG asks Yahoo for ``.info`` only when EDGAR has the annual figures, and reads the stores
in bulk. The cards are the same as before (no board change). Synthetic stores, no network."""
from __future__ import annotations

import copy
import time

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import seed_momentum

_QUOTE = {"regularMarketPrice": 100.0, "fiftyTwoWeekHigh": 105.0, "fiftyDayAverage": 90.0,
          "averageDailyVolume10Day": 160.0, "averageDailyVolume3Month": 100.0, "earningsTimestampStart": 1.8e9}
_ANNUAL = [{"fiscal_year": y, "revenue": r, "gross_profit": r * 0.6, "operating_cash_flow": r * 0.3,
            "capex": r * 0.05, "total_debt": 10.0, "cash": 200.0, "total_assets": 1000.0,
            "rd_expense": r * 0.2, "diluted_shares": 100.0 + i}
           for i, (y, r) in enumerate([(2021, 100.0), (2022, 130.0), (2023, 190.0)])]
_YF_ANNUAL = [dict(r, capital_expenditure=r["capex"]) for r in _ANNUAL]


class _Yahoo:
    """Counts the two provider calls; hands back what the real ones would."""

    def __init__(self, monkeypatch):
        self.info_calls, self.full_calls = [], []
        monkeypatch.setattr(engine.sec_edgar, "_cik_map", lambda: {})  # an EDGAR miss never goes online
        monkeypatch.setattr(engine.yfinance_client, "get_info", self._info)
        monkeypatch.setattr(engine.yfinance_client, "get_financials", self._full)

    def _info(self, ticker):
        self.info_calls.append(ticker)
        return {"ticker": ticker, "info": {"country": "United States", "exchange": "NMS",
                                           "industry": "Software", "longName": f"{ticker} Inc"},
                "annual": [], "market_cap": 5e9, "current_price": 100.0, "info_only": True,
                "source": "yfinance"}

    def _full(self, ticker):
        self.full_calls.append(ticker)
        return {"ticker": ticker, "info": {"country": "United States", "exchange": "NMS",
                                           "industry": "Software", "longName": f"{ticker} Inc"},
                "annual": copy.deepcopy(_YF_ANNUAL), "market_cap": 5e9, "current_price": 100.0,
                "source": "yfinance"}


def _edgar(ticker, annual=_ANNUAL, is_us=True):
    return {"ticker": ticker, "annual": copy.deepcopy(annual), "quarterly": [],
            "meta": {"source": "sec_edgar", "entity_name": f"{ticker} Corp", "is_us": is_us, "available": True}}


# ── the provider call ───────────────────────────────────────────────────────

def test_a_name_with_edgar_annual_costs_one_info_call(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    yahoo = _Yahoo(monkeypatch)
    fin = engine._cached_financials("AAA")
    assert (yahoo.info_calls, yahoo.full_calls) == (["AAA"], [])
    assert fin["source"] == "sec_edgar" and fin["annual"][-1]["fiscal_year"] == 2023


def test_a_name_without_edgar_annual_keeps_the_full_call(rawstore_tmp_db, monkeypatch):
    yahoo = _Yahoo(monkeypatch)
    fin = engine._cached_financials("BBB")
    assert (yahoo.info_calls, yahoo.full_calls) == ([], ["BBB"])
    assert fin["annual"]


def test_the_info_only_payload_is_stored_and_reused(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    yahoo = _Yahoo(monkeypatch)
    engine._cached_financials("AAA")
    engine._cached_financials("AAA")
    assert yahoo.info_calls == ["AAA"]  # the second read came from ticker_payload_cache
    assert rawstore.get("ticker_payload_cache", "AAA")["payload"]["info_only"] is True


def test_a_fresh_full_payload_is_used_and_not_overwritten(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    cache_manager.save("AAA", {"info": {"country": "United States"}, "annual": copy.deepcopy(_YF_ANNUAL)})
    yahoo = _Yahoo(monkeypatch)
    engine._cached_financials("AAA")
    assert (yahoo.info_calls, yahoo.full_calls) == ([], [])
    assert "info_only" not in rawstore.get("ticker_payload_cache", "AAA")["payload"]
    assert rawstore.get("ticker_payload_cache", "AAA")["payload"]["annual"]


def test_an_expired_full_payload_keeps_its_years_when_an_info_call_replaces_it(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    old = time.time() - 10 * 86400
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "AAA", "payload": {"info": {}, "annual": copy.deepcopy(_YF_ANNUAL), "_fetched_at": old},
        "fetched_at": old, "fetched_at_iso": ""}])
    yahoo = _Yahoo(monkeypatch)
    fin = engine._cached_financials("AAA")
    assert yahoo.info_calls == ["AAA"]
    assert fin["info"]["country"] == "United States"                         # AG got fresh info
    stored = rawstore.get("ticker_payload_cache", "AAA")["payload"]
    assert stored["annual"] and "info_only" not in stored                    # Financials keeps its years


def test_an_info_only_row_does_not_serve_a_name_that_lost_its_edgar_annual(rawstore_tmp_db, monkeypatch):
    cache_manager.save("AAA", {"info": {"country": "United States"}, "annual": [], "info_only": True})
    yahoo = _Yahoo(monkeypatch)
    fin = engine._cached_financials("AAA")
    assert yahoo.full_calls == ["AAA"] and fin["annual"]


def test_the_financials_board_never_sees_the_info_only_row(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    _Yahoo(monkeypatch)
    engine._cached_financials("AAA")
    full = {"info": {}, "annual": [{"fiscal_year": "2023", "revenue": 1.0}]}
    assert cache_manager.get_or_fetch("AAA", lambda t: full) == full         # fetched, not served empty


# ── the bulk reads ──────────────────────────────────────────────────────────

def _seed(n: int, themed: bool = True):
    tickers = [f"T{i:03d}" for i in range(n)]
    seed_momentum(tickers)                     # the momentum signal comes from D01 (#380 S4)

    rawstore.write_rows("edgar_financials", [_edgar(t, is_us=(i % 7 != 3)) for i, t in enumerate(tickers)])
    for i, t in enumerate(tickers):
        if i % 4 == 0:  # a quarter of them has a fresh full yfinance payload
            cache_manager.save(t, {"info": {"country": "United States", "exchange": "NMS", "longName": t,
                                            "industry": "Software"}, "annual": copy.deepcopy(_YF_ANNUAL),
                                   "market_cap": 5e9})
    if themed:
        rawstore.write_rows("theme_runs", [{"id": "run1", "created_at": "x", "k_fine": 1, "k_medium": 1,
                                            "k_coarse": 1, "jaccard": 1.0, "is_current": 1}])
        rawstore.write_rows("ticker_themes", [
            {"ticker": t, "theme_id": f"run1:m{i % 3}", "theme": f"Theme {i % 3}", "level": "medium",
             "theme_run_id": "run1"} for i, t in enumerate(tickers) if i % 2 == 0])
    return tickers


def _run(monkeypatch, tickers):
    rows = [{"ticker": t, "quote": dict(_QUOTE), "market_cap": 5e9} for t in tickers]
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})
    return engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)


class _Counter:
    def __init__(self, monkeypatch):
        self.n = 0
        real = rawstore.query

        def counting(*a, **k):
            self.n += 1
            return real(*a, **k)
        monkeypatch.setattr(rawstore, "query", counting)


def test_the_store_reads_do_not_grow_with_the_number_of_tickers(rawstore_tmp_db, monkeypatch):
    _Yahoo(monkeypatch)
    _seed(4)
    small = _Counter(monkeypatch)
    # "G..." names have no stored row at all: a store-only run must not look for them one by one either
    _run(monkeypatch, [f"T{i:03d}" for i in range(4)] + [f"G{i:03d}" for i in range(2)])
    n_small = small.n
    _seed(24)
    big = _Counter(monkeypatch)
    _run(monkeypatch, [f"T{i:03d}" for i in range(24)] + [f"G{i:03d}" for i in range(12)])
    assert big.n == n_small


def test_the_bulk_run_gives_the_cards_of_the_per_ticker_path(rawstore_tmp_db, monkeypatch):
    _Yahoo(monkeypatch)
    tickers = _seed(16)
    bulk = _run(monkeypatch, tickers)["cards"]
    one_by_one = [c for c in (engine.enrich_and_score(t, {"ticker": t, "quote": dict(_QUOTE), "market_cap": 5e9},
                                                      no_fetch=True, spine={}) for t in tickers) if c]
    key = lambda c: c["ticker"]
    a, b = sorted(bulk, key=key), sorted(one_by_one, key=key)
    run_only = {k for k in a[0] if k not in b[0]}
    assert all(k in a[0] for k in b[0])
    for card in a:           # what only the full run adds: 13F and index fields
        for k in run_only:
            card.pop(k)
    assert a == b and len(a) == 14          # 2 of 16 are foreign filers


def test_a_themed_card_carries_the_run_label_and_an_unthemed_one_stays_empty(rawstore_tmp_db, monkeypatch):
    _Yahoo(monkeypatch)
    tickers = _seed(4)
    cards = {c["ticker"]: c for c in _run(monkeypatch, tickers)["cards"]}
    assert cards["T000"]["themes"] == ["Theme 0"]
    assert cards["T001"]["themes"] == []          # C21 (#383): no pinned medium theme → no label


# ── review M1 (and L1): a stale full row's .info is refreshed once a day, and the Refresh sees it ──

def _stale_full_row(ticker="AAA", days=2.0):
    old = time.time() - days * 86400
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": ticker, "payload": {"info": {"longName": "Old Name"}, "annual": copy.deepcopy(_YF_ANNUAL),
                                      "_fetched_at": old}, "fetched_at": old, "fetched_at_iso": ""}])


def _fetch(ticker="AAA", no_fetch=False):
    return engine._cached_financials(ticker, no_fetch=no_fetch, preload=engine._preload([ticker], no_fetch))


def test_a_stale_full_row_costs_one_info_call_a_day_through_the_bulk_path(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    _stale_full_row()
    yahoo = _Yahoo(monkeypatch)
    assert _fetch()["info"]["longName"] == "AAA Inc"
    assert yahoo.info_calls == ["AAA"]            # the stale payload is not served on a Fetch (L1)
    assert _fetch()["info"]["longName"] == "AAA Inc"
    assert yahoo.info_calls == ["AAA"]            # the second Fetch of the day asks nothing


def test_the_refresh_shows_the_info_the_fetch_saw_and_financials_keeps_its_years(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    _stale_full_row()
    _Yahoo(monkeypatch)
    _fetch()
    assert _fetch(no_fetch=True)["info"]["longName"] == "AAA Inc"
    stored = rawstore.get("ticker_payload_cache", "AAA")["payload"]
    assert stored["annual"] == _YF_ANNUAL and "info_only" not in stored
    assert cache_manager.load("AAA") is None      # the statements are still two days old for everyone else
