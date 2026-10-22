"""Sector rows expand to a cap-weighted tree: Morningstar sector -> industry -> members (no network).

Phase 1 (#266): classification is UW's two levels, so the old sector -> industry group -> sub-industry tree is
sector -> industry now. (The file keeps its old name: it is the test file of the sector-row tree.)
"""
from RevenueChaser.core import rawstore
from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS
from RevenueChaser.module_sector_rotation.engine import (
    etf_trees, sector_etf_trees, sector_industry_tree,
)
from RevenueChaser.module_sector_rotation.report import (
    render_holdings_fragment, render_industry_fragment,
)
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

# The Morningstar sector -> its SPDR sector ETF (the 11 rows the rotation map expands).
SECTOR_ETF = {
    "Technology": "XLK", "Healthcare": "XLV", "Financial Services": "XLF", "Basic Materials": "XLB",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Energy": "XLE", "Industrials": "XLI",
    "Utilities": "XLU", "Real Estate": "XLRE", "Communication Services": "XLC",
}


def _seed_universe():
    rawstore.write_rows("equity_universe", [
        # Technology -> Semiconductors (three names, one unweightable)
        {"ticker": "NVDA", "name": "NVIDIA", "sector": "Technology",
         "industry": "Semiconductors", "market_cap": 300.0},
        {"ticker": "AMD", "name": "AMD", "sector": "Technology",
         "industry": "Semiconductors", "market_cap": 100.0},
        {"ticker": "AVGO", "name": "Broadcom", "sector": "Technology",
         "industry": "Semiconductors", "market_cap": 100.0},
        # Technology -> Software
        {"ticker": "MSFT", "name": "Microsoft", "sector": "Technology",
         "industry": "Software - Infrastructure", "market_cap": 200.0},
        # Other sector
        {"ticker": "XOM", "name": "Exxon", "sector": "Energy",
         "industry": "Oil & Gas Integrated", "market_cap": 50.0},
        # No market cap -> dropped entirely
        {"ticker": "ZZZZ", "name": "NoCap", "sector": "Technology",
         "industry": "Software - Infrastructure", "market_cap": None},
    ])


