"""NMF nested themes (Phase 8a) — synthetic profiles, offline."""
from __future__ import annotations

import json
import logging
import re

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import themes


def _profiles():
    p = {}
    for i in range(30):
        p[f"G{i}"] = {"products": ["gpu", "ai"], "end_markets": ["data center"],
                      "business_model": ["semis"], "customer_types": ["hyperscaler"],
                      "value_chain_role": ["designer"], "keywords": ["accelerated"]}
    for i in range(30):
        p[f"B{i}"] = {"products": ["loans", "deposits"], "end_markets": ["retail"],
                      "business_model": ["interest"], "customer_types": ["consumer"],
                      "value_chain_role": ["lender"], "keywords": ["branch"]}
    return p


def test_tag_matrix_max_df(rawstore_tmp_db):
    X, terms, tickers = themes.tag_matrix(_profiles())
    assert X.shape[0] == 60 and len(tickers) == 60
    assert "gpu" in terms and "loans" in terms


def test_build_shadow_writes_nested_themes(rawstore_tmp_db):
    out = themes.build_shadow(candidates=(2, 3, 4), k_medium=2, k_coarse=2,
                              run_id="test", profiles=_profiles())
    assert out["n"] == 60 and out["k_fine"] >= 2
    assert rawstore.count("relatedness_theme_members") == 60
    rows = rawstore.get_all("relatedness_themes")
    assert any(r["level"] == "fine" and r["parent_id"] for r in rows)
    assert any(r["level"] == "medium" for r in rows)
    assert any(r["level"] == "coarse" for r in rows)
    run = rawstore.get("theme_runs", "test")
    assert run["k_fine"] == out["k_fine"] and run["is_current"] == 0


def test_choose_k_is_deterministic(rawstore_tmp_db):
    X, _terms, _t = themes.tag_matrix(_profiles())
    assert themes.choose_k(X, [2, 3, 4]) == themes.choose_k(X, [2, 3, 4])


def test_cutover_materialize_pins_run(rawstore_tmp_db):
    rawstore.replace_all("equity_universe",
                         [{"ticker": f"G{i}", "market_cap": 1e9} for i in range(3)]
                         + [{"ticker": "ZZZ", "market_cap": 1e9}])
    themes.build_shadow(candidates=(2, 3), k_medium=2, k_coarse=2,
                        run_id="r1", profiles=_profiles())
    assert themes.current_run_id() is None          # a shadow run is never current
    mat = themes.cutover_materialize("r1")
    assert themes.current_run_id() == "r1"          # cutover is the only writer
    assert mat["rows"] >= 1
    n = rawstore.query(
        "SELECT count(*) n FROM ticker_themes WHERE theme_run_id='r1'")[0]["n"]
    assert n == mat["rows"]
    levels = {r["level"] for r in rawstore.query(
        "SELECT DISTINCT level FROM ticker_themes WHERE theme_run_id='r1' "
        "AND ticker LIKE 'G%'")}
    assert {"fine", "medium", "coarse"} <= levels


def test_relabel_and_themes_for_ticker(rawstore_tmp_db):
    from RevenueChaser.core import taxonomy
    rawstore.replace_all("equity_universe", [{"ticker": "G0", "market_cap": 1e9}])
    themes.build_shadow(candidates=(2, 3), k_medium=2, k_coarse=2,
                        run_id="r2", profiles=_profiles())
    themes.cutover_materialize("r2")
    assert themes.relabel_from_tags("r2") >= 1
    labels = themes.themes_for_ticker("G0")
    assert labels and all(isinstance(x, str) and x for x in labels)
    assert taxonomy.assign_themes(sector="Technology", ticker="G0") == labels


def _profiles4():
    """Four well-separated 25-name clusters (>= MIN_SIZE each)."""
    p = {}
    specs = [
        ("G", ["gpu", "ai"], ["data center"], ["semis"], ["hyperscaler"],
         ["designer"], ["accelerated"]),
        ("B", ["loans", "deposits"], ["retail"], ["interest"], ["consumer"],
         ["lender"], ["branch"]),
        ("P", ["crude", "refining"], ["transport"], ["integrated"], ["utility"],
         ["producer"], ["upstream"]),
        ("H", ["drugs", "biologics"], ["patients"], ["royalty"], ["hospital"],
         ["manufacturer"], ["clinical"]),
    ]
    for prefix, prod, mkt, bm, cust, vcr, kw in specs:
        for i in range(25):
            p[f"{prefix}{i}"] = {"products": prod, "end_markets": mkt,
                                 "business_model": bm, "customer_types": cust,
                                 "value_chain_role": vcr, "keywords": kw}
    return p


def test_build_shadow_true_nesting(rawstore_tmp_db):
    out = themes.build_shadow(candidates=(2, 3, 4), k_medium=4, k_coarse=2,
                              run_id="nest", profiles=_profiles4())
    rows = {r["theme_id"]: r for r in rawstore.get_all("relatedness_themes")}
    coarse_ids = {tid for tid, r in rows.items() if r["level"] == "coarse"}
    med_rows = [r for r in rows.values() if r["level"] == "medium"]
    fine_rows = [r for r in rows.values() if r["level"] == "fine"]
    assert coarse_ids and med_rows and fine_rows
    med_ids = {r["theme_id"] for r in med_rows}
    for f in fine_rows:
        assert f["parent_id"] in med_ids        # fine -> medium
        assert f["size"] > 0
    for m in med_rows:
        assert m["parent_id"] in coarse_ids     # medium -> coarse (was "")
        assert m["size"] > 0                    # (was 0)
    total = 0
    for cid in coarse_ids:
        c = rows[cid]
        assert c["parent_id"] == ""             # coarse is the root
        assert c["size"] == sum(m["size"] for m in med_rows
                                if m["parent_id"] == cid)
        total += c["size"]
    assert total == 100
    assert sum(m["size"] for m in med_rows) == 100
    assert out["k_coarse"] == len(coarse_ids)


