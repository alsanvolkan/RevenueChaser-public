"""Step 0 peer-set files and loader (Relatedness v2, #341)."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness.validation import peer_sets


def test_exactly_ten_hand_sets_with_ids_one_to_ten():
    version, sets = peer_sets.read_hand_sets()
    assert version == 1
    assert [s.id for s in sets] == list(range(1, 11))
    assert all(s.kind == "hand" for s in sets)


def test_sets_seven_to_ten_are_borderline_the_rest_clear():
    _, sets = peer_sets.read_hand_sets()
    assert [s.id for s in sets if s.borderline] == [7, 8, 9, 10]


def test_hand_sets_hold_five_to_fifteen_tickers():
    _, sets = peer_sets.read_hand_sets()
    assert all(5 <= len(s.tickers) <= 15 for s in sets)


def test_exactly_29_etfs_in_the_list_file():
    version, etfs = peer_sets.read_etf_list()
    assert version == 1 and len(etfs) == 29 and len(set(etfs)) == 29
    assert {"SMH", "XBI", "KRE", "PBW", "SOXX", "CIBR"} <= set(etfs)


def test_peer_set_files_carry_a_version_field():
    for path in (peer_sets.HAND_SETS_PATH, peer_sets.ETF_SETS_PATH):
        assert isinstance(json.loads(path.read_text(encoding="utf-8"))["version"], int)


def _copy(tmp_path, src, mutate):
    data = json.loads(src.read_text(encoding="utf-8"))
    mutate(data)
    p = tmp_path / src.name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_changing_a_hand_set_without_a_version_bump_is_refused(tmp_path):
    p = _copy(tmp_path, peer_sets.HAND_SETS_PATH, lambda d: d["sets"][5]["tickers"].append("KSU"))
    with pytest.raises(peer_sets.PeerSetVersionError, match="bump"):
        peer_sets.read_hand_sets(p)


def test_changing_the_etf_list_without_a_version_bump_is_refused(tmp_path):
    p = _copy(tmp_path, peer_sets.ETF_SETS_PATH, lambda d: d["etfs"].append("XLK"))
    with pytest.raises(peer_sets.PeerSetVersionError):
        peer_sets.read_etf_list(p)


def test_a_bumped_version_needs_its_own_fingerprint(tmp_path, monkeypatch):
    def bump(d):
        d["version"] = 2
        d["sets"][5]["tickers"].append("KSU")
    p = _copy(tmp_path, peer_sets.HAND_SETS_PATH, bump)
    with pytest.raises(peer_sets.PeerSetVersionError):
        peer_sets.read_hand_sets(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    monkeypatch.setitem(peer_sets.FINGERPRINTS, ("hand", 2), peer_sets.fingerprint(data["sets"]))
    version, sets = peer_sets.read_hand_sets(p)
    assert version == 2 and "KSU" in sets[5].tickers


def test_loader_drops_unknown_tickers_and_reports_them_per_set():
    universe = {"JPM", "BAC", "C", "WFC", "GS", "MS", "USB", "PNC",   # TFC missing
                "UNP", "CSX", "NSC"}                                  # CNI, CP missing
    loaded = peer_sets.hand_sets_for(universe)
    by_id = {s.id: s for s in loaded.sets}
    assert "TFC" not in by_id[1].tickers and len(by_id[1].tickers) == 8
    assert by_id[6].tickers == ("UNP", "CSX", "NSC")
    rep = {r["set"]: r for r in loaded.report}
    assert rep["us_large_banks"]["dropped"] == ["TFC"] and rep["us_large_banks"]["kept"] == 8
    assert rep["railroads"]["dropped"] == ["CNI", "CP"]
    # A set left with fewer than two names is dropped and reported, never a failure.
    assert 4 not in by_id and rep["homebuilders"]["kept"] == 0 and rep["homebuilders"]["excluded"]
    assert loaded.version == 1


def test_loader_matches_tickers_case_insensitively():
    loaded = peer_sets.hand_sets_for({"unp", "csx"})
    assert [s.tickers for s in loaded.sets] == [("UNP", "CSX")]


def test_etf_loader_drops_etfs_with_no_holdings_and_unknown_holdings():
    holdings = {"SMH": ["NVDA", "TSM", "AMD", "ZZZZ"], "KRE": [], "XBI": ["VRTX"]}
    universe = {"NVDA", "TSM", "AMD", "VRTX"}
    loaded = peer_sets.etf_sets_for(holdings, universe)
    assert [s.name for s in loaded.sets] == ["SMH"]
    assert loaded.sets[0].tickers == ("NVDA", "TSM", "AMD") and loaded.sets[0].kind == "etf"
    rep = {r["set"]: r for r in loaded.report}
    assert rep["SMH"]["dropped"] == ["ZZZZ"]
    assert rep["KRE"]["excluded"] == "no holdings" and rep["JETS"]["excluded"] == "no holdings"
    assert rep["XBI"]["excluded"]  # one name left
    assert len(loaded.report) == 29


def test_etf_holdings_from_store_reads_etf_holdings_only(rawstore_tmp_db):
    rawstore.write_rows("etf_holdings", [
        {"ticker": "SMH", "entry": [{"ticker": "nvda", "weight": 0.2}, {"ticker": "TSM", "weight": 0.1}]},
        {"ticker": "XLK", "entry": [{"ticker": "AAPL", "weight": 0.2}]},
    ])
    out = peer_sets.etf_holdings_from_store()
    assert out["SMH"] == {"NVDA", "TSM"}
    assert "XLK" not in out          # not one of the 29
    assert out["KRE"] == set()       # in the list, no holdings stored
    assert len(out) == 29


def test_etf_holdings_from_store_never_applies_the_manifest(monkeypatch):
    """Review S1 (#342): with RC_STORE_AUTO_ENSURE unset, a read must not create tables or columns."""
    import duckdb

    path = rawstore.DB_PATH                  # the per-test file from the autouse isolate_rawstore
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE etf_holdings (ticker TEXT PRIMARY KEY, entry JSON, provider TEXT, "
                    "fetched_at DOUBLE)")
        con.execute("INSERT INTO etf_holdings VALUES ('SMH', '[{\"ticker\": \"NVDA\"}]', 'x', 1.0)")
        con.execute("CREATE TABLE theme_runs (id TEXT PRIMARY KEY, created_at TEXT)")
    finally:
        con.close()

    def shape():
        c = duckdb.connect(str(path), read_only=True)
        try:
            return c.execute("SELECT table_name, column_name FROM information_schema.columns "
                             "ORDER BY table_name, column_name").fetchall()
        finally:
            c.close()

    before = shape()
    monkeypatch.setenv("RC_STORE_AUTO_ENSURE", "1")   # recorded, so the teardown restores the session's value
    monkeypatch.delenv("RC_STORE_AUTO_ENSURE")
    monkeypatch.setattr(rawstore, "_ENSURED", {})
    out = peer_sets.etf_holdings_from_store()
    assert out["SMH"] == {"NVDA"}
    assert shape() == before
    import os
    assert "RC_STORE_AUTO_ENSURE" not in os.environ      # the read put the caller's environment back
