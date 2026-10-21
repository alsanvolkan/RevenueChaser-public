"""#150 (U13): the bulk readers give what the per-key readers give, in one query."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore, taxonomy
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_relatedness import themes


def _count_queries(monkeypatch):
    calls = []
    real = rawstore.query

    def counting(*a, **k):
        calls.append(a[0])
        return real(*a, **k)
    monkeypatch.setattr(rawstore, "query", counting)
    return calls


def test_get_many_matches_get_row_for_row(rawstore_tmp_db):
    rawstore.write_rows("ticker_payload_cache", [
        {"ticker": t, "payload": {"info": {"n": t}, "annual": [{"fiscal_year": "2024"}]},
         "fetched_at": 1.0, "fetched_at_iso": ""} for t in ("AAA", "BBB", "CCC")])
    many = rawstore.get_many("ticker_payload_cache", ["AAA", "CCC", "ZZZ", "AAA"])
    assert sorted(many) == ["AAA", "CCC"]
    assert many["AAA"] == rawstore.get("ticker_payload_cache", "AAA")


def test_get_many_is_one_query_and_empty_keys_none(rawstore_tmp_db, monkeypatch):
    calls = _count_queries(monkeypatch)
    assert rawstore.get_many("ticker_payload_cache", []) == {}
    assert calls == []
    rawstore.get_many("ticker_payload_cache", [f"T{i}" for i in range(50)])
    assert len(calls) == 1


def test_get_many_narrows_the_columns_but_keeps_the_key(rawstore_tmp_db):
    rawstore.write_rows("ticker_payload_cache", [
        {"ticker": "AAA", "payload": {"a": 1}, "fetched_at": 1.0, "fetched_at_iso": "x"}])
    row = rawstore.get_many("ticker_payload_cache", ["AAA"], columns=["payload"])["AAA"]
    assert set(row) == {"ticker", "payload"}


def test_get_many_refuses_a_composite_key_and_an_unknown_column(rawstore_tmp_db):
    with pytest.raises(rawstore.RawStoreError):
        rawstore.get_many("ticker_themes", ["AAA"])
    with pytest.raises(rawstore.RawStoreError):
        rawstore.get_many("ticker_payload_cache", ["AAA"], columns=["nope"])


def test_stored_annual_many_matches_get_annual(rawstore_tmp_db):
    annual = [{"fiscal_year": 2023, "revenue": 5.0}]
    rawstore.write_rows("edgar_financials", [
        {"ticker": "AAA", "annual": annual, "quarterly": [], "meta": {"entity_name": "Aaa", "is_us": True}},
        {"ticker": "BBB", "annual": [], "quarterly": [], "meta": {}},
    ])
    many = sec_edgar.stored_annual_many(["aaa", "BBB", "CCC"])
    assert many["AAA"] == sec_edgar.get_annual("AAA", no_fetch=True)
    assert many["BBB"] == sec_edgar.get_annual("BBB", no_fetch=True)
    assert "CCC" not in many


def _seed_themes():
    rawstore.write_rows("theme_runs", [{"id": "r1", "created_at": "x", "k_fine": 1, "k_medium": 1,
                                        "k_coarse": 1, "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("relatedness_themes", [{"theme_id": "r1:m9", "level": "medium", "parent_id": None,
                                                "label": "Label From Catalog", "size": 3}])
    rawstore.write_rows("ticker_themes", [
        {"ticker": "AAA", "theme_id": "r1:m0", "theme": "Chips", "level": "medium", "theme_run_id": "r1"},
        {"ticker": "BBB", "theme_id": "r1:m9", "theme": "", "level": "medium", "theme_run_id": "r1"},
        {"ticker": "CCC", "theme_id": "r1:c0", "theme": "Coarse", "level": "coarse", "theme_run_id": "r1"},
    ])


def test_themes_for_tickers_match_themes_for_ticker(rawstore_tmp_db):
    _seed_themes()
    many = themes.themes_for_tickers(["aaa", "BBB", "CCC", "DDD"])
    assert many == {"AAA": ["Chips"], "BBB": ["Label From Catalog"]}
    for t in ("AAA", "BBB", "CCC", "DDD"):
        assert many.get(t, []) == themes.themes_for_ticker(t)


def test_themes_for_tickers_is_a_fixed_number_of_queries(rawstore_tmp_db, monkeypatch):
    _seed_themes()
    calls = _count_queries(monkeypatch)
    themes.themes_for_tickers(["AAA", "BBB"])
    small = len(calls)
    calls.clear()
    themes.themes_for_tickers([f"T{i}" for i in range(80)] + ["AAA", "BBB"])
    assert len(calls) == small


def test_themes_for_many_never_breaks_a_scan(monkeypatch):
    def boom(_t):
        raise RuntimeError("store down")
    monkeypatch.setattr(themes, "themes_for_tickers", boom)
    assert taxonomy.themes_for_many(["AAA"]) == {}


def test_load_many_applies_the_ttl_and_info_only_rules_of_load(rawstore_tmp_db):
    import time
    from RevenueChaser.core.providers import cache
    old = time.time() - 30 * 86400
    rawstore.write_rows("ticker_payload_cache", [
        {"ticker": "OLD", "payload": {"annual": [{"fiscal_year": "2020"}], "_fetched_at": old},
         "fetched_at": old, "fetched_at_iso": ""}])
    cache.save("NEW", {"annual": [{"fiscal_year": "2024"}]})
    cache.save("INFO", {"info": {"a": 1}, "annual": [], "info_only": True})
    names = ["OLD", "NEW", "INFO", "NONE"]
    for ttl in (False, True):
        for info in (False, True):
            many = cache.load_many(names, ignore_ttl=ttl, allow_info_only=info)
            assert many == {t: cache.load(t, ignore_ttl=ttl, allow_info_only=info) for t in names
                            if cache.load(t, ignore_ttl=ttl, allow_info_only=info) is not None}
    assert sorted(cache.load_many(names)) == ["NEW"]
    assert sorted(cache.load_many(names, ignore_ttl=True, allow_info_only=True)) == ["INFO", "NEW", "OLD"]


def test_stored_annual_many_turns_a_stray_meta_string_into_an_empty_dict(monkeypatch):
    row = {"annual": [{"fiscal_year": 2023}], "quarterly": [], "meta": '{"is_us": true}'}
    monkeypatch.setattr(sec_edgar.rawstore, "get_many", lambda *a, **k: {"AAA": row})
    assert sec_edgar.stored_annual_many(["AAA"])["AAA"][1] == {}
    row["annual"] = "not a list"
    assert sec_edgar.stored_annual_many(["AAA"]) == {}