def test_nested_parents_coarse_is_hierarchy():
    import numpy as np
    X = np.array([
        [10.0, 0.0], [9.0, 0.5], [8.0, 1.0],
        [0.0, 10.0], [0.5, 9.0], [1.0, 8.0],
        [10.0, 10.0], [9.5, 9.5], [9.0, 9.0],
        [-5.0, -5.0], [-5.5, -5.5], [-6.0, -6.0],
    ])
    fine = np.arange(12)
    ids, med, coarse = themes.nested_parents(X, fine, k_medium=4, k_coarse=2)
    assert len(ids) == 12
    for m in set(med.tolist()):
        assert len(set(coarse[med == m].tolist())) == 1   # one coarse per medium
    assert len(set(coarse.tolist())) == 2


def test_nested_parents_cuts_below_the_fine_count():
    """#433: a medium cut at or above the fine count is held below it, so one pair merges."""
    import numpy as np
    X = np.eye(5) * 10.0
    fine = np.arange(5)
    for k_medium in (5, 9):
        ids, med, _coarse = themes.nested_parents(X, fine, k_medium=k_medium, k_coarse=2)
        assert len(ids) == 5
        assert len(set(med.tolist())) == 4          # not 5: one pair of fine themes merged


def test_nested_parents_returns_two_arrays_for_one_fine_theme():
    """#433: a single fine theme hands back separate medium/coarse arrays, not one object twice."""
    import numpy as np
    ids, med, coarse = themes.nested_parents(np.eye(3), np.zeros(3, dtype=int),
                                             k_medium=2, k_coarse=2)
    assert ids == [0]
    assert med is not coarse
    assert med.tolist() == coarse.tolist() == [0]


def test_build_shadow_clamps_k_medium_against_the_realised_fine_count(rawstore_tmp_db, monkeypatch, caplog):
    """#433: NMF can leave a component empty, so the clamp reads the labels the fine fit produced,
    not the requested ``k_fine`` (here 3 asked, 2 realised)."""
    import numpy as np
    monkeypatch.setattr(themes, "_choose", lambda *a, **k: (3, 1.0))
    monkeypatch.setattr(themes, "nmf_assign",
                        lambda X, k, seed=0, fit=None: np.repeat([0, 1], X.shape[0] // 2))
    with caplog.at_level(logging.INFO):
        out = themes.build_shadow(candidates=(2, 3), k_medium=5, k_coarse=2,
                                  run_id="realised", profiles=_profiles())
    assert out["k_fine"] == 3
    assert out["k_medium"] == 1                      # the realised 2 fine themes - 1, not 3 - 1
    meds = {r["theme_id"] for r in rawstore.get_all("relatedness_themes")
            if r["level"] == "medium"}
    assert len(meds) == out["k_medium"]
    msgs = [r.getMessage() for r in caplog.records if "k_medium" in r.getMessage()]
    assert len(msgs) == 1
    assert "5" in msgs[0] and "realised 2 fine themes" in msgs[0]


def test_label_skips_numeric_junk():
    assert themes._label(
        ["1.6t", "gpu", "ai", "2.5d integration", "data center"], 3
    ) == "gpu, data center"


def test_adopt_medium_catalog(rawstore_tmp_db):
    """Target keeps fines/coarses; medium labels come from the source catalog."""
    src, tgt = "src-run", "tgt-run"
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{src}:m0", "level": "medium", "parent_id": f"{src}:c0",
         "label": "Semis", "size": 2},
        {"theme_id": f"{src}:m1", "level": "medium", "parent_id": f"{src}:c0",
         "label": "Banks", "size": 2},
        {"theme_id": f"{src}:c0", "level": "coarse", "parent_id": "",
         "label": "All", "size": 4},
        {"theme_id": f"{tgt}:f0", "level": "fine", "parent_id": f"{tgt}:m9",
         "label": "gpus", "size": 2},
        {"theme_id": f"{tgt}:f1", "level": "fine", "parent_id": f"{tgt}:m8",
         "label": "lenders", "size": 2},
        {"theme_id": f"{tgt}:m9", "level": "medium", "parent_id": f"{tgt}:c0",
         "label": "new-med-a", "size": 2},
        {"theme_id": f"{tgt}:m8", "level": "medium", "parent_id": f"{tgt}:c0",
         "label": "new-med-b", "size": 2},
        {"theme_id": f"{tgt}:m7", "level": "medium", "parent_id": f"{tgt}:c0",
         "label": "new-med-drop", "size": 0},
        {"theme_id": f"{tgt}:c0", "level": "coarse", "parent_id": "",
         "label": "Economy", "size": 4},
    ])
    rawstore.replace_all("relatedness_theme_members", [
        {"ticker": "NVDA", "theme_id": f"{tgt}:f0", "weight": 1.0},
        {"ticker": "AMD", "theme_id": f"{tgt}:f0", "weight": 1.0},
        {"ticker": "JPM", "theme_id": f"{tgt}:f1", "weight": 1.0},
        {"ticker": "BAC", "theme_id": f"{tgt}:f1", "weight": 1.0},
    ])
    rawstore.write_rows("ticker_themes", [
        {"ticker": "NVDA", "theme_id": f"{src}:m0", "theme": "Semis",
         "level": "medium", "theme_run_id": src},
        {"ticker": "AMD", "theme_id": f"{src}:m0", "theme": "Semis",
         "level": "medium", "theme_run_id": src},
        {"ticker": "JPM", "theme_id": f"{src}:m1", "theme": "Banks",
         "level": "medium", "theme_run_id": src},
        {"ticker": "BAC", "theme_id": f"{src}:m1", "theme": "Banks",
         "level": "medium", "theme_run_id": src},
    ])

    out = themes.adopt_medium_catalog(tgt, src)
    assert out["adopted"] >= 2 and out["fines_reparented"] == 2
    assert out["dropped_mediums"] >= 1

    rows = {r["theme_id"]: r for r in rawstore.get_all("relatedness_themes")
            if str(r["theme_id"]).startswith(f"{tgt}:")}
    med = [r for r in rows.values() if r["level"] == "medium"]
    fine = [r for r in rows.values() if r["level"] == "fine"]
    assert any(r["level"] == "coarse" for r in rows.values())
    assert {r["label"] for r in med} == {"Semis", "Banks"}
    med_ids = {r["theme_id"] for r in med}
    assert med_ids == {f"{tgt}:m0", f"{tgt}:m1"}
    for f in fine:
        assert f["parent_id"] in med_ids
    assert f"{tgt}:m7" not in rows and f"{tgt}:m8" not in rows and f"{tgt}:m9" not in rows
    assert f"{tgt}:c0" in rows


