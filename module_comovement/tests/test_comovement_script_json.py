"""Co-movement board: one provider string cannot blank the script (ADR-X1 F3, #144).

``<!--<script>`` in a label made the tokenizer swallow the board's own
``</script>``; a label spelling ``__META__`` was substituted again. The industry text
(a provider string, and what a group is named after since #266) is hostile too. Kept out of
``test_comovement.py`` so the lane does not touch that file.
"""
from __future__ import annotations

from RevenueChaser.core.tests.board_js import PROBE, assert_scripts_intact
from RevenueChaser.module_comovement import report


def _member(t: str) -> dict:
    return {"ticker": t, "name": PROBE, "sector": "Financial Services", "market_cap": 1e9,
            "theme_ids": ["t1"], "industry": PROBE,
            "gics": ["Financial Services", PROBE], "index": ["S&P 500"]}


def test_a_comment_opener_or_placeholder_in_a_label_keeps_the_script_intact(tmp_path):
    p = {"as_of": "2026-09-17T00:00:00+00:00", "eligible": 2, "universe_total": 2,
         "cuts": [20], "fetch": {}, "horizon": 5, "benchmark": "spy",
         "benchmark_applied": True,
         "theme_catalog": {"t1": {"label": PROBE, "level": "fine"}},
         "prices_through": "2026-09-17", "benchmark_status": "applied", "oos_note": PROBE,
         "clusters": {"20": [{"id": "1", "label": PROBE, "n": 2, "mean_corr": 0.72,
                              "last_residual_pct": -3.4,
                              "members": [_member("AAA"), _member("BBB")]}]}}
    assert_scripts_intact(report.render_comovement_dashboard(p), tmp_path)


def test_the_last_move_is_labelled_as_log_points_not_percent():
    """DF56: ``last_residual_pct`` is a mean of log returns x 100, so the card must not call it a percent."""
    from RevenueChaser.module_comovement import report as rep
    src = rep.render_comovement_dashboard({
        "as_of": "2026-09-17T00:00:00+00:00", "eligible": 2, "universe_total": 2, "cuts": [20], "fetch": {},
        "horizon": 5, "benchmark": "spy", "benchmark_applied": True, "theme_catalog": {},
        "prices_through": "2026-09-17", "benchmark_status": "applied",
        "clusters": {"20": [{"id": "1", "label": "x", "n": 2, "mean_corr": 0.7, "last_residual_pct": -3.4,
                             "members": [_member("AAA"), _member("BBB")]}]}})
    assert "toFixed(1) + ' log pts '" in src
    assert "toFixed(1) + '% '" not in src
