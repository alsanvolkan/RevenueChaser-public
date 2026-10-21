"""cache.py tests — TTL expiry, ignore_ttl, get_or_fetch dispatch (rawstore_tmp_db, no network)."""
from __future__ import annotations

import time

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import cache as c


def test_load_missing_ticker_returns_none(rawstore_tmp_db):
    assert c.load("ZZZZ") is None


def test_save_then_load_roundtrip(rawstore_tmp_db):
    c.save("aapl", {"revenue": 100, "annual": [{"fiscal_year": "2025"}]})
    loaded = c.load("AAPL")
    assert loaded["revenue"] == 100
    assert "_fetched_at" in loaded
    assert "_fetched_at_iso" in loaded


def test_save_uses_uppercased_ticker_key(rawstore_tmp_db):
    c.save("aapl", {"revenue": 1, "annual": [{"fiscal_year": "2025"}]})
    row = rawstore.get("ticker_payload_cache", "AAPL")
    assert row is not None
    assert row["ticker"] == "AAPL"


def test_load_corrupt_payload_returns_none(rawstore_tmp_db):
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "ZZZZ", "payload": "not a dict", "fetched_at": time.time(),
        "fetched_at_iso": "",
    }])
    assert c.load("ZZZZ") is None


def test_load_returns_none_when_ttl_expired(rawstore_tmp_db):
    stale_fetched_at = 1_000_000.0
    c.save("ZZZZ", {"revenue": 1})
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "ZZZZ", "payload": {"revenue": 1, "_fetched_at": stale_fetched_at},
        "fetched_at": stale_fetched_at, "fetched_at_iso": "",
    }])
    monkey_now = stale_fetched_at + c._TTL_SECONDS + 10

    def _now():
        return monkey_now

    orig = c._now_ts
    c._now_ts = _now
    try:
        assert c.load("ZZZZ") is None
    finally:
        c._now_ts = orig


def test_load_ignore_ttl_returns_stale_data(rawstore_tmp_db):
    stale_fetched_at = 1_000_000.0
    c.save("ZZZZ", {"revenue": 1})
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "ZZZZ", "payload": {"revenue": 1, "_fetched_at": stale_fetched_at},
        "fetched_at": stale_fetched_at, "fetched_at_iso": "",
    }])
    monkey_now = stale_fetched_at + c._TTL_SECONDS + 10

    def _now():
        return monkey_now

    orig = c._now_ts
    c._now_ts = _now
    try:
        result = c.load("ZZZZ", ignore_ttl=True)
    finally:
        c._now_ts = orig
    assert result["revenue"] == 1


def test_load_within_ttl_returns_data(rawstore_tmp_db):
    fetched_at = time.time()
    c.save("ZZZZ", {"revenue": 1})
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "ZZZZ", "payload": {"revenue": 1, "_fetched_at": fetched_at},
        "fetched_at": fetched_at, "fetched_at_iso": "",
    }])
    monkey_now = fetched_at + 10

    def _now():
        return monkey_now

    orig = c._now_ts
    c._now_ts = _now
    try:
        result = c.load("ZZZZ")
    finally:
        c._now_ts = orig
    assert result["revenue"] == 1


def test_get_or_fetch_returns_cached_without_calling_fetcher(rawstore_tmp_db):
    c.save("ZZZZ", {"revenue": 1, "annual": [{"fiscal_year": "2025"}]})
    calls = []

    def fetcher(ticker):
        calls.append(ticker)
        return {"revenue": 999}

    result = c.get_or_fetch("ZZZZ", fetcher)
    assert result["revenue"] == 1
    assert calls == []


def test_get_or_fetch_calls_fetcher_and_saves_on_cache_miss(rawstore_tmp_db):
    calls = []

    def fetcher(ticker):
        calls.append(ticker)
        return {"revenue": 42, "annual": [{"fiscal_year": "2025"}]}

    result = c.get_or_fetch("ZZZZ", fetcher)
    assert result["revenue"] == 42
    assert calls == ["ZZZZ"]
    assert c.load("ZZZZ")["revenue"] == 42


def test_get_or_fetch_use_cache_false_skips_cache_and_still_saves(rawstore_tmp_db):
    c.save("ZZZZ", {"revenue": 1})
    calls = []

    def fetcher(ticker):
        calls.append(ticker)
        return {"revenue": 2}

    result = c.get_or_fetch("ZZZZ", fetcher, use_cache=False)
    assert result["revenue"] == 2
    assert calls == ["ZZZZ"]