class TestTree:
    def test_structure_and_cap_weights(self, rawstore_tmp_db):
        _seed_universe()
        tree = sector_industry_tree()
        tech = tree["Technology"]
        industries = {i["industry"]: i for i in tech}
        assert set(industries) == {"Semiconductors", "Software - Infrastructure"}
        members = industries["Semiconductors"]["holdings"]
        assert [m["ticker"] for m in members] == ["NVDA", "AMD", "AVGO"]  # sorted desc
        assert members[0]["weight"] == 60.0
        assert round(sum(m["weight"] for m in members), 1) == 100.0

    def test_weight_is_the_share_of_the_members_own_industry(self, rawstore_tmp_db):
        _seed_universe()
        tech = {i["industry"]: i for i in sector_industry_tree()["Technology"]}
        (msft,) = tech["Software - Infrastructure"]["holdings"]
        assert msft["ticker"] == "MSFT" and msft["weight"] == 100.0   # alone in its industry, not 200/700
        (xom,) = {i["industry"]: i for i in sector_industry_tree()["Energy"]}["Oil & Gas Integrated"]["holdings"]
        assert xom["weight"] == 100.0

    def test_the_tree_is_two_levels_not_three(self, rawstore_tmp_db):
        _seed_universe()
        for industry in sector_industry_tree()["Technology"]:
            assert set(industry) == {"industry", "holdings"}      # no industry group, no sub-industry level

    def test_max_per_industry_cuts_the_list_but_not_the_weight_base(self, rawstore_tmp_db):
        rawstore.write_rows("equity_universe", [
            {"ticker": "AAA", "name": "A", "sector": "Technology", "industry": "Semiconductors", "market_cap": 60.0},
            {"ticker": "BBB", "name": "B", "sector": "Technology", "industry": "Semiconductors", "market_cap": 30.0},
            {"ticker": "CCC", "name": "C", "sector": "Technology", "industry": "Semiconductors", "market_cap": 10.0},
        ])
        (semis,) = sector_industry_tree(max_per_industry=2)["Technology"]
        assert [(m["ticker"], m["weight"]) for m in semis["holdings"]] == [("AAA", 60.0), ("BBB", 30.0)]

    def test_industry_text_is_trimmed_and_blank_text_is_unclassified(self, rawstore_tmp_db):
        rawstore.write_rows("equity_universe", [
            {"ticker": "AAA", "name": "A", "sector": "Technology", "industry": "Semiconductors", "market_cap": 10.0},
            {"ticker": "BBB", "name": "B", "sector": "Technology", "industry": " Semiconductors ", "market_cap": 10.0},
            {"ticker": "CCC", "name": "C", "sector": "Technology", "industry": "   ", "market_cap": 10.0},
        ])
        industries = {i["industry"]: {h["ticker"] for h in i["holdings"]}
                      for i in sector_industry_tree()["Technology"]}
        assert industries == {"Semiconductors": {"AAA", "BBB"}, "Unclassified": {"CCC"}}

    def test_uncapped_members_are_dropped(self, rawstore_tmp_db):
        _seed_universe()
        tree = sector_industry_tree()
        tickers = {m["ticker"] for i in tree["Technology"] for m in i["holdings"]}
        assert "ZZZZ" not in tickers

    def test_zero_cap_members_are_dropped_and_an_industry_with_none_left_is_omitted(self, rawstore_tmp_db):
        rawstore.write_rows("equity_universe", [
            {"ticker": "AAA", "name": "A", "sector": "Technology", "industry": "Semiconductors", "market_cap": 10.0},
            {"ticker": "ZERO", "name": "Zero", "sector": "Technology", "industry": "Semiconductors",
             "market_cap": 0.0},
            {"ticker": "NEG", "name": "Neg", "sector": "Technology", "industry": "Semiconductors",
             "market_cap": -5.0},
            {"ticker": "ONLYZ", "name": "Only zero", "sector": "Technology", "industry": "Software - Application",
             "market_cap": 0.0},
        ])
        (semis,) = sector_industry_tree()["Technology"]
        assert semis["industry"] == "Semiconductors"
        assert [(m["ticker"], m["weight"]) for m in semis["holdings"]] == [("AAA", 100.0)]

    def test_empty_store_is_empty_tree(self, rawstore_tmp_db):
        assert sector_industry_tree() == {}

    def test_legacy_gics_sector_spelling_lands_under_the_morningstar_name(self, rawstore_tmp_db):
        # A store the identity apply has not reached yet still holds the GICS spellings; the sector is read
        # through the one alias table, as every other board does.
        rawstore.write_rows("equity_universe", [
            {"ticker": "OLDT", "name": "Old Tech", "sector": "Information Technology",
             "industry": "Software - Application", "market_cap": 10.0},
            {"ticker": "OLDH", "name": "Old Health", "sector": "Health Care",
             "industry": "Biotechnology", "market_cap": 10.0},
        ])
        tree = sector_industry_tree()
        assert set(tree) == {"Technology", "Healthcare"}
        assert set(sector_etf_trees()) == {"XLK", "XLV"}


