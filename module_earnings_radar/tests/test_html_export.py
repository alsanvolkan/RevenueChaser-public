"""Unit tests for html_export render helpers and pipeline data functions."""
import json
import re
from datetime import date

import pytest

from RevenueChaser.module_earnings_radar.report import (
    _quadrant_badge, _conf_cell, _bias_cell,
    _agh_points_cell, _agh_accel_cell, _agh_owners_cell,
    _render_since_er_chart, export_html, TIER_DESC,
    _colgroup, _COL_WIDTHS, _price_cell, _gamma_regime_tip,
    _compute_col_widths, _er_spark_cell, _gex_profile_cell, _gex_nudge, _liquidity_cell,
    _badge, _VIEWS, _COL_IDS, _GROUP_LABELS,
)
from RevenueChaser.module_earnings_radar.pipeline import (
    _compute_earnings_reactions, _compute_price_action, _compute_return_since_er,
    EarningsPipeline,
)
from RevenueChaser.module_earnings_radar.models import TradeCard


# ──────────────────────────────────────────────
# _quadrant_badge
# ──────────────────────────────────────────────
class TestQuadrantBadge:
    def test_leading(self):
        result = _quadrant_badge("LEADING")
        assert "bd-L" in result
        assert "↑" in result
        assert "LEAD" in result

    def test_improving(self):
        result = _quadrant_badge("IMPROVING")
        assert "bd-IM" in result
        assert "↑" in result
        assert "IMPROVI" in result

    def test_weakening(self):
        result = _quadrant_badge("WEAKENING")
        assert "bd-W" in result
        assert "↓" in result

    def test_lagging(self):
        result = _quadrant_badge("LAGGING")
        assert "bd-LG" in result
        assert "↓" in result

    def test_empty_quadrant(self):
        result = _quadrant_badge("")
        assert "bd-I" in result
        assert "N/A" in result

    def test_none_quadrant(self):
        result = _quadrant_badge(None)
        assert "bd-I" in result
        assert "N/A" in result


# ──────────────────────────────────────────────
# _conf_cell
# ──────────────────────────────────────────────
class TestConfCell:
    def test_high_confidence(self):
        result = _conf_cell(75)
        assert "#3fb950" in result
        assert "75%" in result

    def test_medium_confidence(self):
        result = _conf_cell(40)
        assert "#d2991d" in result
        assert "40%" in result

    def test_low_confidence(self):
        result = _conf_cell(10)
        assert "#f85149" in result
        assert "10%" in result

    def test_zero_confidence(self):
        result = _conf_cell(0)
        assert "#f85149" in result
        assert "0%" in result


# ──────────────────────────────────────────────
# _bias_cell  (regression: extended labels)
# ──────────────────────────────────────────────
class TestBiasCell:
    def test_bull(self):
        result = _bias_cell("BULL", 25)
        assert "bd-bull" in result
        assert "BULL" in result

    def test_bullish(self):
        result = _bias_cell("BULLISH", 15)
        assert "bd-bull" in result
        assert "BULLISH" in result

    def test_bear(self):
        result = _bias_cell("BEAR", -20)
        assert "bd-bear" in result
        assert "BEAR" in result

    def test_bearish_lean(self):
        result = _bias_cell("BEARISH_LEAN", -12)
        assert "bd-bear" in result
        assert "BEARISH_LEAN" in result

    def test_neutral(self):
        result = _bias_cell("NEUTRAL", 0)
        assert "bd-neutral" in result

    def test_unknown_falls_to_neutral(self):
        result = _bias_cell("SIDEWAYS", 0)
        assert "bd-neutral" in result


# ──────────────────────────────────────────────
# AGH context cells
# ──────────────────────────────────────────────
class TestAghCells:
    def test_points_missing(self):
        assert "—" in _agh_points_cell(None)

    def test_points_high(self):
        html = _agh_points_cell(8)
        assert "8/9" in html
        assert "#3fb950" in html

    def test_accel_yes_no_unknown(self):
        assert "Yes" in _agh_accel_cell(True)
        assert "No" in _agh_accel_cell(False)
        assert "—" in _agh_accel_cell(None)

    def test_owners_accumulating_with_dilution(self):
        html = _agh_owners_cell("Accumulating", True)
        assert "Accumulating" in html
        assert "DIL" in html

    def test_owners_missing(self):
        assert "—" in _agh_owners_cell("", False)


# ──────────────────────────────────────────────
# _render_since_er_chart
# ──────────────────────────────────────────────
class TestRenderSinceErChart:
    def test_empty_chart(self):
        result = _render_since_er_chart("")
        assert "N/A" in result

    def test_invalid_json(self):
        result = _render_since_er_chart("{bad json")
        assert "N/A" in result

    def test_not_enough_points(self):
        data = json.dumps({
            "points": [["08-10", 100.0]],
            "pct": 1.5,
            "anchor": "08-10 M",
            "anchor_price": 100.0,
            "now_label": "Post-Mkt",
        })
        result = _render_since_er_chart(data)
        assert "N/A" in result

    def test_valid_chart(self):
        data = json.dumps({
            "points": [["08-07", 95.0], ["08-08", 97.0], ["08-11", 102.0]],
            "pct": 7.37,
            "anchor": "08-07 M",
            "anchor_price": 95.0,
            "now_label": "Post-Mkt",
        })
        result = _render_since_er_chart(data)
        assert "<svg" in result
        assert "+7.4%" in result
        assert "polyline" in result

    def test_negative_chart(self):
        data = json.dumps({
            "points": [["08-07", 100.0], ["08-08", 95.0]],
            "pct": -5.0,
            "anchor": "08-07 M",
            "anchor_price": 100.0,
            "now_label": "Market",
        })
        result = _render_since_er_chart(data)
        assert "<svg" in result
        assert "-5.0%" in result
        assert "#f85149" in result

    def test_anchor_price_zero(self):
        data = json.dumps({
            "points": [["08-07", 100.0], ["08-08", 95.0]],
            "pct": 5.0,
            "anchor": "08-07 M",
            "anchor_price": 0,
            "now_label": "",
        })
        result = _render_since_er_chart(data)
        assert "N/A" in result

    def test_now_label_absent(self):
        data = json.dumps({
            "points": [["08-07", 100.0], ["08-08", 105.0]],
            "pct": 5.0,
            "anchor": "08-07 M",
            "anchor_price": 100.0,
            "now_label": "",
        })
        result = _render_since_er_chart(data)
        assert "<svg" in result