def test_get_or_fetch_does_not_save_falsy_fetch_result(rawstore_tmp_db):
    result = c.get_or_fetch("ZZZZ", lambda ticker: {})
    assert result == {}
    assert c.load("ZZZZ") is None


# --- #169a / P8: an empty yfinance answer must not replace a good payload -----------
#
# ``yfinance.get_financials`` returns ``{"ticker", "info": {}, "sector", "annual": []}`` when
# Yahoo is down or rate limited. That dict is truthy, so the old ``if data:`` guard saved it,
# stamped it fresh, and the next read served an empty board for the whole TTL.

_EMPTY = {"ticker": "ZZZZ", "info": {}, "sector": "", "annual": []}
_FULL = {"ticker": "ZZZZ", "info": {"sector": "Tech"}, "sector": "Tech",
         "annual": [{"fiscal_year": "2025", "revenue": 100.0}]}
_INFO_ONLY = {"ticker": "ZZZZ", "info": {"sector": "Tech", "marketCap": 5}, "sector": "Tech", "annual": []}
_LONG_AGO = 1_000_000.0


def _store_full_long_ago():
    """A good payload that is far past its TTL (so ``load`` ignores it, ``ignore_ttl`` does not)."""
    payload = {**_FULL, "_fetched_at": _LONG_AGO, "_fetched_at_iso": "2020-01-01T00:00:00+00:00"}
    rawstore.write_rows("ticker_payload_cache", [{
        "ticker": "ZZZZ", "payload": payload, "fetched_at": _LONG_AGO,
        "fetched_at_iso": payload["_fetched_at_iso"]}])
    return payload


def test_save_does_not_write_an_empty_payload(rawstore_tmp_db):
    assert c.save("ZZZZ", dict(_EMPTY)) is False
    assert rawstore.get("ticker_payload_cache", "ZZZZ") is None
    assert c.load("ZZZZ") is None


def test_save_keeps_the_stored_payload_when_the_new_one_is_empty(rawstore_tmp_db):
    stored = _store_full_long_ago()
    assert c.save("ZZZZ", dict(_EMPTY)) is False
    assert rawstore.get("ticker_payload_cache", "ZZZZ")["payload"] == stored


def test_save_still_writes_a_payload_that_has_annual_or_info(rawstore_tmp_db):
    assert c.save("AAA", {"info": {}, "annual": [{"fiscal_year": "2025"}]}) is True
    assert c.save("BBB", {"info": {"sector": "Tech"}, "annual": []}) is True   # info-only, nothing stored yet
    assert c.load("AAA")["annual"] and c.load("BBB")["info"] == {"sector": "Tech"}


_NO_ANNUAL = {"ticker": "ZZZZ", "info": {"sector": "Tech", "marketCap": 5}, "sector": "Tech", "annual": []}


def test_a_payload_without_annual_does_not_overwrite_a_stored_annual(rawstore_tmp_db):
    stored = _store_full_long_ago()
    assert c.save("ZZZZ", dict(_NO_ANNUAL)) is False
    row = rawstore.get("ticker_payload_cache", "ZZZZ")
    assert row["payload"] == stored                      # annual, info and _fetched_at all unchanged
    assert row["fetched_at"] == _LONG_AGO


def test_an_info_only_payload_may_replace_an_info_only_payload(rawstore_tmp_db):
    c.save("ZZZZ", {"info": {"sector": "Old"}, "annual": []})
    assert c.save("ZZZZ", {"info": {"sector": "New"}, "annual": []}) is True
    assert c.load("ZZZZ")["info"] == {"sector": "New"}


def test_a_payload_with_annual_replaces_the_stored_one(rawstore_tmp_db):
    _store_full_long_ago()
    newer = {**_FULL, "annual": [{"fiscal_year": "2026", "revenue": 120.0}]}
    assert c.save("ZZZZ", newer) is True
    assert c.load("ZZZZ")["annual"][0]["fiscal_year"] == "2026"


