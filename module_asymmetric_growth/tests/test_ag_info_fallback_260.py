"""#260 (C15): a failed or empty AG refetch keeps the expired ``ticker_payload_cache`` row.

The Fetch path used to overwrite / drop the cached payload whenever Yahoo failed. Now it keeps the expired row
(marked ``_stale``, never written back) and only saves a fresh answer; the scorer still gets the cached info.
"""
from __future__ import annotations

import copy
import time

from RevenueChaser.core import rawstore
from RevenueChaser.module_asymmetric_growth import engine

_ANNUAL = [{"fiscal_year": y, "revenue": r, "gross_profit": r * 0.6, "operating_cash_flow": r * 0.3,
            "capex": r * 0.05, "total_debt": 10.0, "cash": 200.0, "total_assets": 1000.0,
            "rd_expense": r * 0.2, "diluted_shares": 100.0 + i}
           for i, (y, r) in enumerate([(2021, 100.0), (2022, 130.0), (2023, 190.0)])]


def _edgar(ticker: str) -> dict:
    return {"ticker": ticker, "annual": copy.deepcopy(_ANNUAL), "quarterly": [],
            "meta": {"source": "sec_edgar", "entity_name": f"{ticker} Corp", "is_us": True, "available": True}}


def _stale_full_row(ticker: str = "AAA", age_days: float = 2.0) -> None:
    old = time.time() - age_days * 86400
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": ticker,
        "payload": {"info": {"longName": "Old Name", "country": "United States"},
                    "annual": copy.deepcopy(_ANNUAL), "_fetched_at": old},
        "fetched_at": old, "fetched_at_iso": ""}])


def _no_edgar_network(monkeypatch) -> None:
    monkeypatch.setattr(engine.sec_edgar, "_cik_map", lambda: {})


