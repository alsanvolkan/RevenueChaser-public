"""Relatedness board: one provider string cannot blank the script (ADR-X1 F3, #144).

LLM theme names and profiles are the likeliest carriers of ``<!--`` or of a
string that spells a placeholder.
"""
from __future__ import annotations

from RevenueChaser.core.tests.board_js import PROBE, assert_scripts_intact
from RevenueChaser.module_relatedness import config, report


def test_a_comment_opener_or_placeholder_in_a_name_keeps_the_script_intact(tmp_path):
    co = lambda t, fine: {"ticker": t, "name": PROBE, "sector": "Technology", "industry": PROBE,
                          "market_cap": 1e12, "weights": {"gpu": 1.0},
                          "profile": {"business_summary": PROBE, "products": [PROBE]},
                          "theme_coarse": PROBE, "theme_medium": "Chips", "theme_fine": fine}
    p = {"as_of": "2026-09-19T00:00:00+00:00", "model": "deepseek-flash", "n": 2,
         "companies": [co("NVDA", PROBE), co("AMD", "CPUs")],
         "peers": {"NVDA": [{"peer": "AMD", "name": PROBE, "score": 1.0, "shared": [PROBE],
                             "percentile": 100}],
                   "AMD": [{"peer": "NVDA", "name": PROBE, "score": 1.0, "shared": ["gpu"],
                            "percentile": 100}]},
         "scores": [1.0], "thinking_compare": {}, "fields": list(config.PROFILE_FIELDS)}
    assert_scripts_intact(report.render_relatedness_dashboard(p), tmp_path)