def test_match_theme_etfs(rawstore_tmp_db):
    """Store-only ETF overlap: SMH tops a semis fine; XLK is the sector ETF."""
    run = "etf-run"
    tid = f"{run}:f0"
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": tid, "level": "fine", "parent_id": f"{run}:m0",
         "label": "Semiconductors", "size": 4},
        {"theme_id": f"{run}:m0", "level": "medium", "parent_id": f"{run}:c0",
         "label": "Semis", "size": 4},
        {"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "",
         "label": "Tech", "size": 4},
    ])
    rawstore.replace_all("relatedness_theme_members", [
        {"ticker": t, "theme_id": tid, "weight": 1.0}
        for t in ("NVDA", "TSM", "AMD", "AVGO")
    ])
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": 1e11, "sector": "Technology"}
        for t in ("NVDA", "TSM", "AMD", "AVGO")
    ])
    rawstore.write_rows("etf_holdings", [
        {"ticker": "SMH", "entry": [
            {"ticker": "NVDA", "name": "n", "weight": 0.2},
            {"ticker": "TSM", "name": "t", "weight": 0.15},
            {"ticker": "AMD", "name": "a", "weight": 0.1},
            {"ticker": "AVGO", "name": "v", "weight": 0.1},
            {"ticker": "MU", "name": "m", "weight": 0.05},
        ]},
        {"ticker": "XLK", "entry": [
            {"ticker": "NVDA", "name": "n", "weight": 0.1},
            {"ticker": "AAPL", "name": "a", "weight": 0.2},
            {"ticker": "MSFT", "name": "m", "weight": 0.2},
        ]},
    ])

    out = themes.match_theme_etfs(run)
    assert out["themes"] >= 1 and out["rows"] >= 1
    rows = rawstore.query(
        "SELECT etf, rank, overlap FROM relatedness_theme_etfs "
        "WHERE theme_id = ? ORDER BY rank", [tid])
    etfs = {r["etf"]: r for r in rows}
    assert "SMH" in etfs
    assert etfs["SMH"]["rank"] <= 3
    assert "XLK" in etfs


# Each Morningstar sector name and the SPDR sector ETF that stands for it.
_SPDR_BY_MORNINGSTAR_SECTOR = {
    "Basic Materials": "XLB", "Communication Services": "XLC", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Energy": "XLE", "Financial Services": "XLF",
    "Healthcare": "XLV", "Industrials": "XLI", "Real Estate": "XLRE", "Technology": "XLK",
    "Utilities": "XLU",
}


def _seed_one_theme(run, sectors, spdr):
    """One fine theme over ``len(sectors)`` tickers; the ``spdr`` ETF holds only one of them.

    A single shared holding fails the ``>= 2`` overlap bar, so ``spdr`` can reach the
    theme only through the forced sector-SPDR rank 1.
    """
    tid = f"{run}:f0"
    tickers = [f"S{i}" for i in range(len(sectors))]
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": tid, "level": "fine", "parent_id": "", "label": "Fine", "size": len(tickers)}])
    rawstore.replace_all("relatedness_theme_members",
                         [{"ticker": t, "theme_id": tid, "weight": 1.0} for t in tickers])
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": 1e11, "sector": sec} for t, sec in zip(tickers, sectors)])
    rawstore.write_rows("etf_holdings", [
        {"ticker": spdr, "entry": [{"ticker": tickers[0], "name": "n", "weight": 0.1},
                                   {"ticker": "OUTSIDE", "name": "o", "weight": 0.9}]}])
    return tid


def _etf_ranks(tid):
    return {r["etf"]: r["rank"] for r in rawstore.query(
        "SELECT etf, rank FROM relatedness_theme_etfs WHERE theme_id = ?", [tid])}


def test_sector_spdr_is_keyed_by_the_morningstar_sector_names():
    from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS
    assert themes._SECTOR_SPDR == _SPDR_BY_MORNINGSTAR_SECTOR
    assert set(themes._SECTOR_SPDR) == set(MORNINGSTAR_SECTORS)


@pytest.mark.parametrize("sector, spdr", sorted(_SPDR_BY_MORNINGSTAR_SECTOR.items()))
def test_match_theme_etfs_ranks_the_spdr_of_the_morningstar_sector_first(
        rawstore_tmp_db, sector, spdr):
    tid = _seed_one_theme("spdr-run", [sector] * 3, spdr)
    themes.match_theme_etfs("spdr-run")
    assert _etf_ranks(tid) == {spdr: 1}


