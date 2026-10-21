"""Tests for the rendered Markdown/HTML section."""
from __future__ import annotations

from RevenueChaser.module_spx_gamma import render
from .conftest import make_snapshot

LEGACY = []

REQUIRED = [
    "Summary",
    "SPX Option Position Map",
    "Details",
    "How to read this map (Gamma)",
    "Delta Map — Walkthrough",
    "Call Wall",
    "Put Wall",
    "Gamma Flip",
    "Gamma Magnet",
    "0DTE Gamma Concentration",
    "0DTE Put Concentration",
    "0DTE Cumulative GEX Crossing",
    "Vanna Call Peak",
    "Vanna Put Peak",
    "One-Day Implied Range (1σ)",
    "Provider GEX Level",
    "High-GEX Concentration",
    "Directionalized Volume",
]


def test_markdown_details_table_and_no_legacy():
    md = render.render_markdown(make_snapshot())
    for t in REQUIRED:
        assert t in md, t
    for legacy in LEGACY:
        assert legacy not in md, f"legacy term leaked: {legacy}"
    assert "Unusual Whales API" in md


def test_html_details_table_and_no_legacy():
    html = render.render_html(make_snapshot())
    for t in REQUIRED:
        assert t in html, t
    for legacy in LEGACY:
        assert legacy not in html, f"legacy term leaked: {legacy}"


def test_charm_peak_not_mislabeled_as_pin():
    md = render.render_markdown(make_snapshot())
    assert "Charm Peak" in md  # conftest charm is far from spot -> Peak, not Pin
    assert "Charm Pin" not in md


def test_neutral_wording_no_resistance_support():
    md = render.render_markdown(make_snapshot())
    assert "Resistance Barrier" not in md
    assert "Support Barrier" not in md
    assert "Attraction Pin" not in md


def test_vanna_note_in_footer():
    md = render.render_markdown(make_snapshot())
    assert "vanna" in md.lower()
    assert "cushion during vol spikes" in md or "drag during vol spikes" in md
    assert "support guarantee" in md  # explicitly disclaimed


def test_vanna_rows_disclose_scaling_window():
    md = render.render_markdown(make_snapshot())
    assert "±25% of the as-of spot" in md
    assert "re-derived per report date" in md


def test_summary_cards_present():
    md = render.render_markdown(make_snapshot())
    for t in ("Delta — TREND", "Gamma — MAGNET", "Vanna — VOLATILITY", "Charm — TIMER",
              "Net dealer Δ", "Net DEX", "Above spot", "Below spot",
              "Net dealer Γ", "Top wall", "Flip distance",
              "Net Vanna", "Top vol-sensitive strike",
              "Live charm", "Positioning", "Where it matters"):
        assert t in md, t


def test_at_a_glance_strip():
    md = render.render_markdown(make_snapshot())
    assert "At a glance" in md or "Delta Bullish" in md


def test_signal_labels_present():
    md = render.render_markdown(make_snapshot())
    assert "Bullish" in md
    assert "Amplifying" in md


def test_flip_diagram_and_crossing():
    md = render.render_markdown(make_snapshot())
    assert "Positive Gamma Zone" in md
    assert "Negative Gamma Zone" in md
    assert "The Volatility Turning Point" in md
    assert "Danger zone" in md
    assert "Safety zone" in md


def test_header_shows_provenance():
    md = render.render_markdown(make_snapshot())
    assert "Data retrieved " in md
    assert "from Unusual Whales API" in md
    assert "Price yfinance ^GSPC" in md
    # duplicated fields are no longer in the header line
    assert "Regime Long Gamma — Dampening" not in md
    assert "Charm: Directionalized Volume" not in md
    assert "Vanna: Directionalized Volume" not in md


def test_delta_walkthrough_present():
    md = render.render_markdown(make_snapshot())
    assert "Spot trades at" in md
    assert "Call Wall" in md
    assert "liquidity pockets" in md


def test_v3_panel_headings_present():
    md = render.render_markdown(make_snapshot())
    for t in ("Volatility Board", "Options Volume & P/C", "Max Pain", "CFTC COT"):
        assert t in md, t


def test_v3_panel_how_to_read_notes():
    snap = make_snapshot()
    snap["max_pain"] = [{"expiry": "2026-09-18", "max_pain": 7470.0, "distance_pct": -3.13}]
    snap["vix"] = {"vix": 14.43, "vix9d": 11.22, "vix3m": 17.48, "vix6m": 20.3, "vix1y": 22.16,
                   "vvix": 86.63, "term_mode": "contango"}
    snap["iv_rank"] = {"date": "2026-08-28", "iv_rank_1y": 5.6, "volatility": 0.128}
    snap["options_volume"] = {"date": "2026-08-28", "call_volume": 171049, "put_volume": 260424,
                              "pc_ratio": 1.52, "net_call_premium": 123848260.0,
                              "net_put_premium": 35089636.0}
    snap["cftc"] = {"report_date": "2026-08-25", "open_interest": 2045669,
                    "rows": [{"group": "Asset Manager", "long": 1, "short": 2, "net": -1, "change": 0}]}
    html = render.render_html(snap)
    for phrase in ("whether implied vol is cheap", "structural gravity center",
                   "how big players (asset managers"):
        assert phrase in html, phrase


def test_v3_missing_data_degrades():
    snap = make_snapshot()
    snap["vix"] = None
    snap["iv_rank"] = None
    snap["options_volume"] = None
    snap["max_pain"] = []
    snap["cftc"] = None
    md = render.render_markdown(snap)
    assert "data unavailable" in md.lower()


def test_skew_derived_unverified_flag_renders():
    snap = make_snapshot()
    snap["skew"] = {
        "put_bias_1m": 0.004, "put_bias_3m": 0.002,
        "derived_unverified": True,
        "note": "Derived from UW /interpolated-iv twin blocks",
    }
    md = render.render_markdown(snap)
    assert "derived/unverified" in md


def test_flip_methods_and_dte_table():
    snap = make_snapshot()
    md = render.render_markdown(snap)
    assert "Gamma flip methods" in md
    assert "DTE Band Walls" in md
    assert "(Put Wall standard)" in md
    ctx = render.build_context(snap)
    assert len(ctx["dte_band_walls"]) == 6