class TestSectorEtfTrees:
    def test_sector_etfs_carry_the_morningstar_names(self):
        sector_rows = {t: i["name"] for t, i in ROTATION_ETFS.items() if i["group"] == "sector"}
        assert sector_rows == {t: s for s, t in SECTOR_ETF.items()}
        assert set(sector_rows.values()) == set(MORNINGSTAR_SECTORS)

    def test_sector_etfs_map_to_their_morningstar_sector(self, rawstore_tmp_db):
        _seed_universe()
        trees = sector_etf_trees()
        assert "XLK" in trees
        industries = [i["industry"] for i in trees["XLK"]]
        assert industries == sorted(industries)  # industries are alphabetized
        assert set(industries) == {"Semiconductors", "Software - Infrastructure"}
        assert "SMH" not in trees  # theme ETF, not a sector
        assert "XLE" in trees

    def test_every_sector_etf_finds_its_tree(self, rawstore_tmp_db):
        # The six GICS-named rows (XLK, XLV, XLF, XLB, XLY, XLP) used to stop matching: one name from
        # each of the 11 Morningstar sectors must reach its ETF row.
        rawstore.write_rows("equity_universe", [
            {"ticker": f"T{n:02d}", "name": f"Co {n}", "sector": sector, "industry": f"Industry {n}",
             "market_cap": 10.0}
            for n, sector in enumerate(MORNINGSTAR_SECTORS)])
        trees = sector_etf_trees()
        assert set(trees) == set(SECTOR_ETF.values())
        for sector, etf in SECTOR_ETF.items():
            (only,) = trees[etf]
            assert only["industry"].startswith("Industry ") and len(only["holdings"]) == 1, (sector, etf)

    def test_only_sector_rows_match_a_tree_even_when_a_theme_shares_a_sector_name(self, monkeypatch):
        monkeypatch.setitem(ROTATION_ETFS, "ZZTH", {"name": "Energy", "group": "theme"})
        tree = {"Energy": [{"industry": "Oil & Gas E&P", "holdings": []}]}
        assert set(etf_trees(tree)) == {"XLE"}

    def test_theme_etf_has_no_sector_tree(self, rawstore_tmp_db):
        _seed_universe()
        assert sector_etf_trees().get("SMH") is None


class TestFragments:
    def test_industry_fragment_lists_industries_and_weights(self, rawstore_tmp_db):
        _seed_universe()
        frag = render_industry_fragment(sector_etf_trees()["XLK"])
        assert "Industry holdings" in frag
        assert "Sub-industry" not in frag and "GICS" not in frag
        assert "Semiconductors" in frag and "Software - Infrastructure" in frag
        assert "NVDA" in frag and "60.00%" in frag

    def test_industry_name_is_shown_once_per_industry(self, rawstore_tmp_db):
        _seed_universe()
        frag = render_industry_fragment(sector_etf_trees()["XLK"])
        assert frag.count('<b style="color:#c9d1d9">Semiconductors</b>') == 1      # three members, one label
        assert frag.count('<b style="color:#c9d1d9">Software - Infrastructure</b>') == 1

    def test_the_exported_map_embeds_the_industry_tree_for_a_sector_row(self, tmp_path, monkeypatch):
        import RevenueChaser.module_sector_rotation.report as rd
        from datetime import date
        from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
        monkeypatch.setattr(rd, "OUTPUT_DIR", tmp_path)
        snap = ETFRotationSnapshot(
            as_of_date=date(2026, 8, 14), ticker="XLK", group_name="sector", close_price=100.0,
            rs_1w=1.0, rs_1m=2.0, rs_3m=3.0, rs_ratio=1.0, rs_momentum=1.0, rs_ratio_z=0.5, rs_momentum_z=0.4,
            rrg_quadrant="LEADING")
        tree = {"XLK": [{"industry": "Semiconductors", "holdings": [
            {"ticker": "NVDA", "name": "NVIDIA", "weight": 100.0}]}]}
        p = rd.export_rotation_dashboard([snap], filename="test_industry_tree.html", uw=None,
                                         sector_holdings=tree)
        c = p.read_text(encoding="utf-8")
        assert "Industry holdings" in c and "Semiconductors" in c and "NVDA" in c
        assert "Top holdings" not in c

    def test_industry_fragment_empty_state(self):
        assert "No industry holdings" in render_industry_fragment([])

    def test_industry_fragment_escapes_hostile_text(self):
        evil = '<img src=x onerror=alert(1)>'
        html = render_industry_fragment([{"industry": evil, "holdings": [
            {"ticker": evil, "name": evil, "weight": 100.0}]}])
        assert evil not in html and "&lt;img" in html

    def test_etf_fragment_still_works(self):
        frag = render_holdings_fragment([
            {"ticker": "NVDA", "name": "NVIDIA", "weight": 14.2,
             "close": 150.0, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}])
        assert "Top holdings" in frag and "NVDA" in frag