@pytest.mark.parametrize("sectors", [
    ["Information Technology"] * 3,
    ["Information Technology", "Information Technology", "Technology"],
], ids=["old-spelling", "old-and-new-spelling-pool"])
def test_match_theme_etfs_reads_a_stored_gics_sector_spelling(rawstore_tmp_db, sectors):
    """A store not yet re-applied still holds ``Information Technology``: it counts as ``Technology``."""
    tid = _seed_one_theme("old-run", sectors, "XLK")
    themes.match_theme_etfs("old-run")
    assert _etf_ranks(tid) == {"XLK": 1}


def test_match_theme_etfs_gives_an_unclassified_theme_no_sector_etf(rawstore_tmp_db):
    tid = _seed_one_theme("none-run", ["Unclassified"] * 3, "XLK")
    out = themes.match_theme_etfs("none-run")
    assert out == {"themes": 0, "rows": 0} and _etf_ranks(tid) == {}


def test_cutover_materialize_does_not_copy_legacy(rawstore_tmp_db):
    """Legacy-keyword themes are no longer copied into the run (#114 D1)."""
    rawstore.replace_all("equity_universe", [
        {"ticker": "G0", "market_cap": 1e9},
        {"ticker": "ZZZ", "market_cap": 1e9},
    ])
    rawstore.write_rows("ticker_themes", [
        {"ticker": "ZZZ", "theme_id": "kw-banks", "theme": "Banks",
         "level": "legacy", "theme_run_id": "legacy-keyword"},
    ])
    themes.build_shadow(candidates=(2, 3), k_medium=2, k_coarse=2,
                        run_id="leg", profiles=_profiles())
    mat = themes.cutover_materialize("leg")
    legacy = rawstore.query(
        "SELECT * FROM ticker_themes WHERE theme_run_id='leg' AND ticker='ZZZ'")
    assert legacy == []
    assert mat["profiled"] >= 1


def test_clean_theme_label_strips_junk_keeps_clean():
    assert themes.clean_theme_label(
        "Financial Services and Banking (a320ceo)"
    ) == "Financial Services and Banking"
    assert themes.clean_theme_label(
        "Oncology Biopharmaceuticals (abscopal effect)"
    ) == "Oncology Biopharmaceuticals"
    assert themes.clean_theme_label("Aerospace (A&D)") == "Aerospace"
    assert themes.clean_theme_label("Media (A+E)") == "Media"
    assert themes.clean_theme_label("Banks (Retail)") == "Banks (Retail)"
    assert themes.clean_theme_label("Clean Label") == "Clean Label"
    assert themes.clean_theme_label("") == ""


def test_adopt_medium_catalog_prefers_label_over_weak_jaccard(rawstore_tmp_db):
    """Equal ticker overlap: label Jaccard picks Semiconductor over Transportation."""
    src, tgt = "src-lab", "tgt-lab"
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{src}:m0", "level": "medium", "parent_id": f"{src}:c0",
         "label": "Semiconductor and Electronic Components", "size": 4},
        {"theme_id": f"{src}:m1", "level": "medium", "parent_id": f"{src}:c0",
         "label": "Transportation and Logistics", "size": 4},
        {"theme_id": f"{src}:c0", "level": "coarse", "parent_id": "",
         "label": "All", "size": 8},
        {"theme_id": f"{tgt}:f0", "level": "fine", "parent_id": f"{tgt}:m9",
         "label": "Semiconductor Embedded Computing", "size": 2},
        {"theme_id": f"{tgt}:m9", "level": "medium", "parent_id": f"{tgt}:c0",
         "label": "old", "size": 2},
        {"theme_id": f"{tgt}:c0", "level": "coarse", "parent_id": "",
         "label": "Economy", "size": 2},
    ])
    rawstore.replace_all("relatedness_theme_members", [
        {"ticker": "NVDA", "theme_id": f"{tgt}:f0", "weight": 1.0},
        {"ticker": "AMBA", "theme_id": f"{tgt}:f0", "weight": 1.0},
    ])
    # equal ticker overlap: NVDA in both source mediums
    rawstore.write_rows("ticker_themes", [
        {"ticker": "NVDA", "theme_id": f"{src}:m0", "theme": "Semi",
         "level": "medium", "theme_run_id": src},
        {"ticker": "TSM", "theme_id": f"{src}:m0", "theme": "Semi",
         "level": "medium", "theme_run_id": src},
        {"ticker": "NVDA", "theme_id": f"{src}:m1", "theme": "Trans",
         "level": "medium", "theme_run_id": src},
        {"ticker": "UPS", "theme_id": f"{src}:m1", "theme": "Trans",
         "level": "medium", "theme_run_id": src},
    ])
    themes.adopt_medium_catalog(tgt, src)
    f0 = rawstore.get("relatedness_themes", f"{tgt}:f0")
    assert f0["parent_id"] == f"{tgt}:m0"
    med = rawstore.get("relatedness_themes", f"{tgt}:m0")
    assert med["label"] == "Semiconductor and Electronic Components"


