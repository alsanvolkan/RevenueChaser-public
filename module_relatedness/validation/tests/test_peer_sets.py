"""Step 0 peer-set files and loader (Relatedness v2, #341)."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness.validation import peer_sets
from RevenueChaser.rcdata.tests import spine_fixture


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


def test_etf_holdings_from_store_reads_the_spines_whole_lists(rawstore_tmp_db):
    """D4 of the 9 Nov 2026 review (E1, #342): the peer sets read the spine's ``etf_holdings``, each fund's newest
    day; ``raw.duckdb.etf_holdings`` (a top 10, empty on homesrv) is not read."""
    spine_fixture.write_etf_holdings({"SMH": ["NVDA", "INTC"], "XLK": ["AAPL"]}, as_of="2026-11-05")
    spine_fixture.write_etf_holdings({"SMH": ["NVDA", "TSM"]}, as_of="2026-11-07")
    rawstore.write_rows("etf_holdings", [{"ticker": "KRE", "entry": [{"ticker": "USB", "weight": 0.2}]}])
    out = peer_sets.etf_holdings_from_store()
    assert out["SMH"] == {"NVDA", "TSM"}
    assert "XLK" not in out          # not one of the 29
    assert out["KRE"] == set()       # the raw table is not read
    assert len(out) == 29


def test_etf_holdings_from_store_never_writes_the_spine():
    """Reviews S1 and R2 (#342): the read goes through a read-only handle and leaves the file as it was."""
    import os

    from RevenueChaser.rcdata import serving
    spine_fixture.write_etf_holdings({"XBI": ["VRTX"]})
    path = serving.live_path("spine")
    mtime = os.stat(path).st_mtime_ns
    assert peer_sets.etf_holdings_from_store()["XBI"] == {"VRTX"}
    assert os.stat(path).st_mtime_ns == mtime