def test_a_failed_refetch_keeps_the_expired_row_and_hands_the_scorer_stale_info(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    _stale_full_row()
    _no_edgar_network(monkeypatch)
    before_row = rawstore.get("ticker_payload_cache", "AAA")
    before = before_row["payload"]

    def boom(ticker):
        raise OSError("Too Many Requests. Rate limited.")

    monkeypatch.setattr(engine.yfinance_client, "get_info", boom)

    fin = engine._cached_financials("AAA")
    assert fin["info"]["longName"] == "Old Name" and fin["_stale"] is True
    assert fin["annual"]                              # the stored EDGAR years still score
    after = rawstore.get("ticker_payload_cache", "AAA")
    assert after["payload"] == before
    # Never written back: a re-save would stamp the expired row as fresh for the cache TTL (#260).
    assert after["fetched_at"] == before_row["fetched_at"]


def test_a_stub_refetch_keeps_the_expired_row(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    _stale_full_row()
    _no_edgar_network(monkeypatch)
    before = rawstore.get("ticker_payload_cache", "AAA")["payload"]
    monkeypatch.setattr(engine.yfinance_client, "get_info",
                        lambda t: {"ticker": t, "info": {"trailingPegRatio": None},
                                   "annual": [], "info_only": True})
    fin = engine._cached_financials("AAA")
    assert fin["info"]["longName"] == "Old Name" and fin["_stale"] is True
    assert rawstore.get("ticker_payload_cache", "AAA")["payload"] == before


def test_a_good_refetch_is_saved(rawstore_tmp_db, monkeypatch):
    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    _stale_full_row()
    _no_edgar_network(monkeypatch)
    monkeypatch.setattr(engine.yfinance_client, "get_info",
                        lambda t: {"ticker": t, "info": {"longName": "New Name", "country": "United States"},
                                   "annual": [], "info_only": True, "source": "yfinance"})
    fin = engine._cached_financials("AAA")
    assert fin["info"]["longName"] == "New Name" and "_stale" not in fin
    stored = rawstore.get("ticker_payload_cache", "AAA")["payload"]
    assert stored["info"]["longName"] == "New Name"
    assert stored["annual"]                           # the info-only merge keeps the years


def test_a_failed_refetch_without_edgar_keeps_the_stored_full_row(rawstore_tmp_db, monkeypatch):
    """No EDGAR row: the full cached payload serves the name even when the refetch fails."""
    _stale_full_row()
    _no_edgar_network(monkeypatch)

    def boom(ticker):
        raise OSError("down")

    monkeypatch.setattr(engine.yfinance_client, "get_financials", boom)
    fin = engine._cached_financials("AAA")
    assert fin["_stale"] is True and fin["annual"]
    assert rawstore.get("ticker_payload_cache", "AAA")["payload"].get("_stale") is None


def test_an_unexpected_refetch_error_still_reaches_the_score_errors(rawstore_tmp_db, monkeypatch):
    """R2B-3: AG catches only what main caught (OSError, ValueError, KeyError); anything else propagates, so the
    run's score_errors (#202) and the 1% freeze guard still see it."""
    import pytest

    rawstore.write_rows("edgar_financials", [_edgar("AAA")])
    _stale_full_row()
    _no_edgar_network(monkeypatch)

    def boom(ticker):
        raise TypeError("a bug, not a provider failure")

    monkeypatch.setattr(engine.yfinance_client, "get_info", boom)
    with pytest.raises(TypeError):
        engine._cached_financials("AAA")


def test_an_info_only_reply_does_not_replace_a_full_row_for_a_name_with_no_edgar(rawstore_tmp_db, monkeypatch):
    """R2B-4: with no EDGAR years only ``annual`` makes the reply an answer; an info-only reply keeps the expired full
    row, which still scores the name (it would otherwise be saved over it and the name drop as no_annual)."""
    _stale_full_row()
    _no_edgar_network(monkeypatch)
    before = rawstore.get("ticker_payload_cache", "AAA")
    monkeypatch.setattr(engine.yfinance_client, "get_financials",
                        lambda t: {"ticker": t, "info": {"longName": "New Name"}, "annual": []})
    fin = engine._cached_financials("AAA")
    assert fin["_stale"] is True and fin["annual"] and fin["info"]["longName"] == "Old Name"
    after = rawstore.get("ticker_payload_cache", "AAA")
    assert after["payload"] == before["payload"] and after["fetched_at"] == before["fetched_at"]


class FakeVendor:
    """Records ``add_yfinance`` the way the run's vendor capture does on a preload batch."""

    def __init__(self):
        self.yfinance = []

    def add_yfinance(self, ticker, payload):
        self.yfinance.append((ticker, payload))


def _hand_preload(ticker="AAA"):
    """The batch dict ``_preload`` builds, plus the vendor and drops the run attaches, built by hand."""
    row = _edgar(ticker)
    vendor = FakeVendor()
    drops = engine._Drops()
    preload = {
        "edgar": {ticker: (row["annual"], row["meta"])},
        "payload": {},
        "vendor": vendor,
        "drops": drops,
    }
    return preload, vendor, drops


def _refuse_full_fetch(monkeypatch):
    def boom(ticker):
        raise AssertionError("get_financials")

    monkeypatch.setattr(engine.yfinance_client, "get_financials", boom)


def test_preload_failed_info_refetch_keeps_the_expired_row_and_counts_the_drop(rawstore_tmp_db, monkeypatch):
    """A stored full row plus a ``get_info`` that raises is stale: counted, not handed to the vendor."""
    _stale_full_row()
    _no_edgar_network(monkeypatch)
    _refuse_full_fetch(monkeypatch)
    preload, vendor, drops = _hand_preload()

    def boom(ticker):
        raise OSError("Too Many Requests. Rate limited.")

    monkeypatch.setattr(engine.yfinance_client, "get_info", boom)
    fin = engine._cached_financials("AAA", preload=preload)
    assert fin["_stale"] is True
    assert fin["info"]["longName"] == "Old Name"
    assert drops.info_refetch_failed == 1
    assert vendor.yfinance == []


def test_preload_fresh_info_reply_is_saved_once_and_handed_to_the_vendor(rawstore_tmp_db, monkeypatch):
    _stale_full_row()
    _no_edgar_network(monkeypatch)
    _refuse_full_fetch(monkeypatch)
    preload, vendor, drops = _hand_preload()
    saves = []
    real_save = engine.cache_manager.save

    def counting_save(ticker, data):
        saves.append(ticker)
        return real_save(ticker, data)

    monkeypatch.setattr(engine.cache_manager, "save", counting_save)
    monkeypatch.setattr(engine.yfinance_client, "get_info",
                        lambda t: {"ticker": t, "info": {"longName": "New Name", "country": "United States"},
                                   "annual": [], "info_only": True, "source": "yfinance"})
    fin = engine._cached_financials("AAA", preload=preload)
    assert fin["info"]["longName"] == "New Name" and "_stale" not in fin
    assert saves == ["AAA"]
    assert rawstore.get("ticker_payload_cache", "AAA")["payload"]["info"]["longName"] == "New Name"
    assert len(vendor.yfinance) == 1 and vendor.yfinance[0][0] == "AAA"
    assert drops.info_refetch_failed == 0


def test_preload_edgar_name_with_nothing_stored_and_a_failed_refetch_merges_edgar(rawstore_tmp_db, monkeypatch):
    """Nothing stored: the empty ``info`` is what EDGAR merges onto, and the miss is counted."""
    _no_edgar_network(monkeypatch)
    _refuse_full_fetch(monkeypatch)
    preload, vendor, drops = _hand_preload()

    def boom(ticker):
        raise OSError("down")

    monkeypatch.setattr(engine.yfinance_client, "get_info", boom)
    fin = engine._cached_financials("AAA", preload=preload)
    assert fin["annual"] == _edgar("AAA")["annual"]
    assert fin["source"] == "sec_edgar"
    assert fin["info"]["longName"] == "AAA Corp"
    assert fin["info"]["country"] == "United States"
    assert drops.info_refetch_failed == 1
    assert vendor.yfinance == []
    assert rawstore.get("ticker_payload_cache", "AAA") is None


def test_preload_info_only_reply_for_a_no_edgar_name_is_recorded_not_saved(rawstore_tmp_db, monkeypatch):
    """R2B-4 follow-up (R4 delta review): a no-EDGAR name whose refetch brings info but no statements keeps its expired
    full row (it still scores), yet the fresh reply is not dropped unrecorded: the run's vendor capture gets it (its
    valuation and ownership rows) and the refetch counts in info_refetch_failed."""
    _stale_full_row()
    _no_edgar_network(monkeypatch)
    preload, vendor, drops = _hand_preload()
    preload["edgar"] = {}                                 # no EDGAR years for this name
    before = rawstore.get("ticker_payload_cache", "AAA")
    reply = {"ticker": "AAA", "info": {"longName": "New Name", "marketCap": 5e9}, "annual": []}
    monkeypatch.setattr(engine.yfinance_client, "get_financials", lambda t: reply)
    fin = engine._cached_financials("AAA", preload=preload)
    assert fin["_stale"] is True and fin["annual"] and fin["info"]["longName"] == "Old Name"
    assert vendor.yfinance == [("AAA", reply)]
    assert drops.info_refetch_failed == 1
    after = rawstore.get("ticker_payload_cache", "AAA")
    assert after["payload"] == before["payload"] and after["fetched_at"] == before["fetched_at"]