def test_adopt_medium_catalog_noop_when_source_has_no_members(rawstore_tmp_db):
    """Source mediums without ticker_themes must not wipe the target catalog."""
    src, tgt = "src-empty", "tgt-keep"
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{src}:m0", "level": "medium", "parent_id": "",
         "label": "Banks", "size": 0},
        {"theme_id": f"{tgt}:f0", "level": "fine", "parent_id": f"{tgt}:m0",
         "label": "Loans", "size": 2},
        {"theme_id": f"{tgt}:m0", "level": "medium", "parent_id": f"{tgt}:c0",
         "label": "Keep Me", "size": 2},
        {"theme_id": f"{tgt}:c0", "level": "coarse", "parent_id": "",
         "label": "All", "size": 2},
    ])
    rawstore.replace_all("relatedness_theme_members", [
        {"ticker": "G0", "theme_id": f"{tgt}:f0", "weight": 1.0},
    ])
    out = themes.adopt_medium_catalog(tgt, src)
    assert out["adopted"] == 0 and out["dropped_mediums"] == 0
    kept = rawstore.get("relatedness_themes", f"{tgt}:m0")
    assert kept and kept["label"] == "Keep Me"
    fine = rawstore.get("relatedness_themes", f"{tgt}:f0")
    assert fine["parent_id"] == f"{tgt}:m0"


def test_drop_empty_coarses(rawstore_tmp_db):
    run = "drop-run"
    rawstore.write_rows("theme_runs", [{
        "id": run, "created_at": "2020-01-01T00:00:00+00:00",
        "k_fine": 1, "k_medium": 1, "k_coarse": 3, "jaccard": 1.0, "is_current": 0,
    }])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{run}:f0", "level": "fine", "parent_id": f"{run}:m0",
         "label": "f", "size": 2},
        {"theme_id": f"{run}:m0", "level": "medium", "parent_id": f"{run}:c0",
         "label": "m", "size": 2},
        {"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "",
         "label": "kept", "size": 2},
        {"theme_id": f"{run}:c1", "level": "coarse", "parent_id": "",
         "label": "Discount Retail", "size": 0},
        {"theme_id": f"{run}:c2", "level": "coarse", "parent_id": "",
         "label": "Gaming", "size": 0},
    ])
    out = themes.drop_empty_coarses(run)
    assert out["dropped"] == 2
    rows = {r["theme_id"]: r for r in rawstore.get_all("relatedness_themes")
            if str(r["theme_id"]).startswith(f"{run}:")}
    assert f"{run}:c0" in rows
    assert f"{run}:c1" not in rows and f"{run}:c2" not in rows
    tr = rawstore.get("theme_runs", run)
    assert tr["k_coarse"] == 1


def _seed_two_pair_giant(run, sec_a, sec_b):
    """Four mediums, two high-overlap pairs under one giant coarse; pair A has ``sec_a``, pair B ``sec_b``."""
    # 4 mediums x 10 tickers; pair (m0,m1) share 8, pair (m2,m3) share 8
    themes_rows = [
        {"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "",
         "label": "Giant", "size": 40},
    ]
    members = []
    eu_by = {}
    for mi in range(4):
        mid = f"{run}:m{mi}"
        themes_rows.append({
            "theme_id": mid, "level": "medium", "parent_id": f"{run}:c0",
            "label": f"Med{mi}", "size": 10,
        })
        base = mi // 2 * 20  # pairs 0-1 share base 0; pairs 2-3 share base 20
        for j in range(10):
            # high within-pair overlap: first 8 tickers shared in pair
            if j < 8:
                t = f"T{base + j}"
            else:
                t = f"T{base + 8 + mi * 2 + (j - 8)}"
            members.append({"ticker": t, "theme_id": f"{run}:f{mi}", "weight": 1.0})
            eu_by[t] = {"ticker": t, "market_cap": 1e9, "sector": sec_a if mi < 2 else sec_b}
        themes_rows.append({
            "theme_id": f"{run}:f{mi}", "level": "fine", "parent_id": mid,
            "label": f"Fine{mi}", "size": 10,
        })
    rawstore.write_rows("relatedness_themes", themes_rows)
    rawstore.replace_all("relatedness_theme_members", members)
    rawstore.replace_all("equity_universe", list(eu_by.values()))
    rawstore.write_rows("theme_runs", [{
        "id": run, "created_at": "2020-01-01T00:00:00+00:00",
        "k_fine": 4, "k_medium": 4, "k_coarse": 1, "jaccard": 1.0, "is_current": 0,
    }])


def _coarse_labels(run):
    """The run's coarse labels, without the " (n)" suffix that only makes them unique."""
    return {re.sub(r" \(\d+\)$", "", r["label"]) for r in rawstore.get_all("relatedness_themes")
            if r["level"] == "coarse" and str(r["theme_id"]).startswith(f"{run}:")}


def test_rebalance_coarses_names_a_coarse_without_sector_values_after_its_largest_medium(
        rawstore_tmp_db):
    """No member has a sector value: the label falls back to the medium's, never ``Unclassified``."""
    _seed_two_pair_giant("reb-none", None, "")
    assert themes.rebalance_coarses("reb-none").get("rebalanced") is True
    assert _coarse_labels("reb-none") == {"Med0", "Med1", "Med2", "Med3"}


@pytest.mark.parametrize("sec_a, sec_b", [
    ("Technology", "Financial Services"),
    ("Information Technology", "Financials"),        # a store the master has not re-applied yet
], ids=["morningstar", "stored-gics-spelling"])
def test_rebalance_coarses_splits_giant(rawstore_tmp_db, sec_a, sec_b):
    """Four mediums, two high-overlap pairs under one giant coarse → split.

    The new coarses are named from their modal sector, always in the Morningstar spelling.
    """
    run = "reb-run"
    _seed_two_pair_giant(run, sec_a, sec_b)

    out = themes.rebalance_coarses(run)
    assert out.get("rebalanced") is True
    meds = [r for r in rawstore.get_all("relatedness_themes")
            if r["level"] == "medium" and str(r["theme_id"]).startswith(f"{run}:")]
    coarses = [r for r in rawstore.get_all("relatedness_themes")
               if r["level"] == "coarse" and str(r["theme_id"]).startswith(f"{run}:")]
    assert len(coarses) >= 2
    parents = {m["parent_id"] for m in meds}
    assert len(parents) >= 2
    # each coarse is named after the modal sector of its members
    assert _coarse_labels(run) == {"Technology", "Financial Services"}
    # no coarse holds > 0.45 of profiled fine members
    fine_tickers = set()
    for m in rawstore.get_all("relatedness_theme_members"):
        if str(m["theme_id"]).startswith(f"{run}:"):
            fine_tickers.add(m["ticker"])
    n = len(fine_tickers)
    tsets = themes._theme_ticker_sets(run)
    for c in coarses:
        assert len(tsets.get(c["theme_id"]) or ()) / n <= 0.45 + 1e-9
    assert out.get("k_coarse", 0) >= 2