def test_get_or_fetch_returns_the_previous_payload_flagged_stale_when_the_refetch_is_empty(rawstore_tmp_db):
    stored = _store_full_long_ago()
    result = c.get_or_fetch("ZZZZ", lambda t: dict(_EMPTY))
    assert result["_stale"] is True
    assert result["annual"] == stored["annual"] and result["info"] == stored["info"]
    assert rawstore.get("ticker_payload_cache", "ZZZZ")["payload"] == stored   # the store is untouched


def test_get_or_fetch_serves_the_previous_payload_of_any_age(rawstore_tmp_db):
    stored = _store_full_long_ago()                       # ~56 years older than any TTL
    assert c.load("ZZZZ") is None                         # expired for a normal read
    assert c.get_or_fetch("ZZZZ", lambda t: {})["annual"] == stored["annual"]


def test_get_or_fetch_with_an_empty_refetch_and_nothing_stored_returns_the_empty_payload_unsaved(rawstore_tmp_db):
    result = c.get_or_fetch("ZZZZ", lambda t: dict(_EMPTY))
    assert result["annual"] == [] and result.get("_stale") is not True
    assert rawstore.get("ticker_payload_cache", "ZZZZ") is None


def test_get_or_fetch_with_an_info_only_refetch_keeps_the_stored_annual(rawstore_tmp_db):
    stored = _store_full_long_ago()
    result = c.get_or_fetch("ZZZZ", lambda t: dict(_NO_ANNUAL))
    assert result["_stale"] is True and result["annual"] == stored["annual"]
    assert rawstore.get("ticker_payload_cache", "ZZZZ")["payload"] == stored


def test_get_or_fetch_does_not_flag_a_good_refetch_stale(rawstore_tmp_db):
    _store_full_long_ago()
    newer = {**_FULL, "annual": [{"fiscal_year": "2026"}]}
    result = c.get_or_fetch("ZZZZ", lambda t: dict(newer))
    assert "_stale" not in result and result["annual"][0]["fiscal_year"] == "2026"


def test_get_or_fetch_fresh_cache_is_not_marked_stale(rawstore_tmp_db):
    c.save("ZZZZ", dict(_FULL))
    assert "_stale" not in c.get_or_fetch("ZZZZ", lambda t: dict(_EMPTY))


def test_save_refuses_a_payload_carrying_stale(rawstore_tmp_db):
    assert c.save("ZZZZ", {**_FULL, "_stale": True}) is False
    assert rawstore.get("ticker_payload_cache", "ZZZZ") is None


def test_a_stale_payload_saved_back_does_not_restart_the_ttl(rawstore_tmp_db):
    """``get_or_fetch`` hands back an old payload flagged ``_stale``; a caller that saves it again must not
    stamp it with a new ``_fetched_at``, or the 24 h TTL would start over on data that is not fresh."""
    stored = _store_full_long_ago()
    stale = c.get_or_fetch("ZZZZ", lambda t: dict(_EMPTY))
    assert stale["_stale"] is True
    assert c.save("ZZZZ", stale) is False                 # a caller that round-trips the payload
    assert rawstore.get("ticker_payload_cache", "ZZZZ")["payload"] == stored   # not rewritten
    assert c.load("ZZZZ") is None                         # still expired: the next read refetches


def test_a_payload_without_the_stale_marker_is_still_saved(rawstore_tmp_db):
    assert c.save("ZZZZ", dict(_FULL)) is True
    assert "_stale" not in c.load("ZZZZ")


def test_the_asymmetric_growth_save_pattern_cannot_cache_an_empty_payload(rawstore_tmp_db):
    """AG saves the raw ``get_financials`` result right after a miss (``engine.py``): the guard is here."""
    c.save("ZZZZ", dict(_EMPTY))
    assert c.load("ZZZZ") is None and c.load("ZZZZ", ignore_ttl=True) is None


# ── info-only payloads (#150 U13) ─────────────────────────────────────────────

_INFO_ONLY = {"info": {"country": "United States"}, "annual": [], "info_only": True}


def test_an_info_only_payload_is_a_miss_for_every_other_reader(rawstore_tmp_db):
    assert c.save("AAAA", dict(_INFO_ONLY)) is True
    assert c.load("AAAA") is None
    assert c.load("AAAA", ignore_ttl=True) is None