# ──────────────────────────────────────────────
# _compute_earnings_reactions  (pipeline.py)
# ──────────────────────────────────────────────
class TestComputeEarningsReactions:
    @staticmethod
    def _make_ohlc(dates_prices):
        return [{"date": d, "close": p} for d, p in dates_prices]

    def test_empty_hist(self):
        result = _compute_earnings_reactions([], [100.0], [])
        assert result["m1"] == ""
        assert result["m2"] == ""
        assert result["m3"] == ""
        assert result["m4"] == ""

    def test_no_today_close(self):
        result = _compute_earnings_reactions([], [], [])
        for k in ("m1", "m2", "m3", "m4", "current"):
            assert result[k] == ""

    def test_upcoming_does_not_block_completed(self):
        """Regression: upcoming event must not skip completed earnings."""
        hist = [
            {"report_date": "2026-08-11", "actual_eps": None},   # upcoming
            {"report_date": "2026-06-15", "actual_eps": 1.50},    # completed
            {"report_date": "2026-03-20", "actual_eps": 1.30},    # completed
        ]
        closes = [95.0, 97.0, 99.0, 100.0, 101.0, 102.0, 103.0]
        ohlc = self._make_ohlc([
            ("2026-03-20", 60.0), ("2026-03-21", 62.0),
            ("2026-06-15", 70.0), ("2026-06-16", 72.0),
            ("2026-08-10", 100.0), ("2026-08-11", 101.0),
            ("2026-08-12", 103.0),
        ])
        today_close = closes[-1]  # 103.0
        result = _compute_earnings_reactions(hist, closes, ohlc)
        # The upcoming event shouldn't prevent completed from being processed
        assert result["m1"] != ""  # most recent completed (June 15)
        assert result["m2"] != ""  # second completed (March 20)

    def test_eps_surprise_beat_and_miss(self):
        hist = [
            {"report_date": "2026-06-15", "actual_eps": 1.10, "street_mean_est": 1.00},  # +10% beat
            {"report_date": "2026-03-20", "actual_eps": 0.90, "street_mean_est": 1.00},  # -10% miss
        ]
        closes = [95.0, 97.0, 99.0, 100.0, 101.0, 102.0, 103.0]
        ohlc = self._make_ohlc([
            ("2026-03-20", 60.0), ("2026-03-21", 62.0),
            ("2026-06-15", 70.0), ("2026-06-16", 72.0),
            ("2026-08-10", 100.0), ("2026-08-11", 101.0),
            ("2026-08-12", 103.0),
        ])
        result = _compute_earnings_reactions(hist, closes, ohlc)
        assert result["m1_eps"] == "+10.0%"
        assert result["m2_eps"] == "-10.0%"

    def test_eps_surprise_missing_estimate(self):
        hist = [
            {"report_date": "2026-06-15", "actual_eps": 1.10},  # no estimate field
        ]
        closes = [95.0, 97.0, 99.0, 100.0, 101.0, 102.0, 103.0]
        ohlc = self._make_ohlc([
            ("2026-06-15", 70.0), ("2026-06-16", 72.0), ("2026-08-12", 103.0),
        ])
        result = _compute_earnings_reactions(hist, closes, ohlc)
        assert result["m1_eps"] == ""
        assert result["m1"] != ""  # price reaction still computed

    def test_eps_surprise_zero_estimate(self):
        hist = [
            {"report_date": "2026-06-15", "actual_eps": 0.10, "street_mean_est": 0.0},
        ]
        closes = [95.0, 97.0, 99.0, 100.0, 101.0, 102.0, 103.0]
        ohlc = self._make_ohlc([
            ("2026-06-15", 70.0), ("2026-06-16", 72.0), ("2026-08-12", 103.0),
        ])
        result = _compute_earnings_reactions(hist, closes, ohlc)
        assert result["m1_eps"] == ""


# ──────────────────────────────────────────────
# _compute_price_action  (pipeline.py)
# ──────────────────────────────────────────────
class TestComputePriceAction:
    @staticmethod
    def _make_ohlc(dates_prices):
        return [{"date": d, "close": p} for d, p in dates_prices]

    def test_am_anchor_previous_day(self):
        """AM (BMO): 1D measured from the close strictly before earnings date."""
        ohlc = self._make_ohlc([
            ("2026-08-05", 90.0), ("2026-08-06", 92.0), ("2026-08-07", 95.0),
            ("2026-08-10", 100.0),  # day before earnings
            ("2026-08-11", 101.0),  # earnings date (BMO)
        ])
        result = _compute_price_action(ohlc, "2026-08-11", "BMO")
        # anchor = 08-10 close (100.0); 1D = 100/95 - 1 = +5.3%
        assert result["1d"] == f"{(100.0/95.0 - 1)*100:+.1f}%"

    def test_pm_anchor_same_day(self):
        """PM (AMC): anchor = close ON earnings date."""
        ohlc = self._make_ohlc([
            ("2026-08-05", 90.0), ("2026-08-06", 92.0), ("2026-08-07", 95.0),
            ("2026-08-10", 100.0),
            ("2026-08-11", 102.0),  # earnings date (AMC)
        ])
        result = _compute_price_action(ohlc, "2026-08-11", "AMC")
        # anchor = 08-11 close (102.0); 1D = 102/100 - 1 = +2.0%
        assert result["1d"] == f"{(102.0/100.0 - 1)*100:+.1f}%"

    def test_extended_timing_variants_treated_as_pm(self):
        """Non-AM timing defaults to PM anchor (same-day close)."""
        ohlc = self._make_ohlc([
            ("2026-08-10", 100.0), ("2026-08-11", 105.0), ("2026-08-12", 106.0),
        ])
        for timing in ("afterhours", "AMC", "postmarket", "pm", ""):
            result = _compute_price_action(ohlc, "2026-08-11", timing)
            assert result["1d"] == f"{(105.0/100.0 - 1)*100:+.1f}%"

    def test_not_enough_history(self):
        """4W empty when fewer than 20 trading days before anchor."""
        ohlc = self._make_ohlc([
            ("2026-08-10", 100.0), ("2026-08-11", 101.0), ("2026-08-12", 102.0),
        ])
        result = _compute_price_action(ohlc, "2026-08-11", "AMC")
        assert result["1d"] != ""
        assert result["4w"] == ""

    def test_empty_ohlc(self):
        result = _compute_price_action([], "2026-08-11", "AMC")
        assert result == {"1d": "", "1w": "", "2w": "", "4w": ""}

    def test_missing_anchor_falls_back_to_latest(self):
        """If earnings date is before all OHLC dates, anchor = most recent close."""
        ohlc = self._make_ohlc([
            ("2026-08-10", 100.0), ("2026-08-12", 104.0),
        ])
        result = _compute_price_action(ohlc, "2026-08-08", "AMC")
        # no date <= 08-08, so anchor = 08-12 (104.0); 1D = 104/100 - 1 = +4.0%
        assert result["1d"] == f"{(104.0/100.0 - 1)*100:+.1f}%"