def _seed_hub_giant(run, hub=6, core=20, uniq=10, outsiders=18, osize=2,
                    sector="Technology"):
    """One seeded giant coarse over >12 mediums: a tight hub + disjoint outsiders.

    The six hub mediums share ``core`` tickers plus a unique tail, so average-link
    agglomerative with ``k = min(12, n)`` swallows the whole hub into one coarse
    (~70% of profiled tickers). The 18 disjoint mediums never join it, so there
    are more mediums than ``k`` and the giant genuinely needs splitting.
    """
    rows, members, eu = [], [], {}
    total = hub * (core + uniq) + outsiders * osize
    rows.append({"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "",
                 "label": "Giant", "size": total})
    for i in range(hub):
        mid, fid = f"{run}:m{i}", f"{run}:f{i}"
        tickers = [f"C{j}" for j in range(core)] + [f"H{i}_{j}" for j in range(uniq)]
        rows.append({"theme_id": mid, "level": "medium", "parent_id": f"{run}:c0",
                     "label": f"Hub{i}", "size": len(tickers)})
        rows.append({"theme_id": fid, "level": "fine", "parent_id": mid,
                     "label": f"HubFine{i}", "size": len(tickers)})
        for t in tickers:
            members.append({"ticker": t, "theme_id": fid, "weight": 1.0})
            eu[t] = {"ticker": t, "market_cap": 1e9, "sector": sector}
    for i in range(outsiders):
        k = hub + i
        mid, fid = f"{run}:m{k}", f"{run}:f{k}"
        tickers = [f"O{i}_{j}" for j in range(osize)]
        rows.append({"theme_id": mid, "level": "medium", "parent_id": f"{run}:c0",
                     "label": f"Out{i}", "size": len(tickers)})
        rows.append({"theme_id": fid, "level": "fine", "parent_id": mid,
                     "label": f"OutFine{i}", "size": len(tickers)})
        for t in tickers:
            members.append({"ticker": t, "theme_id": fid, "weight": 1.0})
            eu[t] = {"ticker": t, "market_cap": 1e9, "sector": sector}
    rawstore.write_rows("relatedness_themes", rows)
    rawstore.replace_all("relatedness_theme_members", members)
    rawstore.replace_all("equity_universe", list(eu.values()))
    rawstore.write_rows("theme_runs", [{
        "id": run, "created_at": "2020-01-01T00:00:00+00:00",
        "k_fine": hub + outsiders, "k_medium": hub + outsiders, "k_coarse": 1,
        "jaccard": 1.0, "is_current": 0,
    }])


def test_rebalance_coarses_enforces_max_share_with_more_than_k(rawstore_tmp_db):
    """A giant surviving ``k=12`` must be split until every coarse <= max_share.

    The current one-shot ``k=min(12, n)`` leaves one coarse owning ~70% of the
    tickers; after rebalance no multi-medium coarse may exceed 45%.
    """
    run = "reb-big"
    _seed_hub_giant(run)
    profiled = {m["ticker"] for m in rawstore.get_all("relatedness_theme_members")
                if str(m["theme_id"]).startswith(f"{run}:")}
    n = len(profiled)

    out = themes.rebalance_coarses(run)

    assert out.get("rebalanced") is True
    assert out.get("k_coarse", 0) >= 2
    coarses = [r for r in rawstore.get_all("relatedness_themes")
               if r["level"] == "coarse"
               and str(r["theme_id"]).startswith(f"{run}:")]
    meds = [r for r in rawstore.get_all("relatedness_themes")
            if r["level"] == "medium"
            and str(r["theme_id"]).startswith(f"{run}:")]
    coarse_ids = {c["theme_id"] for c in coarses}
    assert out["k_coarse"] == len(coarses)

    tsets = themes._theme_ticker_sets(run)
    for c in coarses:
        children = [m for m in meds if m["parent_id"] == c["theme_id"]]
        share = len(tsets.get(c["theme_id"]) or ()) / n
        if len(children) > 1:                     # single-medium coarse may exceed
            assert share <= 0.45 + 1e-9
    assert out["max_share"] <= 0.45 + 1e-9

    # no orphaned mediums
    assert all(m["parent_id"] in coarse_ids for m in meds)

    # duplicate coarse labels (all "Technology") must be uniqued
    labels = [c["label"] for c in coarses]
    assert len(labels) == len(set(labels))
    assert any(lab.endswith(")") for lab in labels)