class TestUnclassified:
    """R3 (#86): the master's ``Unclassified`` (and the legacy ``Other`` / blank) share one bucket, sorted last."""

    def _seed(self):
        rawstore.write_rows("equity_universe", [
            {"ticker": "NEE", "name": "NextEra", "sector": "Utilities",
             "industry": "Utilities - Regulated Electric", "market_cap": 100.0},
            {"ticker": "UNK1", "name": "Unknown 1", "sector": "Utilities",
             "industry": "Unclassified", "market_cap": 20.0},
            {"ticker": "UNK2", "name": "Unknown 2", "sector": "Utilities",
             "industry": "Other", "market_cap": 30.0},
            {"ticker": "UNK3", "name": "Unknown 3", "sector": "Utilities",
             "industry": None, "market_cap": 10.0},
            {"ticker": "UNK5", "name": "Unknown 5", "sector": "Utilities",
             "industry": "", "market_cap": 3.0},
            {"ticker": "ALL", "name": "All Unknown", "sector": "Unclassified",
             "industry": "Unclassified", "market_cap": 5.0},
            {"ticker": "OLD", "name": "Old Other", "sector": "Other",
             "industry": "Other", "market_cap": 7.0},
        ])

    def test_unknown_levels_share_one_unclassified_bucket_and_never_say_other(self, rawstore_tmp_db):
        self._seed()
        tree = sector_industry_tree()
        assert set(tree) == {"Utilities", "Unclassified"}                       # no "Other" sector
        industries = {i["industry"]: i for i in tree["Utilities"]}
        assert set(industries) == {"Utilities - Regulated Electric", "Unclassified"}
        assert {h["ticker"] for h in industries["Unclassified"]["holdings"]} == {"UNK1", "UNK2", "UNK3", "UNK5"}
        assert {h["ticker"] for i in tree["Unclassified"] for h in i["holdings"]} == {"ALL", "OLD"}
        assert [i["industry"] for i in tree["Unclassified"]] == ["Unclassified"]

    def test_a_row_with_no_industry_is_shown_not_dropped(self, rawstore_tmp_db):
        self._seed()
        frag = render_industry_fragment(sector_etf_trees()["XLU"])
        assert "UNK3" in frag and "UNK5" in frag and "Unclassified" in frag

    def test_unclassified_is_sorted_last_within_its_parent(self, rawstore_tmp_db):
        self._seed()
        tree = sector_industry_tree()
        assert [i["industry"] for i in tree["Utilities"]] == ["Utilities - Regulated Electric", "Unclassified"]
        # alphabetically "Unclassified" sorts before "Utilities - Water": it must still come last
        rawstore.write_rows("equity_universe", [
            {"ticker": "WTR", "name": "Water", "sector": "Utilities",
             "industry": "Utilities - Water", "market_cap": 5.0}])
        assert [i["industry"] for i in sector_industry_tree()["Utilities"]] == [
            "Utilities - Regulated Electric", "Utilities - Water", "Unclassified"]

    def test_the_unclassified_bucket_never_becomes_a_sector_etf_row(self, rawstore_tmp_db):
        self._seed()
        assert set(sector_etf_trees()) == {"XLU"}          # only the real sector's ETF row

    def test_the_detail_fragment_labels_a_blank_level_unclassified(self):
        html = render_industry_fragment([{"industry": "", "holdings": [
            {"ticker": "AAA", "name": "A", "weight": 100.0}]}])
        assert "Unclassified" in html and "Other" not in html