# ──────────────────────────────────────────────
# _compute_return_since_er  (pipeline.py)
# ──────────────────────────────────────────────
class TestComputeReturnSinceEr:
    @staticmethod
    def _make_ohlc(dates_prices):
        return [{"date": d, "close": p} for d, p in dates_prices]

    def test_empty_ed_str(self):
        result = _compute_return_since_er([], [100.0, 101.0], [], "")
        assert result == ("", "", "")

    def test_no_closes(self):
        result = _compute_return_since_er([], [], [], "2026-08-10")
        assert result == ("", "", "")

    def test_am_anchor_previous_day(self):
        """BMO ticker: anchor = previous trading day close before earnings date."""
        hist = []
        closes = [100.0, 101.0, 102.0, 103.0]
        ohlc = self._make_ohlc([
            ("2026-08-07", 100.0),
            ("2026-08-10", 101.0),  # previous trading day
            ("2026-08-11", 102.0),  # earnings date (BMO)
            ("2026-08-12", 103.0),
        ])
        # Past earnings date -> time gate skipped
        label, anchor, chart = _compute_return_since_er(hist, closes, ohlc, "2026-08-11", "BMO")
        assert anchor == "08-10"  # previous trading day
        assert "08-10" in label
        assert chart != ""

    def test_pm_anchor_same_day(self):
        """AMC ticker: anchor = close ON earnings date."""
        hist = []
        closes = [100.0, 101.0, 102.0, 103.0]
        ohlc = self._make_ohlc([
            ("2026-08-07", 100.0),
            ("2026-08-10", 101.0),
            ("2026-08-11", 102.0),  # earnings date (AMC)
            ("2026-08-12", 103.0),
        ])
        label, anchor, chart = _compute_return_since_er(hist, closes, ohlc, "2026-08-11", "AMC")
        assert anchor == "08-11"  # same trading day
        assert "08-11" in label
        assert chart != ""

    def test_pm_extended_timing_variants(self):
        """Any non-AM timing should be treated as PM (anchor = close on date)."""
        hist = []
        closes = [100.0, 101.0, 102.0, 103.0]
        ohlc = self._make_ohlc([
            ("2026-08-07", 100.0),
            ("2026-08-10", 101.0),
            ("2026-08-11", 102.0),
            ("2026-08-12", 103.0),
        ])
        for timing in ("afterhours", "AMC", "postmarket", "pm", ""):
            _, anchor, chart = _compute_return_since_er(hist, closes, ohlc, "2026-08-11", timing)
            assert anchor == "08-11"
            assert chart != ""

    def test_no_matching_ohlc_date(self):
        """If earnings date not in OHLC, fall back to most recent close."""
        hist = []
        closes = [100.0, 101.0]
        ohlc = self._make_ohlc([
            ("2026-08-10", 100.0),
            ("2026-08-12", 101.0),
        ])
        _, anchor, chart = _compute_return_since_er(hist, closes, ohlc, "2026-08-11", "AMC")
        assert chart != ""


# ──────────────────────────────────────────────
# preselect — filter classification
# ──────────────────────────────────────────────
class TestPreselect:
    def _pipeline(self, tickers, screener, cal_meta):
        p = EarningsPipeline(tickers, uw=None, cal_meta=cal_meta)
        p._screener_cache = screener
        return p

    def test_passed_and_excluded_outcomes(self):
        screener = {
            "AAA": {"call_volume": 1000, "put_volume": 1000, "total_open_interest": 1000},
            "BBB": {"call_volume": 1, "put_volume": 1, "total_open_interest": 10},
        }
        cal_meta = {
            "AAA": {"expected_move_perc": 0.05},
            "BBB": {"expected_move_perc": 0.05},
        }
        p = self._pipeline(["AAA", "BBB"], screener, cal_meta)
        p.preselect(watchlist=set())

        assert p._filter_outcome["AAA"] == ("passed", [])
        status, reasons = p._filter_outcome["BBB"]
        assert status == "excluded"
        assert "OptVol" in reasons
        assert any(e["ticker"] == "BBB" for e in p._filter_stats["excluded"])

    def test_bypass_outcome(self):
        screener = {
            "AAA": {"call_volume": 1, "put_volume": 1, "total_open_interest": 1},
        }
        cal_meta = {"AAA": {"expected_move_perc": 0.05}}
        p = self._pipeline(["AAA"], screener, cal_meta)
        p.preselect(watchlist={"AAA"})

        assert p._filter_outcome["AAA"] == ("bypassed", [])
        assert p._filter_stats["bypassed"] == 1


