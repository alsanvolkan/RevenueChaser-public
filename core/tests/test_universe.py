"""Shared classification universe — membership + filter-field join."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore, universe
from RevenueChaser.core.identity import types as id_types


def _seed_universe(rows):
    rawstore.replace_all("equity_universe", [{
        "ticker": r["ticker"], "cik": "", "name": r.get("name") or r["ticker"],
        "market_cap": r.get("market_cap", 1.0e9), "price": 10.0,
        "sector": r.get("sector") or "Technology", "industry": r.get("industry", "x"),
        "quote": {"symbol": r["ticker"]},
    } for r in rows])


def test_classification_universe_has_no_market_cap_floor(rawstore_tmp_db):
    """avo, 29 Oct 2026: the tier is the whole limit, so the default keeps every cap."""
    _seed_universe([
        {"ticker": "BIG", "market_cap": 400e6},
        {"ticker": "TINY", "market_cap": 50e6},
    ])
    assert universe.DEFAULT_MC_MIN == 0
    assert universe.classification_universe() == ["BIG", "TINY"]


def test_a_caller_can_still_ask_for_a_floor_of_its_own(rawstore_tmp_db):
    """The parameter stays for --floor and the Co-movement setting, it is only no longer the default."""
    _seed_universe([
        {"ticker": "BIG", "market_cap": 400e6},
        {"ticker": "TINY", "market_cap": 50e6},
    ])
    assert universe.classification_universe(mc_min=300_000_000) == ["BIG"]


def test_filter_fields_joins_name_gics_and_index(rawstore_tmp_db):
    _seed_universe([{"ticker": "NVDA", "name": "NVIDIA Corporation",
                     "market_cap": 1e12, "sector": "Technology", "industry": "Semiconductors"}])
    rawstore.replace_all("index_constituents", [
        {"slug": "sp500", "ticker": "NVDA"},
    ])
    meta = universe.filter_fields(["NVDA", "MISSING"])
    assert meta["NVDA"]["name"] == "NVIDIA Corporation"
    assert meta["NVDA"]["market_cap"] == 1e12
    assert meta["NVDA"]["sector"] == "Technology"
    assert "S&P 500" in (meta["NVDA"]["index"] or [])
    assert meta["MISSING"]["ticker"] == "MISSING"
    assert meta["MISSING"]["name"] == "MISSING"


# ── spine reader contract (plan SC1 / filters S1) ───────────────────────────
def _seed_theme_run(run="r1"):
    rawstore.write_rows("theme_runs", [{"id": run, "created_at": "x", "k_fine": 2,
                                        "k_medium": 2, "k_coarse": 2,
                                        "jaccard": 1.0, "is_current": 1}])
    p = f"{run}:"
    rawstore.write_rows("relatedness_themes", [
        # coarse named after a GICS sector with a numeric suffix (today's harm)
        {"theme_id": p + "c1", "level": "coarse", "parent_id": "", "label": "Financials (2)", "size": 3},
        {"theme_id": p + "c2", "level": "coarse", "parent_id": "", "label": "Gold Miners", "size": 1},
        {"theme_id": p + "m1", "level": "medium", "parent_id": p + "c1", "label": "Regional Lending", "size": 2},
        {"theme_id": p + "m2", "level": "medium", "parent_id": p + "c1", "label": "Card Networks", "size": 1},
        {"theme_id": p + "m3", "level": "medium", "parent_id": p + "c2", "label": "Precious Metals", "size": 1},
        {"theme_id": p + "f1", "level": "fine", "parent_id": p + "m1", "label": "Community Banks", "size": 2},
        {"theme_id": p + "f2", "level": "fine", "parent_id": p + "m2", "label": "Payments", "size": 1},
        {"theme_id": p + "f3", "level": "fine", "parent_id": p + "m3", "label": "Gold (2)", "size": 1},
    ])
    rawstore.write_rows("relatedness_theme_members", [
        {"ticker": "FITB", "theme_id": p + "f1", "weight": 1.0},
        {"ticker": "HBAN", "theme_id": p + "f1", "weight": 1.0},
        {"ticker": "V", "theme_id": p + "f2", "weight": 1.0},
        {"ticker": "AEM", "theme_id": p + "f3", "weight": 1.0},
        # a member of an older run must not leak in
        {"ticker": "OLD", "theme_id": "r0:f9", "weight": 1.0},
    ])


def test_filter_fields_carries_the_two_levels(rawstore_tmp_db):
    _seed_universe([{"ticker": "AEM", "sector": "Basic Materials", "industry": "Gold"}])
    row = universe.filter_fields(["AEM"])["AEM"]
    assert row["sector"] == "Basic Materials"
    assert row["industry"] == "Gold"
    assert row["gics"] == ["Basic Materials", "Gold"]
    assert "industry_group" not in row and "sub_industry" not in row


def test_filter_fields_normalises_an_old_gics_sector_to_the_morningstar_name(rawstore_tmp_db):
    _seed_universe([{"ticker": "AEM", "sector": "Materials", "industry": "Gold"},
                    {"ticker": "FITB", "sector": "Financials", "industry": "Banks - Regional"}])
    rows = universe.filter_fields(["AEM", "FITB"])
    assert rows["AEM"]["gics"] == ["Basic Materials", "Gold"] and rows["AEM"]["sector"] == "Basic Materials"
    assert rows["FITB"]["gics"] == ["Financial Services", "Banks - Regional"]


def test_filter_fields_gives_a_ticker_with_no_industry_a_sector_only_path(rawstore_tmp_db):
    _seed_universe([{"ticker": "NOIND", "sector": "Energy", "industry": ""}])
    row = universe.filter_fields(["NOIND"])["NOIND"]
    assert row["industry"] == "" and row["gics"] == ["Energy", ""]


def test_filter_fields_trims_the_stored_industry_text(rawstore_tmp_db):
    _seed_universe([{"ticker": "XOM", "sector": "Energy", "industry": "  Oil & Gas E&P "}])
    row = universe.filter_fields(["XOM"])["XOM"]
    assert row["industry"] == "Oil & Gas E&P" and row["gics"] == ["Energy", "Oil & Gas E&P"]


def test_theme_catalog_is_the_pinned_run_only_with_display_fixes(rawstore_tmp_db):
    _seed_theme_run()
    cat = universe.theme_catalog()
    assert set(cat) == {f"r1:{x}" for x in ("c1", "c2", "m1", "m2", "m3", "f1", "f2", "f3")}
    # A GICS-sector coarse label is replaced by its largest medium child's label.
    assert cat["r1:c1"]["label"] == "Regional Lending"
    assert cat["r1:c1"]["level"] == "coarse" and cat["r1:c1"]["parent"] == ""
    assert cat["r1:c2"]["label"] == "Gold Miners"
    # " (n)" is dropped when the bare label is unique.
    assert cat["r1:f3"]["label"] == "Gold"
    assert cat["r1:f1"]["parent"] == "r1:m1"


def test_a_coarse_theme_named_like_a_morningstar_sector_reads_as_its_largest_medium_child(rawstore_tmp_db):
    rawstore.write_rows("theme_runs", [{"id": "r2", "created_at": "x", "k_fine": 1, "k_medium": 2, "k_coarse": 2,
                                        "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": "r2:c1", "level": "coarse", "parent_id": "", "label": "Healthcare (3)", "size": 3},
        {"theme_id": "r2:c2", "level": "coarse", "parent_id": "", "label": "Consumer Cyclical", "size": 3},
        {"theme_id": "r2:c3", "level": "coarse", "parent_id": "", "label": "Health Care (4)", "size": 3},
        {"theme_id": "r2:m1", "level": "medium", "parent_id": "r2:c1", "label": "Biotech Tools", "size": 2},
        {"theme_id": "r2:m2", "level": "medium", "parent_id": "r2:c1", "label": "Devices", "size": 1},
        {"theme_id": "r2:m3", "level": "medium", "parent_id": "r2:c2", "label": "Autos", "size": 2},
        {"theme_id": "r2:m4", "level": "medium", "parent_id": "r2:c3", "label": "Hospitals", "size": 2},
    ])
    cat = universe.theme_catalog()
    assert cat["r2:c1"]["label"] == "Biotech Tools"
    assert cat["r2:c2"]["label"] == "Autos"
    assert cat["r2:c3"]["label"] == "Hospitals"          # a run built before the rename: the GICS spelling too


@pytest.mark.parametrize("old_name", ["Information Technology", "Consumer Discretionary", "Consumer Staples",
                                      "Financials", "Health Care", "Materials"])
def test_every_old_gics_sector_spelling_still_reads_as_a_sector_name_in_a_stored_theme_run(rawstore_tmp_db, old_name):
    rawstore.write_rows("theme_runs", [{"id": "r3", "created_at": "x", "k_fine": 1, "k_medium": 1, "k_coarse": 1,
                                        "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": "r3:c1", "level": "coarse", "parent_id": "", "label": old_name, "size": 2},
        {"theme_id": "r3:m1", "level": "medium", "parent_id": "r3:c1", "label": "Child", "size": 2}])
    assert universe.theme_catalog()["r3:c1"]["label"] == "Child"


def test_a_sector_coarse_run_keeps_its_sector_names(rawstore_tmp_db):
    """#108 card "Sektörden coarse": a run whose coarse level IS the sectors (``<run>:sec-<slug>`` ids) shows them
    as they are; only an NMF coarse that happens to carry a sector name reads as its largest medium child."""
    rawstore.write_rows("theme_runs", [{"id": "r4", "created_at": "x", "k_fine": 1, "k_medium": 3, "k_coarse": 3,
                                        "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": "r4:sec-technology", "level": "coarse", "parent_id": "", "label": "Technology", "size": 3},
        {"theme_id": "r4:sec-unclassified", "level": "coarse", "parent_id": "", "label": "Unclassified", "size": 1},
        {"theme_id": "r4:c9", "level": "coarse", "parent_id": "", "label": "Energy", "size": 2},
        {"theme_id": "r4:m1", "level": "medium", "parent_id": "r4:sec-technology", "label": "Semis", "size": 3},
        {"theme_id": "r4:m2", "level": "medium", "parent_id": "r4:sec-unclassified", "label": "Shells", "size": 1},
        {"theme_id": "r4:m3", "level": "medium", "parent_id": "r4:c9", "label": "Drillers", "size": 2},
    ])
    cat = universe.theme_catalog()
    assert cat["r4:sec-technology"]["label"] == "Technology"
    assert cat["r4:sec-unclassified"]["label"] == "Unclassified"
    assert cat["r4:c9"]["label"] == "Drillers"


def test_filter_fields_carries_theme_ids_for_every_level(rawstore_tmp_db):
    _seed_universe([{"ticker": t} for t in ("FITB", "AEM", "NOPROF")])
    _seed_theme_run()
    rows = universe.filter_fields(["FITB", "AEM", "NOPROF"])
    assert sorted(rows["FITB"]["theme_ids"]) == ["r1:c1", "r1:f1", "r1:m1"]
    assert sorted(rows["AEM"]["theme_ids"]) == ["r1:c2", "r1:f3", "r1:m3"]
    assert rows["NOPROF"]["theme_ids"] == []          # explicit "Unthemed"


def test_no_pinned_run_means_no_themes_not_a_crash(rawstore_tmp_db):
    _seed_universe([{"ticker": "AEM"}])
    assert universe.theme_catalog() == {}
    assert universe.filter_fields(["AEM"])["AEM"]["theme_ids"] == []


def test_universe_counts_are_all_caps_and_the_classified_slice(rawstore_tmp_db):
    """With no floor (the default) nothing is below it.

    A NULL cap is still counted apart and, on this store path, still left out: the query is
    ``market_cap >= ?`` and NULL compares to nothing. The spine path keeps it (``COALESCE(..., 0) >= 0``),
    which is the live behaviour since PR 6.
    """
    _seed_universe([{"ticker": "BIG", "market_cap": 4e8},
                    {"ticker": "TINY", "market_cap": 5e7},
                    {"ticker": "NOCAP", "market_cap": None}])
    c = universe.universe_counts()
    assert c == {"scanned_total": 3, "classification_total": 2, "floor_total": 2,
                 "funds_etfs": 0, "spacs": 0, "other_securities": 0, "unknown_type": 2,
                 "below_floor": 0, "no_market_cap": 1,
                 "floor": 0}


def test_universe_counts_still_split_by_an_explicit_floor(rawstore_tmp_db):
    """The funnel machinery stays for the callers that pass a floor (--floor, the Co-movement setting)."""
    _seed_universe([{"ticker": "BIG", "market_cap": 4e8},
                    {"ticker": "TINY", "market_cap": 5e7},
                    {"ticker": "NOCAP", "market_cap": None}])
    c = universe.universe_counts(mc_min=300_000_000)
    assert c["below_floor"] == 1 and c["no_market_cap"] == 1
    assert c["floor_total"] == 1 and c["classification_total"] == 1
    assert c["floor"] == 300_000_000


# ── R3 (#86): an unknown GICS value reads Unclassified (never the legacy "Other") ─────────────
def test_filter_fields_reads_an_unknown_sector_as_unclassified(rawstore_tmp_db):
    rawstore.replace_all("equity_universe", [
        {"ticker": "LEG", "name": "Legacy", "market_cap": 1e9, "sector": "Other", "industry": "Other",
         "quote": {}},                                                                  # vendor fallback
        {"ticker": "MAS", "name": "Master", "market_cap": 1e9, "sector": "Unclassified",
         "industry": "Unclassified", "quote": {}},
        {"ticker": "NUL", "name": "Blank", "market_cap": 1e9, "sector": None, "industry": None, "quote": {}},
        {"ticker": "PART", "name": "Part", "market_cap": 1e9, "sector": "Financial Services",
         "industry": "Unclassified", "quote": {}},
    ])
    rows = universe.filter_fields(["LEG", "MAS", "NUL", "PART", "MISSING"])
    for t in ("LEG", "MAS", "NUL", "MISSING"):
        assert rows[t]["sector"] == "Unclassified", t
        assert rows[t]["gics"][0] == "Unclassified", t
        assert "Other" not in rows[t]["gics"], t
    assert rows["MAS"]["gics"] == ["Unclassified"] * 2
    assert rows["LEG"]["gics"] == ["Unclassified"] * 2           # the legacy ``Other`` industry too
    assert rows["NUL"]["gics"] == ["Unclassified", ""]
    assert rows["PART"]["sector"] == "Financial Services"
    assert rows["PART"]["gics"] == ["Financial Services", "Unclassified"]


def test_a_coarse_theme_named_like_the_unknown_bucket_is_replaced_by_its_child(rawstore_tmp_db):
    rawstore.write_rows("theme_runs", [{"id": "r1", "created_at": "x", "k_fine": 1, "k_medium": 1,
                                        "k_coarse": 1, "jaccard": 1.0, "is_current": 1}])
    rawstore.write_rows("relatedness_themes", [
        {"theme_id": "r1:c1", "level": "coarse", "parent_id": "", "label": "Unclassified (3)", "size": 3},
        {"theme_id": "r1:c2", "level": "coarse", "parent_id": "", "label": "Other", "size": 3},
        {"theme_id": "r1:m1", "level": "medium", "parent_id": "r1:c1", "label": "Shell Companies", "size": 3},
        {"theme_id": "r1:m2", "level": "medium", "parent_id": "r1:c2", "label": "Misc Holdings", "size": 3},
    ])
    cat = universe.theme_catalog()
    assert cat["r1:c1"]["label"] == "Shell Companies"
    assert cat["r1:c2"]["label"] == "Misc Holdings"


# ── security-type filter (#87, Identity R4) ─────────────────────────────────
def _seed_master(types: dict):
    """``ticker -> security_type`` rows in ``security_master`` (None = NULL type)."""
    rawstore.ensure_table("security_master")
    rawstore.write_rows("security_master", [
        {"ticker": t, "security_type": ty} for t, ty in types.items()])


def test_type_groups_are_disjoint_and_cover_exactly_the_excluded_types():
    keys = [k for k, _label, _types in universe.TYPE_GROUPS]
    assert len(keys) == len(set(keys))
    union: set = set()
    for _k, _label, group in universe.TYPE_GROUPS:
        assert not (union & group), "a type may be in only one funnel row"
        union |= group
    assert union == id_types.EXCLUDED_TYPES      # a new excluded type needs a funnel row


def test_classification_universe_drops_excluded_types_at_any_cap(rawstore_tmp_db):
    _seed_universe([
        {"ticker": "CEFBIG", "market_cap": 9e9}, {"ticker": "ETFX", "market_cap": 8e9},
        {"ticker": "SPACX", "market_cap": 7e9}, {"ticker": "PREF", "market_cap": 6e9},
        {"ticker": "WARR", "market_cap": 5e9}, {"ticker": "UNIT", "market_cap": 4.5e9},
        {"ticker": "AAA", "market_cap": 4e9}, {"ticker": "BBB", "market_cap": 3e9},
        {"ticker": "CEFLOW", "market_cap": 1e6}])
    _seed_master({"CEFBIG": "cef", "ETFX": "etp", "SPACX": "spac", "PREF": "preferred",
                  "WARR": "warrant", "UNIT": "unit", "AAA": "common", "BBB": "adr",
                  "CEFLOW": "cef"})
    assert universe.classification_universe() == ["AAA", "BBB"]
    assert universe.classification_universe(mc_min=0) == ["AAA", "BBB"]
    assert universe.excluded_tickers() == {
        "CEFBIG", "ETFX", "SPACX", "PREF", "WARR", "UNIT", "CEFLOW"}


def test_classification_universe_rows_carries_the_columns_the_peer_ladder_needs(rawstore_tmp_db):
    """FIN-3: comps peers are picked inside the spine, so the reader that feeds them carries sector and industry.

    The rows are the same slice as the ticker list (same floor if one is asked for, same excluded types, same cap-descending order);
    ``sub_industry`` is not read — the identity master writes it NULL.
    """
    _seed_universe([
        {"ticker": "BIG", "market_cap": 4e9, "sector": "Energy", "industry": "Oil & Gas E&P"},
        {"ticker": "ETFX", "market_cap": 3e9, "sector": "Energy", "industry": "Oil & Gas E&P"},
        {"ticker": "TINY", "market_cap": 50e6, "sector": "Energy", "industry": "Oil & Gas E&P"}])
    _seed_master({"BIG": "common", "ETFX": "etp"})
    assert universe.classification_universe_rows() == [
        {"ticker": "BIG", "market_cap": 4e9, "sector": "Energy", "industry": "Oil & Gas E&P"},
        {"ticker": "TINY", "market_cap": 50e6, "sector": "Energy", "industry": "Oil & Gas E&P"}]
    for mc_min in (universe.DEFAULT_MC_MIN, 0, 300_000_000):
        assert ([r["ticker"] for r in universe.classification_universe_rows(mc_min)]
                == universe.classification_universe(mc_min))


def test_every_operating_type_stays(rawstore_tmp_db):
    ops = sorted(id_types.OPERATING_TYPES)
    _seed_universe([{"ticker": f"T{i}", "market_cap": 1e9 - i} for i in range(len(ops))])
    _seed_master({f"T{i}": ty for i, ty in enumerate(ops)})
    assert universe.classification_universe() == [f"T{i}" for i in range(len(ops))]


def test_an_unknown_missing_or_null_type_stays_and_order_is_cap_descending(rawstore_tmp_db):
    _seed_universe([
        {"ticker": "NOROW", "market_cap": 5e9},     # no security_master row
        {"ticker": "NULLT", "market_cap": 4e9},     # NULL type
        {"ticker": "UNK", "market_cap": 3e9},       # 'unknown'
        {"ticker": "OTH", "market_cap": 2e9},       # 'other'
        {"ticker": "ODD", "market_cap": 1e9},       # a type this code does not know
        {"ticker": "FUND", "market_cap": 6e9}])
    _seed_master({"NULLT": None, "UNK": "unknown", "OTH": "other", "ODD": "martian",
                  "FUND": "cef"})
    assert universe.classification_universe() == ["NOROW", "NULLT", "UNK", "OTH", "ODD"]


def test_a_missing_security_master_table_leaves_the_universe_unfiltered(tmp_path):
    """``rawstore.query`` answers [] for a missing table: a JOIN would empty the universe."""
    _seed_universe([{"ticker": "AAA", "market_cap": 2e9}, {"ticker": "BBB", "market_cap": 1e9}])
    assert rawstore.query("SELECT count(*) n FROM information_schema.tables "
                          "WHERE table_name = 'security_master'")[0]["n"] == 0
    assert universe._security_types() == {}
    assert universe.classification_universe() == ["AAA", "BBB"]
    assert universe.excluded_tickers() == set()
    c = universe.universe_counts()
    assert c["classification_total"] == c["floor_total"] == 2
    assert c["funds_etfs"] == c["spacs"] == c["other_securities"] == 0
    assert c["unknown_type"] == 2             # nothing is typed, nothing is hidden


def test_an_empty_security_master_table_leaves_the_universe_unfiltered(rawstore_tmp_db):
    _seed_universe([{"ticker": "AAA", "market_cap": 2e9}])
    assert universe.classification_universe() == ["AAA"]


def _mixed_universe():
    _seed_universe([
        {"ticker": "OPS1", "market_cap": 9e9}, {"ticker": "OPS2", "market_cap": 8e9},
        {"ticker": "FUND1", "market_cap": 7e9}, {"ticker": "FUND2", "market_cap": 6e9},
        {"ticker": "SPAC1", "market_cap": 5e9},
        {"ticker": "PREF1", "market_cap": 4e9}, {"ticker": "WARR1", "market_cap": 3.5e9},
        {"ticker": "UNIT1", "market_cap": 3.2e9},
        {"ticker": "NOROW", "market_cap": 3.1e9}, {"ticker": "UNK1", "market_cap": 3.0e9},
        # below the floor: every type group here must NOT be counted
        {"ticker": "TINYFUND", "market_cap": 1e6}, {"ticker": "TINYSPAC", "market_cap": 2e6},
        {"ticker": "TINYOPS", "market_cap": 3e6},
        {"ticker": "NOCAPFUND", "market_cap": None}])
    _seed_master({"OPS1": "common", "OPS2": "adr", "FUND1": "cef", "FUND2": "etp",
                  "SPAC1": "spac", "PREF1": "preferred", "WARR1": "warrant",
                  "UNIT1": "unit", "UNK1": "unknown",
                  "TINYFUND": "cef", "TINYSPAC": "spac", "TINYOPS": "common",
                  "NOCAPFUND": "cef"})


def test_universe_counts_split_the_floor_slice_by_type_group(rawstore_tmp_db):
    """Asked with an explicit floor: the three tiny names and the NULL cap stay out of the slice."""
    _mixed_universe()
    c = universe.universe_counts(mc_min=300_000_000)
    assert c["scanned_total"] == 14
    assert c["below_floor"] == 3 and c["no_market_cap"] == 1
    assert c["floor_total"] == 10
    assert (c["funds_etfs"], c["spacs"], c["other_securities"]) == (2, 1, 3)
    assert c["classification_total"] == 4
    assert c["unknown_type"] == 2               # NOROW + UNK1 (kept, not an operating type)
    assert c["floor"] == 300_000_000


def test_universe_counts_invariants_and_agreement_with_the_universe(rawstore_tmp_db):
    """The funnel must add up, and its last line is exactly the universe boards read."""
    _mixed_universe()
    c = universe.universe_counts()
    assert c["scanned_total"] == c["below_floor"] + c["no_market_cap"] + c["floor_total"]
    assert c["floor_total"] == (c["funds_etfs"] + c["spacs"] + c["other_securities"]
                                + c["classification_total"])
    assert c["classification_total"] == len(universe.classification_universe())


def test_universe_counts_follow_a_custom_floor(rawstore_tmp_db):
    _mixed_universe()
    c = universe.universe_counts(mc_min=1e6)
    assert c["floor_total"] == 13                # every capped name
    assert (c["funds_etfs"], c["spacs"]) == (3, 2)
    assert c["classification_total"] == len(universe.classification_universe(1e6))


# ── R5 (#88): screeners read the spine GICS ─────────────────────────────────
def test_spine_gics_reads_the_universe_and_marks_unknown_names_unclassified(rawstore_tmp_db):
    _seed_universe([
        {"ticker": "NVDA", "sector": "Technology", "industry": "Semiconductors"},
        {"ticker": "OLD", "sector": "Other", "industry": ""},   # legacy unknown spelling
        {"ticker": "GAP", "sector": "Energy", "industry": "Other"},
    ])
    g = universe.spine_gics()
    assert g["NVDA"].sector == "Technology"
    assert g["NVDA"].industry == "Semiconductors"
    assert (g["OLD"].sector, g["OLD"].industry) == ("Unclassified", "Unclassified")
    assert (g["GAP"].sector, g["GAP"].industry) == ("Energy", "Unclassified")
    # a ticker without a row is Unclassified at both levels, never a vendor guess
    miss = universe.gics_of("MISSING", g)
    assert (miss.sector, miss.industry) == ("Unclassified",) * 2
    assert universe.gics_of("nvda", g).sector == "Technology"


def test_spine_gics_normalises_an_old_gics_sector_spelling(rawstore_tmp_db):
    _seed_universe([{"ticker": "NVDA", "sector": "Information Technology", "industry": "Semiconductors"},
                    {"ticker": "JNJ", "sector": "Health Care", "industry": "Drug Manufacturers - General"}])
    g = universe.spine_gics()
    assert (g["NVDA"].sector, g["JNJ"].sector) == ("Technology", "Healthcare")


def test_spine_gics_for_named_tickers_filters_in_sql(rawstore_tmp_db, monkeypatch):
    """A per-ticker lookup (Earnings Radar, Financials) must not scan and ship the whole universe."""
    _seed_universe([
        {"ticker": "NVDA", "sector": "Technology"},
        {"ticker": "XOM", "sector": "Energy"},
    ])
    seen = []
    real = universe.rawstore.query

    def spy(sql, params=None, **kw):
        rows = real(sql, params, **kw)
        seen.append(len(rows))
        return rows
    monkeypatch.setattr(universe.rawstore, "query", spy)
    g = universe.spine_gics(["nvda"])
    assert set(g) == {"NVDA"}
    assert seen == [1]                                   # only the named row left the store
    assert universe.spine_gics([]) == {}


def test_spine_gics_without_a_universe_table_is_empty(tmp_path):
    assert universe.spine_gics() == {}
    assert universe.gics_of("AAA", {}).sector == "Unclassified"


def test_load_cached_universe_returns_normalised_rows(rawstore_tmp_db):
    _seed_universe([{"ticker": "AAA", "market_cap": 2e9, "sector": "Energy"}])
    rows = universe.load_cached_universe()
    assert [r["ticker"] for r in rows] == ["AAA"]
    assert rows[0]["market_cap"] == 2e9
    assert rows[0]["sector"] == "Energy"
    assert rows[0]["quote"] == {"symbol": "AAA"}
    assert set(rows[0]) == {"ticker", "market_cap", "sector", "price", "name", "industry",
                            "industry_group", "sub_industry", "quote"}


def test_load_cached_universe_parses_a_json_string_quote_and_survives_bad_json(
        rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(rawstore, "query", lambda sql, *a, **k: [
        {"ticker": "A", "quote": '{"x": 1}', "sector": None},
        {"ticker": "B", "quote": "not json"}])
    rows = universe.load_cached_universe()
    assert rows[0]["quote"] == {"x": 1}
    assert rows[0]["sector"] == ""
    assert rows[1]["quote"] == {}


def test_load_cached_universe_is_empty_without_rows(rawstore_tmp_db):
    assert universe.load_cached_universe() == []


def test_load_universe_meta_reads_the_meta_row_and_defaults_empty(rawstore_tmp_db):
    assert universe.load_universe_meta() == {}
    rawstore.replace_all("equity_universe_provenance", [{
        "key": "__meta__", "generated_at": "2026-10-02T10:00:00+00:00",
        "refreshed_by": "Swing Potential"}])
    assert universe.load_universe_meta() == {
        "generated_at": "2026-10-02T10:00:00+00:00", "refreshed_by": "Swing Potential"}


def test_asymmetric_growth_keeps_its_old_reader_names():
    from RevenueChaser.module_asymmetric_growth import engine
    assert engine._load_cached_universe is universe.load_cached_universe
    assert engine._load_universe_meta is universe.load_universe_meta


def test_the_meta_key_has_one_definition():
    import ast
    import inspect

    from RevenueChaser.module_asymmetric_growth import engine
    assert universe.UNIVERSE_META_KEY == "__meta__"
    assert engine._UNIVERSE_META_KEY == universe.UNIVERSE_META_KEY
    tree = ast.parse(inspect.getsource(engine))
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and n.value == "__meta__"]
    assert literals == []


def test_load_universe_meta_defaults_null_fields_to_empty_strings(rawstore_tmp_db):
    rawstore.replace_all("equity_universe_provenance", [{
        "key": "__meta__", "generated_at": None, "refreshed_by": None}])
    assert universe.load_universe_meta() == {"generated_at": "", "refreshed_by": ""}