def test_an_info_only_payload_is_a_hit_for_a_reader_that_asks_for_it(rawstore_tmp_db):
    c.save("AAAA", dict(_INFO_ONLY))
    assert c.load("AAAA", allow_info_only=True)["info"]["country"] == "United States"
    assert c.load("AAAA", ignore_ttl=True, allow_info_only=True) is not None


def test_get_or_fetch_refetches_over_an_info_only_payload(rawstore_tmp_db):
    c.save("AAAA", dict(_INFO_ONLY))
    full = {"info": {"country": "United States"}, "annual": [{"fiscal_year": "2024", "revenue": 1.0}]}
    assert c.get_or_fetch("AAAA", lambda t: full) == full
    assert c.load("AAAA")["annual"]


def test_a_full_payload_replaces_an_info_only_one(rawstore_tmp_db):
    c.save("AAAA", dict(_INFO_ONLY))
    c.save("AAAA", {"info": {}, "annual": [{"fiscal_year": "2024"}]})
    assert c.load("AAAA")["annual"] == [{"fiscal_year": "2024"}]
    assert "info_only" not in c.load("AAAA")


# ── #150 review M1: an info-only payload refreshes the .info of a stored full row ──

_M1_FULL = {"info": {"country": "Old", "longName": "Old Inc"}, "annual": [{"fiscal_year": "2023"}],
         "market_cap": 1.0, "source": "yfinance"}
_M1_INFO = {"info": {"country": "United States", "longName": "New Inc"}, "annual": [], "market_cap": 5e9,
             "info_only": True, "source": "yfinance"}


def _age_statements(ticker: str, days: float):
    row = rawstore.get("ticker_payload_cache", ticker)
    payload = dict(row["payload"], _fetched_at=time.time() - days * 86400)
    rawstore.write_rows("ticker_payload_cache", [{"ticker": ticker, "payload": payload,
                                                  "fetched_at": payload["_fetched_at"], "fetched_at_iso": ""}])


def test_an_info_only_payload_refreshes_the_info_of_a_stored_full_row(rawstore_tmp_db):
    c.save("AAAA", _M1_FULL)
    _age_statements("AAAA", 2.0)
    assert c.save("AAAA", _M1_INFO) is True
    stored = rawstore.get("ticker_payload_cache", "AAAA")["payload"]
    assert stored["info"]["country"] == "United States" and stored["market_cap"] == 5e9
    assert stored["annual"] == _M1_FULL["annual"] and "info_only" not in stored   # still a full row
    assert time.time() - stored["_fetched_at"] > 1.9 * 86400                     # the statements keep their age
    assert time.time() - stored["_info_fetched_at"] < 60
    assert time.time() - rawstore.get("ticker_payload_cache", "AAAA")["fetched_at"] > 1.9 * 86400   # the row's own column too


def test_the_refreshed_info_is_a_hit_for_the_reader_that_asks_for_it_and_a_miss_for_the_others(rawstore_tmp_db):
    c.save("AAAA", _M1_FULL)
    _age_statements("AAAA", 2.0)
    c.save("AAAA", _M1_INFO)
    assert c.load("AAAA", allow_info_only=True)["info"]["country"] == "United States"
    assert c.load_many(["AAAA"], allow_info_only=True)["AAAA"]["info"]["country"] == "United States"
    assert c.load("AAAA") is None and c.load_many(["AAAA"]) == {}            # statements are 2 days old


def test_an_old_info_stamp_does_not_make_an_old_row_fresh(rawstore_tmp_db):
    c.save("AAAA", _M1_FULL)
    _age_statements("AAAA", 2.0)
    c.save("AAAA", _M1_INFO)
    row = rawstore.get("ticker_payload_cache", "AAAA")
    payload = dict(row["payload"], _info_fetched_at=time.time() - 3 * 86400)
    rawstore.write_rows("ticker_payload_cache", [{"ticker": "AAAA", "payload": payload,
                                                  "fetched_at": row["fetched_at"], "fetched_at_iso": ""}])
    assert c.load("AAAA", allow_info_only=True) is None


def test_an_empty_info_only_payload_still_changes_nothing_over_a_full_row(rawstore_tmp_db):
    c.save("AAAA", _M1_FULL)
    before = rawstore.get("ticker_payload_cache", "AAAA")["payload"]
    assert c.save("AAAA", {"info": {}, "annual": [], "info_only": True}) is False
    assert rawstore.get("ticker_payload_cache", "AAAA")["payload"] == before