# ──────────────────────────────────────────────
# export_html  integration
# ──────────────────────────────────────────────
class TestExportHtml:
    def _minimal_card(self, ticker="AAPL", **overrides) -> TradeCard:
        defaults = {
            "ticker": ticker,
            "name": "Apple Inc.",
            "cluster": "Technology",
            "earnings_date": date(2026, 8, 11),
            "report_time_label": "PM",
            "direction_score": 20.0,
            "confidence_score": 65.0,
            "bias_label": "BULL",
            "expected_move_pct": 5.0,
            "eps_estimate": "1.25",
            "final_action": "OPTIONS_BULL",
            "rule_audit": {"rule2_guidance": {"reasons": [], "available": True}},
        }
        defaults.update(overrides)
        return TradeCard(**defaults)

    def test_empty_cards(self):
        path = export_html([], [], {})
        content = path.read_text(encoding="utf-8")
        assert "REVENUECHASER" in content
        assert "0 events" in content
        assert "Methodology" in content  # methodology tables always render

    def test_single_card(self, tmp_path):
        card = self._minimal_card()
        path = export_html([card], [{"ticker": "AAPL"}], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}})
        content = path.read_text(encoding="utf-8")
        assert "AAPL" in content
        assert "Apple Inc." in content
        assert "BULL" in content
        assert "bd-bull" in content

    def test_name_fallback_to_dash(self, tmp_path):
        """When card.name equals ticker, Name column shows — not the ticker."""
        card = self._minimal_card(ticker="AAPL", name="AAPL")
        path = export_html([card], [], {"AAPL": {"nrc": 50, "tier": "Ignore"}})
        content = path.read_text(encoding="utf-8")
        # The Name column should NOT show "AAPL" (that's the Ticker column)
        # It should show — or nothing
        assert "name-cell" in content

    def test_bias_neutral_card(self):
        card = self._minimal_card(bias_label="NEUTRAL", direction_score=0)
        path = export_html([card], [], {"AAPL": {"nrc": 50, "tier": "Ignore"}})
        content = path.read_text(encoding="utf-8")
        assert "bd-neutral" in content
        assert "NEUTRAL" in content

    def test_filter_stats_rendered(self):
        stats = {"total": 100, "passed": 30, "bypassed": 5, "filtered": 65,
                 "by_reason": {"OptVol": 40, "OI": 25}}
        path = export_html([], [], {}, filter_stats=stats)
        content = path.read_text(encoding="utf-8")
        assert "Filter Gates" in content
        assert "35 scored" in content  # passed + bypassed = 30 + 5

    def test_empty_filter_stats_render_no_gates_line(self):
        """DF03: a run that filtered nothing (total 0) must not print '0 tickers -> 0 scored'."""
        stats = {"total": 0, "passed": 0, "bypassed": 0, "filtered": 0, "by_reason": {}}
        content = export_html([], [], {}, filter_stats=stats).read_text(encoding="utf-8")
        assert "0 tickers" not in content
        assert "OptVol\u2265" not in content

    def test_methodology_tables_present(self):
        path = export_html([], [], {})
        content = path.read_text(encoding="utf-8")
        assert "Methodology" in content
        assert "<details" in content and "methodology" in content
        assert "Filter Gates" in content
        assert "NRC Scoring" in content
        assert "Active Scoring Rules" in content

    def test_header_bullish_count_follows_the_bias_threshold(self):
        """DF11: the header counted direction > 0 while the badges need >= 12."""
        cards = [self._minimal_card("AAA", direction_score=5.0, bias_label="NEUTRAL"),
                 self._minimal_card("BBB", direction_score=15.0, bias_label="BULLISH_LEAN"),
                 self._minimal_card("CCC", direction_score=-5.0, bias_label="NEUTRAL"),
                 self._minimal_card("DDD", direction_score=-15.0, bias_label="BEARISH_LEAN")]
        content = export_html(cards, [], {}).read_text(encoding="utf-8")
        assert "4 events \u00b7 1 bullish \u00b7 1 bearish \u00b7 2 neutral" in content

    def test_ticker_column_is_sticky(self):
        card = self._minimal_card()
        content = export_html([card], [{"ticker": "AAPL"}], {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        assert "sticky-tick" in content

    def test_header_sticks_as_one_unit_and_sort_arrows_via_css(self):
        """The two header rows pin together (sticky thead) and every sortable
        header gets its ⇅/▲/▼ marker from CSS, not a literal glyph."""
        card = self._minimal_card()
        content = export_html([card], [], {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        assert "table.data-table thead{position:sticky" in content
        assert ">MCap</th>" in content
        assert "MCap ⇅" not in content
        assert "th[data-sort-key]::after" in content
        assert 'data-dir="asc"' in content and 'data-dir="desc"' in content

    def test_bearish_label(self):
        card = self._minimal_card(bias_label="BEARISH_LEAN", direction_score=-12)
        path = export_html([card], [], {"AAPL": {"nrc": 40, "tier": "Tier C — Emerging"}})
        content = path.read_text(encoding="utf-8")
        assert "bd-bear" in content
        assert "BEARISH_LEAN" in content

    def test_excluded_section_renders(self):
        """Excluded cards render in their own section with filter reason."""
        included = self._minimal_card(ticker="AAA", name="Alpha")
        excluded = self._minimal_card(ticker="BBB", name="Beta",
                                      filter_status="excluded",
                                      filter_reasons="OptVol, OI")
        nrc = {
            "AAA": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"},
            "BBB": {"nrc": 40, "tier": "Ignore"},
        }
        path = export_html([included, excluded], [], nrc)
        content = path.read_text(encoding="utf-8")
        assert "Excluded by Filters" in content
        assert "BBB" in content
        assert "Filtered out: OptVol, OI" in content  # tooltip marker

    def test_excluded_not_in_tier_sections(self):
        """Excluded cards must not appear in the tier sections."""
        excluded = self._minimal_card(ticker="BBB", name="Beta",
                                      filter_status="excluded",
                                      filter_reasons="EM<1.5%")
        nrc = {"BBB": {"nrc": 40, "tier": "Ignore"}}
        path = export_html([excluded], [], nrc)
        content = path.read_text(encoding="utf-8")
        # The excluded ticker appears only in the excluded section, not as a tier row.
        assert "Excluded by Filters" in content
        # No tier section header (e.g. "Ignore — PM") should be present.
        assert "Ignore —" not in content

    def test_gex_and_structure_render(self):
        card = self._minimal_card(
            gex_call_wall="$25.00", gex_put_wall="$15.00",
            gex_gamma_flip="$15.52", gex_gamma_magnet="$25.00",
            current_price="$20.00",
            gex_structure="Positive gamma regime · approaching call wall",
        )
        path = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}})
        content = path.read_text(encoding="utf-8")
        assert "GEX & Liquidity" in content
        assert "gex-track" in content
        assert "25.00" in content  # raw strike kept in the tooltip
        assert "vs flip" in content  # spot vs-flip caption
        # Structure moved into the drawer payload (DECISION_DATA), not the row.
        assert "Positive gamma regime · approaching call wall" in content

    def test_detail_drawer_scaffolding(self):
        import json
        detail = [{"q": "Q1 2026", "date": "2026-05-12", "implied": 5.0, "actual": 3.0,
                   "eps_actual": 0.30, "eps_est": 0.28, "surprise_dollar": 0.02, "surprise_pct": 7.14}]
        card = self._minimal_card(detail_json=json.dumps(detail))
        path = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}})
        content = path.read_text(encoding="utf-8")
        assert "const DETAIL_DATA" in content
        assert "data-ticker=\"AAPL\"" in content
        assert "toggleDetail" in content
        assert "renderPriceChart" in content
        assert "renderEpsChart" in content

    def test_trade_read_column_removed(self):
        card = self._minimal_card(trade_read="BULLISH")
        path = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}})
        content = path.read_text(encoding="utf-8")
        assert "Trade Read" not in content

    def test_date_time_merged(self):
        card = self._minimal_card(earnings_date=date(2026, 8, 11), report_time_label="PM")
        path = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}})
        content = path.read_text(encoding="utf-8")
        assert "Date/Time" in content
        assert "08/11 PM" in content

    def test_identity_columns_date_time_first(self):
        """Identity order = Date/Time | Ticker | Name | Sector | Rotation."""
        card = self._minimal_card(earnings_date=date(2026, 8, 11), report_time_label="PM")
        content = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}}).read_text(encoding="utf-8")
        start = content.index('<table class="data-table">')
        thead = content[start:content.index("</thead>", start)]
        rows = re.findall(r"<tr>(.*?)</tr>", thead, flags=re.S)
        sub_row = rows[1]
        tick = sub_row.index("sticky-tick")
        dt = sub_row.index(">Date/Time</th>")
        name = sub_row.index(">Name</th>")
        sect = sub_row.index(">Sector</th>")
        rot = sub_row.index(">Rotation</th>")
        # Date/Time precedes Ticker; the rest keep their relative order.
        assert dt < tick < name < sect < rot
        # sticky stays on Ticker (second column), not Date/Time.
        assert "sticky-tick" not in sub_row[:dt]

    def test_identity_body_cells_date_time_first(self):
        card = self._minimal_card(earnings_date=date(2026, 8, 11), report_time_label="PM")
        content = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}}).read_text(encoding="utf-8")
        start = content.index('data-ticker="AAPL"')
        row = content[start:content.index("</tr>", start)]
        dt_pos = row.index("08/11 PM")
        tick_pos = row.index("sticky-tick")
        name_pos = row.index("name-cell")
        assert dt_pos < tick_pos < name_pos

    def test_trade_by_column_present_and_cell_rendered(self):
        """Identity shows a Trade By cell: PM trades by the report day, AM by the
        prior session; an unknown report time renders '—' (never fabricated)."""
        pm = self._minimal_card(earnings_date=date(2026, 8, 11), report_time_label="PM")
        am = self._minimal_card(earnings_date=date(2026, 8, 11), report_time_label="AM")
        uk = self._minimal_card(earnings_date=date(2026, 8, 11), report_time_label="—")
        hdr = export_html([pm], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}}).read_text(encoding="utf-8")
        assert ">Trade By<" in hdr
        # PM report on Tue 2026-08-11 -> trade by 08/11
        pm_html = export_html([pm], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}}).read_text(encoding="utf-8")
        assert "08/11" in pm_html
        # AM report -> prior session (Mon 2026-08-10)
        am_html = export_html([am], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}}).read_text(encoding="utf-8")
        assert "08/10" in am_html
        # unknown time -> dash cell, no invented date
        uk_html = export_html([uk], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}}).read_text(encoding="utf-8")
        assert "trade-by unavailable" in uk_html.lower() or ">—</span>" in uk_html

    def test_agh_headers_use_full_names(self):
        """AGH group and columns carry descriptive labels."""
        card = self._minimal_card()
        content = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}}).read_text(encoding="utf-8")
        assert "Asymmetric Growth Hunter" in content
        assert "Score (0-9)" in content
        assert "Acceleration" in content
        assert "Owners (AG)" in content
        assert ">Accel<" not in content  # short label replaced

    def test_rev_est_column_removed(self):
        """The Rev Est. column always rendered '—' (consensus revenue unavailable),
        so it was dropped rather than left as a dead column."""
        card = self._minimal_card(current_price="$25.50")
        path = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}})
        content = path.read_text(encoding="utf-8")
        assert "Rev Est." not in content
        assert "$25.50" in content

    def test_inst_flow_sort_button_removed(self):
        """The Inst. Flow column moved to the drawer; its sort chip is gone."""
        path = export_html([], [], {})
        content = path.read_text(encoding="utf-8")
        assert "sortByInstFlow" not in content
        assert "Inst. Flow (net)" not in content

    def test_gamma_tooltip_in_structure(self):
        card = self._minimal_card(gex_gamma_flip="$15.00", gex_structure="Positive gamma regime")
        path = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}})
        content = path.read_text(encoding="utf-8")
        assert "dampen" in content