def test_unique_theme_labels(rawstore_tmp_db):
    run = "uniq-run"
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "",
         "label": "Banks", "size": 1},
        {"theme_id": f"{run}:c1", "level": "coarse", "parent_id": "",
         "label": "Banks", "size": 1},
        {"theme_id": f"{run}:c2", "level": "coarse", "parent_id": "",
         "label": "Banks", "size": 1},
        {"theme_id": f"{run}:m0", "level": "medium", "parent_id": f"{run}:c0",
         "label": "Banks", "size": 1},
    ])
    changed = themes.unique_theme_labels(run, "coarse")
    assert changed == 2
    labels = sorted(r["label"] for r in rawstore.get_all("relatedness_themes")
                    if str(r["theme_id"]).startswith(f"{run}:")
                    and r["level"] == "coarse")
    assert labels == ["Banks", "Banks (2)", "Banks (3)"]
    assert rawstore.get("relatedness_themes", f"{run}:m0")["label"] == "Banks"


def test_scrub_run_labels(rawstore_tmp_db):
    run = "scrub-run"
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": f"{run}:m0", "level": "medium", "parent_id": f"{run}:c0",
         "label": "Banks (a320ceo)", "size": 1},
        {"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "",
         "label": "Finance (Retail)", "size": 1},
    ])
    n = themes.scrub_run_labels(run)
    assert n >= 1
    m0 = rawstore.get("relatedness_themes", f"{run}:m0")
    assert m0["label"] == "Banks"
    c0 = rawstore.get("relatedness_themes", f"{run}:c0")
    assert c0["label"] == "Finance (Retail)"


def test_cli_cutover_optional_adopt(rawstore_tmp_db, monkeypatch, capsys):
    from RevenueChaser.module_relatedness import __main__ as cli

    order = []
    monkeypatch.setattr(themes, "current_run_id", lambda: "cur")
    monkeypatch.setattr(themes, "relabel_from_tags",
                        lambda rid, **k: order.append("relabel") or 0)
    monkeypatch.setattr(themes, "unique_theme_labels",
                        lambda rid, level="coarse": order.append("unique") or 0)
    monkeypatch.setattr(themes, "adopt_medium_catalog",
                        lambda t, s: order.append("adopt") or
                        {"adopted": 2, "fines_reparented": 4, "dropped_mediums": 1})
    monkeypatch.setattr(themes, "scrub_run_labels",
                        lambda rid: order.append("scrub") or 3)
    monkeypatch.setattr(themes, "drop_empty_coarses",
                        lambda rid: order.append("drop") or {"dropped": 1})
    monkeypatch.setattr(themes, "rebalance_coarses",
                        lambda rid: order.append("rebalance") or {"rebalanced": False})
    monkeypatch.setattr(themes, "match_theme_etfs",
                        lambda rid: order.append("etfs") or
                        {"themes": 5, "rows": 10})
    monkeypatch.setattr(themes, "cutover_materialize",
                        lambda rid: order.append("cut") or
                        {"run_id": rid, "rows": 9, "profiled": 3})

    assert cli.main(["--cutover", "--run-id", "tgt",
                     "--adopt-medium-from", "src"]) == 0
    assert order == ["relabel", "adopt", "scrub", "drop", "rebalance",
                     "unique", "unique", "unique", "etfs", "cut"]
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["run_id"] == "tgt" and out["rows"] == 9


# --- #118 A3 (C8 + C9 + C20): NMF robustness -----------------------------------

def _half(n_each):
    p = {}
    for i in range(n_each):
        p[f"G{i}"] = {"products": ["gpu", "ai"], "end_markets": ["data center"],
                      "business_model": ["semis"], "customer_types": ["hyperscaler"],
                      "value_chain_role": ["designer"], "keywords": ["accelerated"]}
        p[f"B{i}"] = {"products": ["loans", "deposits"], "end_markets": ["retail"],
                      "business_model": ["interest"], "customer_types": ["consumer"],
                      "value_chain_role": ["lender"], "keywords": ["branch"]}
    return p


def test_build_shadow_below_floor_returns_error_dict(rawstore_tmp_db):
    out = themes.build_shadow(run_id="tiny", profiles=_half(2) | {"X": {"products": ["a"]}})
    assert out["n"] == 5 and "error" in out
    assert rawstore.get("theme_runs", "tiny") is None      # nothing written


def test_build_shadow_default_candidates_on_a_small_universe(rawstore_tmp_db):
    """n = 50 with the default (60, 90, 120, 150) candidates used to IndexError."""
    out = themes.build_shadow(run_id="n50", profiles=_half(25))
    assert "error" not in out and out["n"] == 50
    assert 2 <= out["k_fine"] < 50
    assert rawstore.count("relatedness_theme_members") == 50


def test_build_shadow_small_n_does_not_raise(rawstore_tmp_db):
    out = themes.build_shadow(run_id="n12", profiles=_half(6), candidates=(2, 3))
    assert out["n"] == 12 and "error" not in out


def test_zero_rows_are_not_members(rawstore_tmp_db):
    profs = _half(30)
    profs["LONE1"] = {"products": ["xylophone repair"]}     # tag seen once: below min_df
    profs["LONE2"] = {"products": ["zeppelin tours"]}
    out = themes.build_shadow(run_id="zr", profiles=profs, candidates=(2, 3),
                              k_medium=2, k_coarse=2)
    members = {r["ticker"] for r in rawstore.get_all("relatedness_theme_members")}
    assert "LONE1" not in members and "LONE2" not in members
    assert out["n"] == 60 and out["unthemed"] == 2


