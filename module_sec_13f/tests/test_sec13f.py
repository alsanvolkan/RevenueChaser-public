"""Tests for the SEC Form 13F parser + dashboard."""
import re
from datetime import date

import pytest

from RevenueChaser.module_sec_13f import engine as sf


class TestQuarterEnds:
    def test_six_quarters_from_2025q1(self):
        qs = sf.quarter_ends(today=date(2026, 8, 15))
        assert qs == ["2025-03-31", "2025-06-30", "2025-09-30",
                      "2025-12-31", "2026-03-31", "2026-06-30"]

    def test_early_year(self):
        assert sf.quarter_ends(start="2025-03-31", today=date(2025, 5, 1)) == ["2025-03-31"]


class TestReleaseSchedule:
    def test_two_quarters(self):
        sched = sf.release_schedule(n=2, today=date(2026, 8, 15))
        assert sched[0]["report_period"] == "2026 Q3"
        assert sched[0]["quarter_end"] == "2026-09-30"
        assert sched[0]["filing_due"] == "2026-11-14"
        assert sched[1]["quarter_end"] == "2026-12-31"


class TestNormalize:
    def test_issuer_name(self):
        assert sf.normalize_issuer_name("ROCKET COS INC-CLASS A") == "ROCKET"
        assert sf.normalize_issuer_name("Apple Inc.") == "APPLE"
        assert sf.normalize_issuer_name("ALPHABET INC CAP STK CL A") == "ALPHABET"
        assert sf.normalize_issuer_name("ROCKET LAB CORP") == "ROCKET LAB"
        assert sf.normalize_issuer_name("") == ""

    def test_period(self):
        assert sf.normalize_period("31-MAR-2026") == "2026-03-31"
        assert sf.normalize_period("2025-06-30") == "2025-06-30"
        assert sf.normalize_period("") is None
        assert sf.normalize_period("garbage") is None

    def test_cusip(self):
        assert sf.normalize_cusip("77311w101") == "77311W101"
        assert sf.normalize_cusip("N/A") is None
        assert sf.normalize_cusip("") is None
        assert sf.normalize_cusip("000000000") is None


class TestFilingRank:
    def test_later_filing_date_wins(self):
        assert sf.filing_rank("20-JUN-2025", "13F-HR", "x") > sf.filing_rank("15-MAY-2025", "13F-HR/A", "y")

    def test_same_day_amendment_wins_over_the_original(self):
        # An original and its amendment can carry the same filing date; the
        # amendment must win whatever order the rows come back in.
        orig = sf.filing_rank("24-AUG-2026", "13F-HR", "acc1")
        amend = sf.filing_rank("24-AUG-2026", "13F-HR/A", "acc1a")
        assert max(orig, amend) == amend and max(amend, orig) == amend


class TestPeriodMetrics:
    def test_new_exited_and_flows(self):
        periods = {
            "2025-03-31": {"shares": 100.0, "value": 1000.0, "cik": {"A": 60.0, "B": 40.0}},
            "2025-06-30": {"shares": 150.0, "value": 1500.0, "cik": {"A": 70.0, "C": 80.0}},
        }
        m = sf.compute_period_metrics(periods)
        assert m["2025-03-31"]["new_holders"] is None
        assert m["2025-06-30"]["new_holders"] == 1
        assert m["2025-06-30"]["exited_holders"] == 1
        assert m["2025-06-30"]["buy_flow"] == 90.0   # A +10, C +80
        assert m["2025-06-30"]["sell_flow"] == 40.0  # B exited (-40)
        assert m["2025-06-30"]["holders"] == 2

    def test_concentration(self):
        periods = {"2025-03-31": {"shares": 100.0, "value": 0.0,
                                  "cik": {"A": 90.0, "B": 5.0, "C": 5.0}}}
        m = sf.compute_period_metrics(periods)
        assert m["2025-03-31"]["top10_pct"] == 100.0
        assert m["2025-03-31"]["hhi"] == round((0.9 ** 2 + 0.05 ** 2 + 0.05 ** 2) * 10000, 1)