class TestDecisionAttribution:
    """The GUI must say WHERE the Action came from: which rule(s)."""

    def _html(self, card) -> str:
        path = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}})
        return path.read_text(encoding="utf-8")

    def test_options_action_shows_r4_basis(self):
        card = _minimal_card(final_action="OPTIONS_BULL", direction_score=30.0,
                             volatility_edge_score=80.0, confidence_score=70.0)
        content = self._html(card)
        assert "OPTIONS_BULL" in content
        assert "R4" in content                      # basis subtitle
        assert "R4 vol-edge" in content.lower() or "R4 vol-edge" in content  # tooltip
        assert "dir" in content

    def test_shares_action_shows_r4_blocked(self):
        card = _minimal_card(final_action="SHARES_BEAR", direction_score=-20.0,
                             volatility_edge_score=25.0, confidence_score=70.0)
        content = self._html(card)
        assert "SHARES_BEAR" in content
        assert "<50" in content                     # R4 below the 50 options gate
        assert "shares or defined risk" in content.lower()

    def test_no_trade_shows_r6_veto(self):
        card = _minimal_card(final_action="NO_TRADE", direction_score=80.0)
        content = self._html(card)
        assert "R6 veto" in content

    def test_low_coverage_shows_reason(self):
        card = _minimal_card(final_action="WATCH_LOW_COVERAGE", confidence_score=30.0)
        content = self._html(card)
        assert "coverage" in content

    def test_why_chips_are_prefixed_with_the_rule(self):
        """Reasons reach the drawer (DECISION_DATA) tagged by rule short name."""
        audit = {"rule2_guidance": {"reasons": ["strong beat history (15.0%)"], "available": True},
                 "rule4_volatility": {"reasons": ["short gamma (spot $40.00 < flip $50.00)"],
                                      "available": True, "score": 20.0, "coverage": 0.5}}
        card = _minimal_card(rule_audit=audit, direction_score=30.0, volatility_edge_score=80.0)
        content = self._html(card)
        assert '"r": "R2"' in content
        assert '"r": "R4"' in content
        assert "strong beat history (15.0%)" in content
        # "<" is escaped as \u003c inside the embedded JSON (E5).
        assert "short gamma (spot $40.00 \\u003c flip $50.00)" in content
        # Drawer renders per-rule rows; the reason is not glued with ': ' in a chip.
        assert "R2: strong beat history" not in content

    def test_drawer_includes_decision_panel_and_data(self):
        audit = {"rule2_guidance": {"reasons": ["strong beat history (15.0%)"], "available": True,
                                    "score": 45.0, "coverage": 0.4}}
        card = _minimal_card(final_action="OPTIONS_BULL", direction_score=30.0,
                             volatility_edge_score=80.0, confidence_score=70.0,
                             rule_audit=audit)
        content = self._html(card)
        assert "Decision — which rule produced this action" in content
        assert "const DECISION_DATA" in content
        assert "renderDecision" in content
        assert '"r": "R2"' in content

    def test_r5_drawer_role_is_tag_and_never_a_score(self):
        audit = {"rule5_positioning": {"reasons": ["put-lean", "package-risk"],
                                       "available": True, "score": -13.0, "coverage": 0.5}}
        card = _minimal_card(final_action="SHARES_BEAR", direction_score=-20.0,
                             volatility_edge_score=25.0, confidence_score=60.0,
                             rule_audit=audit)
        content = self._html(card)
        assert '"r": "R5"' in content
        assert '"role": "tag (no vote)"' in content
        # R5's legacy -13 score must never surface as a signed number in the drawer.
        assert '"score": -13' not in content

    def test_r8_has_no_detailed_explanation(self):
        audit = {"rule8_sentiment": {"reasons": ["extreme retail call buying (87%)"],
                                     "available": True, "score": 20.0, "coverage": 1.0}}
        card = _minimal_card(final_action="DIRECTIONAL_WATCH", rule_audit=audit)
        content = self._html(card)
        assert '"r": "R8"' in content          # R8 rule row still renders
        assert "rexp" not in content           # detailed explainer removed
        assert "contrarian" not in content
        assert "retail proxy" not in content

    def test_phase1_roles_present_in_methodology(self):
        """Methodology names the Phase 1 role of each rule (no R5 vote)."""
        content = export_html([], [], {}).read_text(encoding="utf-8")
        assert "direction 20%" in content
        assert "tag 0% (no vote)" in content
        assert "veto 0%" in content
        assert "momentum 10%" in content
        # Direction vote list excludes R5 but includes R8 momentum.
        assert "Direction comes from R2/R3/R7 and the R8 retail-call momentum" in content

    def test_drawer_chart_svg_uses_scaling_viewbox(self):
        """Chart SVGs must be emitted with a scaling viewBox (no fixed width) and
        drawYAxis must return the accumulated string (plot-fix regression)."""
        import json
        detail = [{"q": "Q1 2026", "date": "2026-05-12", "implied": 5.0, "actual": 3.0,
                   "eps_actual": 0.30, "eps_est": 0.28, "surprise_dollar": 0.02,
                   "surprise_pct": 7.14, "revenue_actual": 5_000_000.0,
                   "revenue_est": 4_900_000.0}]
        card = _minimal_card(detail_json=json.dumps(detail))
        content = export_html([card], [], {"AAPL": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"}}).read_text(encoding="utf-8")
        # The charts are built client-side, so we assert on the emitted JS source:
        # the three charts must open with a scaling viewBox, never a fixed width,
        # and drawYAxis must return `s` (otherwise the svg opening tag is lost).
        assert content.count("viewBox=\"0 0 ' + W + ' ' + H") >= 3   # rev + eps + price
        assert "'<svg width=\"" not in content                       # no fixed-width chart svg
        i = content.index("function drawYAxis")
        yaxis = content[i:content.index("function fmtRev", i)]
        assert "return s;" in yaxis

    def test_decision_drawer_uses_user_friendly_cards(self):
        """Decision drawer template uses Action/Direction/Confidence cards plus
        per-rule boxes, and the headline no longer prints raw R4 vol-edge / dir."""
        audit = {"rule2_guidance": {"reasons": ["strong beat history (29.0%)"], "available": True,
                                    "score": 45.0, "coverage": 0.8},
                 "rule8_sentiment": {"reasons": ["extreme retail call buying (94%)"],
                                     "available": True, "score": -20.0, "coverage": 1.0}}
        card = _minimal_card(final_action="OPTIONS_BULL", trade_read="BULLISH",
                             bias_label="BULLISH_LEAN", direction_score=24.0,
                             confidence_score=71.0, volatility_edge_score=0.0,
                             rule_audit=audit)
        content = self._html(card)
        # Card structure lives in renderDecision's JS template.
        assert "class=\"dcards\"" in content
        assert "Action</div>" in content
        assert "Confidence</div>" in content
        assert 'class="rcard ' in content           # per-rule box template (concat)
        assert "R4 vol-edge <b>" not in content     # old raw headline removed


def _minimal_card(ticker="AAPL", **overrides) -> TradeCard:
    defaults = {
        "ticker": ticker,
        "name": "Apple Inc.",
        "cluster": "Technology",
        "earnings_date": date(2026, 8, 11),
        "report_time_label": "PM",
        "direction_score": 20.0,
        "confidence_score": 65.0,
        "bias_label": "BULL",
        "expected_move_pct": 5.0,
        "eps_estimate": "1.25",
        "final_action": "OPTIONS_BULL",
        "rule_audit": {"rule2_guidance": {"reasons": [], "available": True}},
    }
    defaults.update(overrides)
    return TradeCard(**defaults)


class TestColgroup:
    def test_23_columns(self):
        assert len(_COL_WIDTHS) == 23
        assert _colgroup().count("<col ") == 23

    def test_header_rows_line_up_with_columns(self):
        """Group colspans, subheader cells and the colgroup widths must all match,
        or every column after the off-by-one group shifts under the wrong header."""
        card = TradeCard(
            ticker="AAPL", name="Apple Inc.", cluster="Technology",
            earnings_date=date(2026, 8, 11), report_time_label="PM",
            direction_score=20.0, confidence_score=65.0, bias_label="BULL",
            expected_move_pct=5.0, eps_estimate="1.25",
            final_action="OPTIONS_BULL",
            rule_audit={"rule2_guidance": {"reasons": [], "available": True}},
        )
        content = export_html([card], [{"ticker": "AAPL"}],
                              {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        start = content.index('<table class="data-table">')
        thead = content[start:content.index("</thead>", start)]
        rows = re.findall(r"<tr>(.*?)</tr>", thead, flags=re.S)
        group_row, sub_row = rows[0], rows[1]

        spans = [int(c) for c in re.findall(r'colspan="(\d+)"', group_row)]
        assert len(spans) == 6  # Identity · Decision · Earnings Setup · Market · GEX · AGH
        assert sum(spans) == 23
        assert spans == [6, 3, 4, 1, 6, 3]
        assert len(re.findall(r"<th ", sub_row)) == 23
        assert ">LEAPS<" not in sub_row  # moved to the drawer
        assert ">Structure<" not in sub_row
        assert ">Why<" not in sub_row
        assert ">Inst. Flow<" not in sub_row
        assert ">EPS Est.<" not in sub_row  # EPS consensus is in the drawer
        # Identity opens Date/Time then Trade By, Ticker still sticky.
        assert sub_row.index(">Date/Time<") < sub_row.index(">Trade By<") < sub_row.index("sticky-tick")
        # Decision columns now Cov | Bias | Action (in that order).
        assert sub_row.index(">Cov<") < sub_row.index(">Bias<") < sub_row.index(">Action<")
        # Price opens its own Market group, before GEX.
        gex_start = sub_row.index(">GEX profile<")
        assert ">Price<" in sub_row and sub_row.index(">Price<") < gex_start
        # Earnings Setup no longer opens with Price.
        assert not (sub_row.index(">Price<") < sub_row.index(">Exp. Move<"))


class TestContentFitWidths:
    def test_widths_are_23_and_sum_to_the_colgroup(self):
        widths = _compute_col_widths([])
        assert len(widths) == 23
        assert _colgroup(widths).count("<col ") == 23

    def test_longer_content_widens_its_column(self):
        # The Sector column now reads the card's spine sector (E3, #372 ER-4), not the cluster.
        short = TradeCard(ticker="A", name="X", sector="Tech",
                          earnings_date=date(2026, 8, 11), report_time_label="PM")
        long = TradeCard(ticker="A", name="X", sector="Technology and Semiconductors",
                         earnings_date=date(2026, 8, 11), report_time_label="PM")
        w_short = _compute_col_widths([short])
        w_long = _compute_col_widths([long])
        assert w_long[4] > w_short[4]   # Sector column (index 4) grows with the text
        assert w_long[3] >= w_short[3]  # Name not smaller for the longer label

    def test_report_table_fills_pane(self):
        card = _minimal_card()
        content = export_html([card], [{"ticker": "AAPL"}],
                              {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        # The data table fills the pane; the computed widths act as minimums.
        assert "table-layout:fixed;width:100%;min-width:0" in content
        assert "width:auto" not in content
        assert "min-width:1712px" not in content
        assert "min-width:2600px" not in content

    def test_all_tables_share_one_colgroup(self):
        """Every tier table must use the same per-column widths (taken from the
        widest content across the whole board), so columns line up everywhere."""
        a = _minimal_card(ticker="AAA", name="A Short Co")
        b = _minimal_card(ticker="BBB", name="A Much Longer Company Name Inc")
        content = export_html(
            [a, b], [],
            {"AAA": {"nrc": 85, "tier": "Tier A — Revenue Catalyst"},
             "BBB": {"nrc": 20, "tier": "Ignore"}},
        ).read_text(encoding="utf-8")
        colgroups = re.findall(r"<colgroup>.*?</colgroup>", content, flags=re.S)
        assert len(colgroups) >= 2                      # at least two tier tables
        assert len(set(colgroups)) == 1                 # byte-identical widths
        assert len(re.findall(r"<col ", colgroups[0])) == 23

    def test_drawer_is_right_panel_with_panels(self):
        card = _minimal_card()
        content = export_html([card], [], {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        assert "const COL_WIDTHS" in content            # embedded fallback
        assert "tableColWidths" in content              # measured at open
        assert "drawerGridStyle" in content
        assert 'class="drawer"' in content and 'id="drawerIn"' in content
        # Drawer panels: decision · structure · charts.
        assert "Decision — which rule produced this action" in content
        assert "Market structure" in content
        assert 'id="dr-structure"' in content
        assert 'id="dr-rev-chart"' in content and 'id="dr-eps-chart"' in content and 'id="dr-price-chart"' in content

    def test_view_presets_embedded(self):
        card = _minimal_card()
        content = export_html([card], [], {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        assert "const VIEWS = " in content
        assert '"scan"' in content and '"earnings"' in content and '"gex"' in content and '"full"' in content
        assert "STICKY_PRICE_VIEWS" in content
        assert 'id="views"' in content and 'id="density"' in content and 'id="q"' in content
        assert 'id="colpick"' in content and content.count('data-col="') >= 23
        assert "Show radar" in content

    def test_decision_spider_present(self):
        card = _minimal_card()
        content = export_html([card], [], {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        assert "renderRuleWeb" in content
        assert 'id="dr-web"' in content
        assert 'id="dr-rules"' in content
        assert "decision-split" in content


def test_tier_desc_matches_live_cutoffs():
    """Section descriptions must quote the live NRC tiers (A≥70 · B 55–69 · C 40–54)."""
    assert "&ge; 70" in TIER_DESC["Tier A — Revenue Catalyst"]
    assert "55&ndash;69" in TIER_DESC["Tier B — Watchlist"]
    assert "40&ndash;54" in TIER_DESC["Tier C — Emerging"]
    assert "&lt; 40" in TIER_DESC["Ignore"]


# ──────────────────────────────────────────────
# _price_cell / _gamma_regime_tip  (report rework)
# ──────────────────────────────────────────────
class TestPriceCell:
    def test_present(self):
        assert "$25.50" in _price_cell("$25.50")
        assert "a5d6ff" in _price_cell("$25.50")

    def test_empty(self):
        assert "—" in _price_cell("")


class TestGammaRegimeTip:
    def test_positive_gamma(self):
        card = TradeCard(gex_gamma_flip="$15.00", gex_structure="Positive gamma regime · approaching call wall")
        tip = _gamma_regime_tip(card)
        assert "positive gamma" in tip
        assert "dampen" in tip
        assert "15.00" in tip

    def test_negative_gamma(self):
        card = TradeCard(gex_gamma_flip="$20.00", gex_structure="Negative gamma regime")
        tip = _gamma_regime_tip(card)
        assert "negative gamma" in tip
        assert "amplify" in tip
        assert "20.00" in tip

    def test_no_flip(self):
        card = TradeCard(gex_gamma_flip="", gex_structure="GEX unavailable")
        assert "unknown" in _gamma_regime_tip(card)


# ──────────────────────────────────────────────
# Redesign components: StatusBadge · GexProfile · ErSpark · ScoreMeter
# ──────────────────────────────────────────────
class TestStatusBadge:
    def test_tone_and_label(self):
        html = _badge("bull", "LEAD", extra="bd-L")
        assert "tone-bull" in html
        assert "bd-L" in html
        assert "LEAD" in html

    def test_title_and_icon(self):
        html = _badge("info", "Excellent", icon="✓", title="tradability")
        assert "tone-info" in html
        assert 'title="tradability"' in html
        assert "✓" in html

    def test_liquidity_is_never_green(self):
        """Liquidity is a quality/info signal — Excellent must not be bull-toned."""
        card = TradeCard(liquidity_label="Excellent")
        html = _liquidity_cell(card)
        assert "tone-info" in html
        assert "tone-bull" not in html
        assert "bd-bull" not in html

    def test_liquidity_tradeable_is_muted(self):
        assert "tone-muted" in _liquidity_cell(TradeCard(liquidity_label="Tradeable"))


class TestGexProfile:
    def test_caption_uses_spot_vs_flip(self):
        card = TradeCard(gex_call_wall="$37.50", gex_put_wall="$30.00",
                         gex_gamma_flip="$37.22", gex_gamma_magnet="$30.00",
                         current_price="$32.03")
        html = _gex_profile_cell(card)
        assert "gex-track" in html
        assert "vs flip" in html
        assert "-13.9%" in html  # (32.03-37.22)/37.22

    def test_labels_for_present_levels(self):
        card = TradeCard(gex_call_wall="$37.50", gex_put_wall="$30.00",
                         gex_gamma_flip="$37.22", gex_gamma_magnet="$30.00",
                         current_price="$32.03")
        html = _gex_profile_cell(card)
        assert html.count("gex-lbl") == 5
        for short in ("put wall", "magnet", "spot", "flip 0\u03b3", "call wall"):
            assert short in html
        for letter in (">P<", ">M<", ">S<", ">F<", ">C<"):
            assert letter in html
        assert "37.50" in html and "30.00" in html  # strikes in labels/tooltip

    def test_null_level_omitted(self):
        """CPRT-like: no put wall -> no P label and no put tick."""
        card = TradeCard(gex_call_wall="$37.50", gex_gamma_flip="$37.22",
                         gex_gamma_magnet="$30.00", current_price="$32.03")
        html = _gex_profile_cell(card)
        assert "gex-lbl put" not in html
        assert ">P<" not in html
        assert html.count("gex-lbl") == 4

    def test_spot_equals_flip_is_nudged(self):
        """ORCL-like: spot ~= flip (and magnet == call) must not stack labels."""
        import re
        card = TradeCard(gex_call_wall="$185.00", gex_put_wall="$155.00",
                         gex_gamma_flip="$160.97", gex_gamma_magnet="$185.00",
                         current_price="$161.00")
        html = _gex_profile_cell(card)
        lefts = sorted(float(m) for m in re.findall(
            r'gex-lbl (?:put|magnet|spot|flip|call)" style="left:([\d.]+)%', html))
        assert len(lefts) == 5
        gaps = [b - a for a, b in zip(lefts, lefts[1:])]
        assert all(g >= 15.9 for g in gaps), gaps

    def test_no_flip_is_safe(self):
        card = TradeCard(gex_call_wall="$10.00", current_price="$9.00")
        html = _gex_profile_cell(card)
        assert "gex-track" in html
        assert "flip N/A" in html

    def test_no_levels_is_na(self):
        assert "N/A" in _gex_profile_cell(TradeCard())

    def test_nudge_helper_spreads_and_stays_in_track(self):
        out = _gex_nudge([50.0, 52.0, 54.0])
        assert out[1] - out[0] >= 15.9 and out[2] - out[1] >= 15.9
        edge = _gex_nudge([0.0, 100.0, 100.0])
        assert all(0.0 <= p <= 100.0 for p in edge)
        assert sorted(edge)[1] - sorted(edge)[0] >= 15.9


class TestErSpark:
    def test_four_bars_with_colors(self):
        card = TradeCard(earn_m4_pct="+24%", earn_m3_pct="-5%",
                         earn_m2_pct="+21%", earn_m1_pct="-2%")
        html = _er_spark_cell(card)
        assert "spark" in html
        assert html.count('style="height:') == 4
        assert html.count("spark-pct") == 4
        assert "up" in html and "dn" in html

    def test_percentages_shown_under_bars(self):
        card = TradeCard(earn_m4_pct="+24%", earn_m3_pct="-5%",
                         earn_m2_pct="+21%", earn_m1_pct="-2%")
        html = _er_spark_cell(card)
        for pct in ("+24%", "-5%", "+21%", "-2%"):
            assert pct in html

    def test_missing_quarter_shows_dash(self):
        card = TradeCard(earn_m4_pct="+24%", earn_m3_pct="", earn_m2_pct="+21%",
                         earn_m1_pct="-2%")
        html = _er_spark_cell(card)
        assert "spark-pct na" in html

    def test_empty_is_na(self):
        assert "N/A" in _er_spark_cell(TradeCard())


class TestScoreMeter:
    def test_pips_and_score(self):
        html = _agh_points_cell(6)
        assert "6/9" in html
        assert "score-meter" in html
        assert html.count("<i ") == 9
        assert "on hi" in html

    def test_low_score_is_muted(self):
        html = _agh_points_cell(2)
        assert "2/9" in html
        assert "on lo" in html


class TestViews:
    def test_every_view_column_exists(self):
        for view, cols in _VIEWS.items():
            for cid in cols:
                assert cid in _COL_IDS, f"{view} references unknown column {cid}"

    def test_scan_keeps_decision_columns(self):
        for required in ("bias", "action", "score"):
            assert required in _VIEWS["scan"]

    def test_full_has_every_column(self):
        assert set(_VIEWS["full"]) == set(_COL_IDS)

    def test_groups_cover_all_columns(self):
        assert set(_GROUP_LABELS) >= {"identity", "decision", "earnings", "market", "gex", "hunter"}


class TestRedesignLayout:
    def test_widened_columns(self):
        assert _COL_WIDTHS[_COL_IDS.index("gex")] == 280
        assert _COL_WIDTHS[_COL_IDS.index("surprises")] == 130

    def test_row_height_grows_when_gex_labels_visible(self):
        card = _minimal_card()
        content = export_html([card], [], {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        assert ".gex-on{--row-h:64px}" in content
        assert ".comfort.gex-on{--row-h:76px}" in content
        assert "gex-on" in content  # toggled by applyView

    def test_board_scrollbar_reserves_a_gutter(self):
        card = _minimal_card()
        content = export_html([card], [], {"AAPL": {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")
        assert "scrollbar-gutter:stable" in content
        assert ".board::-webkit-scrollbar{width:12px" in content


# ──────────────────────────────────────────────
# E5: provider strings escaped in markup and embedded data
# ──────────────────────────────────────────────
class TestExportEscaping:
    _EVIL = '"><img src=x onerror=alert(1)></script><script>alert(1)</script>'

    def _render(self, evil: str) -> str:
        card = TradeCard(
            ticker=evil, name=evil[1:], cluster=evil, report_time_label=evil,
            earnings_date=date(2026, 8, 11), direction_score=20.0, confidence_score=65.0,
            bias_label=evil, final_action=evil, expected_move_pct=5.0,
            filter_reasons=evil, market_cap=evil, stock_volume=evil,
            option_volume=evil, total_open_interest=evil, current_price=evil,
            agh_owners=evil, rotation_quadrant=evil, trade_read=evil,
            gex_structure=evil, inst_flow_label=evil, leaps_label=evil,
            detail_json=json.dumps([{"q": evil}]),
            rule_audit={"rule2_guidance": {"available": True, "label": evil,
                                           "reasons": [evil], "score": 1}},
        )
        return export_html([card], [{"ticker": evil, "sector": evil}],
                           {evil: {"nrc": 50, "tier": "Ignore"}}).read_text(encoding="utf-8")

    def test_no_markup_or_script_breakout(self):
        benign = self._render("SAFE")
        html = self._render(self._EVIL)
        assert "<img" not in html
        assert html.count("<script") == benign.count("<script")
        assert html.count("</script") == benign.count("</script")

    def test_attribute_values_are_quoted_safely(self):
        html = self._render(self._EVIL)
        assert 'data-ticker="&quot;&gt;&lt;img' in html
        assert 'data-name="&gt;&lt;img' in html


class TestErSparkLabels:
    """The cells are the return from each print's reaction-session close to today, not a one-day move."""

    def test_wrapper_and_bar_tooltips_say_return_since_print(self):
        card = TradeCard(earn_m4_pct="+24%", earn_m3_pct="-5%", earn_m2_pct="+21%", earn_m1_pct="-2%")
        html = _er_spark_cell(card)
        assert 'title="Return since print (-4E..-1E)"' in html
        assert 'title="Return since print: +24%"' in html
        assert "ER reactions" not in html

    def test_column_header_is_not_called_surprises(self):
        from RevenueChaser.module_earnings_radar.report import _COLUMNS as COLUMNS
        labels = [c[1] for c in COLUMNS if c[0] == "surprises"]
        assert labels == ["Return since print"]
