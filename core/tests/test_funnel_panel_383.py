"""#383 C16: one shared funnel panel — Asymmetric Growth adopts it (Swing / Fundamentals do not, yet).

``funnel_panel_html`` renders a validated funnel as the collapsible panel; an empty or invalid funnel has no
panel, and every string goes through the shared escaper.
"""
from __future__ import annotations

from RevenueChaser.core import funnel


def _steps():
    S = funnel.step
    return [S("universe", "Universe", 3, "start"),
            S("no_annual", "No annual financials", 2, "drop", by_design=False, tickers=["AAA", "BBB"]),
            S("scored", "Scored", 1, "result")]


def test_empty_or_invalid_steps_make_no_panel():
    assert funnel.funnel_panel_html([]) == ""
    assert funnel.funnel_panel_html(None) == ""
    bad = _steps()
    bad[-1]["count"] = 2                       # 3 - 2 = 1, not 2: does not add up
    assert funnel.funnel_panel_html(bad) == ""


def test_a_hostile_label_and_ticker_are_escaped():
    steps = _steps()
    steps[1]["label"] = '<img src=x onerror=alert(1)>'
    steps[1]["tickers"] = ["<script>alert(2)</script>"]
    out = funnel.funnel_panel_html(steps)
    assert "<img" not in out and "<script" not in out
    assert "&lt;img src=x" in out


def test_the_panel_id_and_summary_are_configurable():
    out = funnel.funnel_panel_html(_steps(), panel_id="my-funnel", summary="Where names drop")
    assert 'id="my-funnel"' in out and "<summary>Where names drop</summary>" in out
    assert 'id="funnel-panel"' in funnel.funnel_panel_html(_steps())