class TestDashboard:
    def test_no_sector_chips_are_left(self):
        """KF (#375): the old sector chips are gone; the shared filter kit handles Sector."""
        from RevenueChaser.module_sec_13f.report import render_sec13f_dashboard
        cell = {"shares": 100.0, "value": 1000.0, "holders": 2}
        totals = {t: {"2025-03-31": dict(cell), "2025-06-30": dict(cell, shares=120.0)} for t in ("AAA", "BBB")}
        meta = {"AAA": {"name": "A", "sector": "Technology"}, "BBB": {"name": "B", "sector": "Healthcare"}}
        h = render_sec13f_dashboard(totals, meta, {})
        assert 'filter-btn' not in h
        assert 'data-sector=' not in h
        assert 'setSector' not in h

    def test_the_filter_bar_mounts_with_the_four_facets(self):
        """KF (#375): Index, Market cap, Sector and Theme are present; Earnings is not."""
        from RevenueChaser.module_sec_13f.report import render_sec13f_dashboard
        cell = {"shares": 100.0, "value": 1000.0, "holders": 2}
        totals = {"AAA": {"2025-03-31": dict(cell), "2025-06-30": dict(cell, shares=120.0)}}
        h = render_sec13f_dashboard(totals, {"AAA": {"name": "A"}}, {})
        assert '<section class="rcf-bar" id="rc-filters"></section>' in h
        assert 'var META = ' in h
        assert '"facets":["index","mc","gics","theme"]' in h.replace(' ', '')
        # The shared kit JS is inlined so the bar can mount client-side.
        assert 'RCFilters.mount' in h
        # Earnings facet is omitted.
        assert '"facets":["index","mc","gics","theme","earnings"]' not in h.replace(' ', '')

    def test_filter_rows_ride_the_payload_not_the_html(self):
        """KF (#375): the K1 rows are in the payload's ``fields`` key, not embedded in the page."""
        from RevenueChaser.module_sec_13f.report import board_payload, render_sec13f_dashboard
        cell = {"shares": 100.0, "value": 1000.0, "holders": 2}
        totals = {"AAA": {"2025-03-31": dict(cell), "2025-06-30": dict(cell, shares=120.0)},
                  "BBB": {"2025-03-31": dict(cell), "2025-06-30": dict(cell, shares=120.0)}}
        h = render_sec13f_dashboard(totals, {"AAA": {"name": "A"}, "BBB": {"name": "B"}}, {})
        # The HTML never inlines a fields dict or a ticker field row.
        assert '"fields":' not in h or 'var FIELDS = {}' in h
        payload = board_payload(totals, {"AAA": {"name": "A"}, "BBB": {"name": "B"}}, {})
        assert "fields" in payload
        assert set(payload["fields"]) >= {"AAA", "BBB"}
        for f in payload["fields"].values():
            assert "ticker" in f and "name" in f

    def test_unknown_sectors_read_unclassified_in_the_rows(self):
        """R3 (#86): ``Other`` (stored before R3 / the vendor fallback) and ``Unclassified`` are one bucket."""
        from RevenueChaser.module_sec_13f.report import board_payload, render_sec13f_dashboard
        cell = {"shares": 100.0, "value": 1000.0, "holders": 2}
        totals = {t: {"2025-03-31": dict(cell), "2025-06-30": dict(cell, shares=120.0)} for t in ("AAA", "BBB", "CCC")}
        meta = {"AAA": {"name": "A", "sector": "Other"}, "BBB": {"name": "B", "sector": "Unclassified"},
                "CCC": {"name": "C"}}                                               # no sector stored at all
        h = render_sec13f_dashboard(totals, meta, {})
        assert 'data-sector=' not in h
        rows = board_payload(totals, meta, {})["rows"]
        assert [r[2] for r in rows] == ["Unclassified", "Unclassified", "Unclassified"]

    def test_a_stored_gics_spelling_reads_as_its_morningstar_sector(self):
        """#266: a meta row stored before the rename (``Information Technology``, ``Health Care``, ``Financials``)
        gets the Morningstar row value; no row keeps the old spelling."""
        from RevenueChaser.module_sec_13f.report import board_payload, render_sec13f_dashboard
        cell = {"shares": 100.0, "value": 1000.0, "holders": 2}
        totals = {t: {"2025-03-31": dict(cell), "2025-06-30": dict(cell, shares=120.0)} for t in ("AAA", "BBB", "CCC")}
        meta = {"AAA": {"name": "A", "sector": "Information Technology"},
                "BBB": {"name": "B", "sector": "Health Care"},
                "CCC": {"name": "C", "sector": "Financials"}}
        h = render_sec13f_dashboard(totals, meta, {})
        for old in ("Information Technology", "Health Care", "Financials"):
            assert f'data-sector="{old}"' not in h and f'data-val="{old}"' not in h
        rows = board_payload(totals, meta, {})["rows"]
        assert sorted(r[2] for r in rows) == ["Financial Services", "Healthcare", "Technology"]

    def test_an_asset_class_row_keeps_its_label(self):
        """Crypto / indices carry an asset class instead of a sector: shown on the row."""
        from RevenueChaser.module_sec_13f.report import board_payload, render_sec13f_dashboard
        cell = {"shares": 100.0, "value": 1000.0, "holders": 2}
        totals = {"BTC": {"2025-03-31": dict(cell), "2025-06-30": dict(cell, shares=120.0)}}
        meta = {"BTC": {"name": "Bitcoin", "sector": "Crypto / Digital Assets"}}
        h = render_sec13f_dashboard(totals, meta, {})
        assert board_payload(totals, meta, {})["rows"][0][2] == "Crypto / Digital Assets"
        assert 'data-sector=' not in h

    def test_render_with_new_columns(self):
        from RevenueChaser.module_sec_13f.report import board_payload, render_sec13f_dashboard
        totals = {"RKT": {"2025-03-31": {"shares": 100.0, "value": 1000.0, "holders": 2,
                                          "new_holders": None, "exited_holders": None,
                                          "top10_pct": 100.0, "hhi": 5000.0,
                                          "buy_flow": None, "sell_flow": None},
                          "2025-06-30": {"shares": 150.0, "value": 1500.0, "holders": 2,
                                          "new_holders": 1, "exited_holders": 1,
                                          "top10_pct": 90.0, "hhi": 4000.0,
                                          "buy_flow": 60.0, "sell_flow": 10.0}}}
        meta = {"RKT": {"name": "Rocket Companies Inc", "sector": "Financial Services"}}
        enrich = {"RKT": {"price": {"2025-03-31": 10.0, "2025-06-30": 12.0},
                          "shares_outstanding": 2000.0, "short_interest": 5_200_000.0, "days_to_cover": 1.5,
                          "insider_pct": 1.4}}
        h = render_sec13f_dashboard(totals, meta, enrich)
        assert "SEC 13F" in h
        assert "Value ($)" in h
        assert "% Out" in h
        assert "New/Exit" in h
        assert "Top10%" in h
        assert "Net flow" in h
        assert "Short int" in h
        assert "Insider%" in h
        assert "RKT" not in h                                        # the row lives in the payload (#429)
        cells = dict(zip(board_payload(totals, meta, enrich)["cols"],
                         board_payload(totals, meta, enrich)["rows"][0][3]))
        assert cells["short"][:2] == ["5.20M (1.5d)", 5_200_000.0]
        assert [row[0] for row in board_payload(totals, meta, enrich)["rows"]] == ["RKT"]

    def test_render_suppresses_delta_for_missing_quarter(self):
        """A quarter with no SEC data must not produce a misleading QoQ delta."""
        from RevenueChaser.module_sec_13f.report import board_payload, render_sec13f_dashboard
        totals = {"RKT": {
            "2026-03-31": {"shares": 100.0, "value": 1000.0, "holders": 5,
                           "new_holders": None, "exited_holders": None,
                           "top10_pct": 100.0, "hhi": 5000.0,
                           "buy_flow": None, "sell_flow": None},
            "2026-06-30": {"shares": 60.0, "value": 600.0, "holders": 3,
                           "new_holders": None, "exited_holders": None,
                           "top10_pct": 100.0, "hhi": 5000.0,
                           "buy_flow": None, "sell_flow": None},
        }}
        meta = {"RKT": {"name": "Rocket", "sector": "Financial Services"}}
        render_sec13f_dashboard(totals, meta, {})                    # renders without error
        payload = board_payload(totals, meta, {})
        cells = dict(zip(payload["cols"], payload["rows"][0][3]))
        # Full-SEC quarter keeps its Δ% vs prior quarter.
        assert cells["ch_2026-06-30"] == ["-40.0%", -40.0, "neg"]

    def test_render_shows_sec_to_sec_deltas(self):
        """Full-SEC consecutive quarters keep their QoQ metrics."""
        from RevenueChaser.module_sec_13f.report import board_payload, render_sec13f_dashboard
        totals = {"RKT": {
            "2026-03-31": {"shares": 100.0, "value": 1000.0, "holders": 5,
                           "new_holders": None, "exited_holders": None,
                           "top10_pct": 100.0, "hhi": 5000.0,
                           "buy_flow": None, "sell_flow": None},
            "2026-06-30": {"shares": 150.0, "value": 1500.0, "holders": 6,
                           "new_holders": 2, "exited_holders": 1,
                           "top10_pct": 90.0, "hhi": 4000.0,
                           "buy_flow": 60.0, "sell_flow": 10.0},
        }}
        meta = {"RKT": {"name": "Rocket", "sector": "Financial Services"}}
        render_sec13f_dashboard(totals, meta, {})
        payload = board_payload(totals, meta, {})
        cells = dict(zip(payload["cols"], payload["rows"][0][3]))
        assert cells["ch_2026-06-30"] == ["+50.0%", 50.0, "pos"]
        assert cells["streak"] == ["1", 1, "zero"]
        assert cells["netflow"] == ["50", 50.0, "pos"]
        assert cells["new_exit"] == ["+2/−1", 1, "zero"]             # +2/−1