def test_choose_k_follows_its_docstring(monkeypatch):
    """Smallest stable k wins; with none stable, the most stable one."""
    import numpy as np
    stab = {2: 0.95, 3: 0.99, 4: 0.99}
    sizes = {2: 25, 3: 40, 4: 40}
    monkeypatch.setattr(themes, "nmf_assign", lambda X, k, seed=0, fit=None: np.repeat(np.arange(k), 50 // k))
    monkeypatch.setattr(themes, "stability", lambda X, k, seeds=themes.SEEDS, fit=None: stab[k])
    monkeypatch.setattr(themes, "_min_size", lambda lab: sizes[len(set(lab.tolist()))])
    X = np.zeros((50, 10))
    assert themes.choose_k(X, [2, 3, 4], min_size=20, floor=0.9) == 2
    stab.update({2: 0.50, 3: 0.60, 4: 0.60})
    assert themes.choose_k(X, [2, 3, 4], min_size=20, floor=0.9) == 3


def test_seeds_is_five():
    assert themes.SEEDS == 5


def test_stability_is_computed_once_per_candidate(rawstore_tmp_db, monkeypatch):
    calls = []
    real = themes.stability
    monkeypatch.setattr(themes, "stability",
                        lambda X, k, seeds=themes.SEEDS, fit=themes.DEFAULT_FIT: calls.append(k) or real(X, k, 2, fit))
    themes.build_shadow(run_id="once", profiles=_half(30), candidates=(2, 3),
                        k_medium=2, k_coarse=2)
    assert sorted(calls) == [2, 3]                          # nothing recomputed for the winner


# --- #118 A6 (C21 + C24 in-module): one reader of a run's rows ---------------------

def _two_runs():
    """``r1`` and ``r10``: a run id that is a string prefix of another."""
    rows = []
    for run in ("r1", "r10"):
        rows += [
            {"theme_id": f"{run}:c0", "level": "coarse", "parent_id": "",
             "label": "Banks", "size": 1},
            {"theme_id": f"{run}:c1", "level": "coarse", "parent_id": "",
             "label": "Banks", "size": 1},
            {"theme_id": f"{run}:m0", "level": "medium", "parent_id": f"{run}:c0",
             "label": "Regional", "size": 1},
        ]
    rawstore.write_rows("relatedness_themes", rows)
    rawstore.write_rows("relatedness_theme_tags", [
        {"theme_id": "r1:f0", "tag": "loans", "rank": 0},
        {"theme_id": "r10:f0", "tag": "gpu", "rank": 0}])
    rawstore.write_rows("relatedness_theme_members", [
        {"ticker": "AAA", "theme_id": "r1:f0", "weight": 1.0},
        {"ticker": "BBB", "theme_id": "r10:f0", "weight": 1.0}])


def test_run_rows_sees_only_its_own_run(rawstore_tmp_db):
    _two_runs()
    assert {r["theme_id"] for r in themes.run_rows("relatedness_themes", "r1")} == {
        "r1:c0", "r1:c1", "r1:m0"}
    assert [r["theme_id"] for r in themes.run_rows("relatedness_themes", "r10", "medium")] == [
        "r10:m0"]
    assert [r["tag"] for r in themes.run_rows("relatedness_theme_tags", "r1")] == ["loans"]
    assert [r["ticker"] for r in themes.run_rows("relatedness_theme_members", "r10")] == ["BBB"]


def test_label_writers_stay_inside_their_run(rawstore_tmp_db):
    _two_runs()
    assert themes.unique_theme_labels("r1", "coarse") == 1
    labels = {r["theme_id"]: r["label"] for r in rawstore.get_all("relatedness_themes")}
    assert labels["r1:c1"] == "Banks (2)" and labels["r10:c1"] == "Banks"   # r10 untouched
    from RevenueChaser.module_relatedness import naming
    assert set(naming._run_themes("r1")) == {"r1:c0", "r1:c1", "r1:m0"}
    assert naming._fine_tags("r1") == {"r1:f0": ["loans"]}
    assert naming._fine_members("r1") == {"r1:f0": ["AAA"]}


# --- #118 A3 review: k is bounded by the vocabulary too; adaptive min_df -----------

def _vocab_profiles(n, vocab, per=3, seed=0):
    import random
    rng = random.Random(seed)
    tags = [f"tag{j}" for j in range(vocab)]
    return {f"T{i}": {"products": rng.sample(tags, per)} for i in range(n)}


def test_k_is_bounded_by_the_vocabulary(rawstore_tmp_db):
    """200 names over 40 tags: nndsvd raises when k > n_features (default k is 60+)."""
    out = themes.build_shadow(run_id="vocab", profiles=_vocab_profiles(200, 40))
    assert "error" not in out and out["n"] == 200
    assert 2 <= out["k_fine"] < 40


def test_choose_k_clamps_to_features():
    import numpy as np
    X = np.abs(np.random.RandomState(0).rand(500, 8))
    assert themes.choose_k(X, [60, 90, 120, 150]) < 8       # fallback also fits the vocabulary
    assert themes.choose_k(np.abs(np.random.RandomState(1).rand(50, 8)), [60]) < 8


def test_min_df_adapts_to_a_small_universe():
    profs = {f"S{i}": {"products": ["common", "rare" if i < 2 else f"x{i}"]} for i in range(12)}
    _X, terms, _t = themes.tag_matrix(profs)
    assert "rare" in terms                                    # in 2 docs: kept at min_df=1
    big = {f"S{i}": {"products": [f"t{i % 40}", "rare" if i < 2 else f"x{i}"]}
           for i in range(100)}
    _X, terms, _t = themes.tag_matrix(big)
    assert "rare" not in terms                                # 100 names: min_df is 3 again


def test_fewer_than_three_distinct_tags_is_an_error_dict(rawstore_tmp_db):
    """One shared tag plus tags below min_df leaves a 1-column matrix: nndsvd would raise."""
    profs = {f"S{i}": {"products": ["shared"]} for i in range(30)}
    profs.update({f"U{i}": {"products": [f"unique{i}"]} for i in range(30)})
    out = themes.build_shadow(run_id="onetag", profiles=profs)
    assert "error" in out and rawstore.get("theme_runs", "onetag") is None