class TestUniverseInfo:
    """The 13F meta step's own sector names are the Morningstar ones (#266)."""

    def test_the_override_table_holds_morningstar_names_only(self):
        from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS
        from RevenueChaser.module_sec_13f.universe_info import TICKER_GICS_OVERRIDES
        assert TICKER_GICS_OVERRIDES
        assert set(TICKER_GICS_OVERRIDES.values()) <= set(MORNINGSTAR_SECTORS)

    def test_resolve_sector_for_an_override_ticker(self):
        from RevenueChaser.module_sec_13f.universe_info import resolve_sector
        assert resolve_sector("000660", "") == "Technology"          # was Information Technology
        assert resolve_sector("BRK.B", "") == "Financial Services"   # was Financials
        assert resolve_sector("TIL", "") == "Healthcare"             # was Health Care
        assert resolve_sector("DSV", "") == "Industrials"
        # the override wins over whatever the screener said
        assert resolve_sector("FNMA", "Technology") == "Financial Services"

    def test_an_override_spelled_the_old_way_still_resolves_to_the_morningstar_name(self, monkeypatch):
        """The override goes through ``canonical_sector`` too, so a hand-added old GICS spelling cannot leak out."""
        from RevenueChaser.module_sec_13f import universe_info
        monkeypatch.setitem(universe_info.TICKER_GICS_OVERRIDES, "OLDX", "Health Care")
        assert universe_info.resolve_sector("OLDX", "") == "Healthcare"

    def test_resolve_sector_maps_vendor_and_old_spellings(self):
        from RevenueChaser.module_sec_13f.universe_info import resolve_sector
        assert resolve_sector("AAPL", "Technology") == "Technology"
        assert resolve_sector("AAPL", "Information Technology") == "Technology"
        assert resolve_sector("JNJ", "Health Care") == "Healthcare"
        assert resolve_sector("JPM", "Financial Services") == "Financial Services"
        assert resolve_sector("JPM", "Financials") == "Financial Services"
        assert resolve_sector("NKE", "Consumer Discretionary") == "Consumer Cyclical"
        assert resolve_sector("PG", "consumer staples") == "Consumer Defensive"
        assert resolve_sector("LIN", "Materials") == "Basic Materials"

    def test_an_unknown_vendor_sector_is_unclassified(self):
        from RevenueChaser.module_sec_13f.universe_info import resolve_sector
        for raw in ("", "Other", "Spaceships", None):
            assert resolve_sector("ZZZ", raw) == "Unclassified"

    def test_a_non_equity_keeps_its_asset_class(self):
        from RevenueChaser.module_sec_13f.universe_info import resolve_sector
        assert resolve_sector("BTC", "Technology") == "Crypto / Digital Assets"
        assert resolve_sector("NDX", "") == "Indices & Benchmarks"

    def test_fetch_universe_info_stores_morningstar_sectors(self):
        from RevenueChaser.module_sec_13f.universe_info import fetch_universe_info

        class _UW:
            def screener_batch(self, tickers):
                return {"AAPL": {"full_name": "Apple Inc", "issue_type": "Common Stock",
                                 "sector": "Information Technology", "close": "190", "prev_close": "189"},
                        "JNJ": {"full_name": "Johnson & Johnson", "issue_type": "Common Stock", "sector": "Healthcare"},
                        "ODD": {"full_name": "Odd Co", "issue_type": "Common Stock", "sector": "Other"}}

        info = fetch_universe_info(_UW(), ["AAPL", "JNJ", "ODD", "BRK.B"])
        assert info["AAPL"]["sector"] == "Technology"
        assert info["JNJ"]["sector"] == "Healthcare"
        assert info["ODD"]["sector"] == "Unclassified"
        assert info["BRK.B"]["sector"] == "Financial Services"       # alias row: the override, no screener row

    def test_fetch_universe_info_keeps_only_what_a_reader_uses(self):
        """DF91: price and prev_close were fetched, converted and never read."""
        from RevenueChaser.module_sec_13f.universe_info import fetch_universe_info

        class _UW:
            def screener_batch(self, tickers):
                return {"AAPL": {"full_name": "Apple Inc", "issue_type": "Common Stock", "sector": "Technology",
                                 "close": "190", "prev_close": "189"}}

        info = fetch_universe_info(_UW(), ["AAPL", "BRK.B"])
        assert set(info["AAPL"]) == {"name", "sector", "type"}
        assert set(info["BRK.B"]) == {"name", "sector", "type"}
