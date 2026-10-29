"""XS bundle (#49 Market rows, Earnings): DF05 and the failed-flow drawer row.

DF05: ``TradeCard.rotation_score`` is never assigned (``test_cleanup_199``), so the quadrant badge took a score it
never used. The field stays, because the stored card (``history._row``, ``dataclasses.asdict``) carries it.
Flow: a failed UW flow request already reads "Flow unavailable (provider failure)" (#166), but the drawer still
printed "· $0 net" after it, a number the run never measured.
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import date

from RevenueChaser.module_earnings_radar import report
from RevenueChaser.module_earnings_radar.models import TradeCard


def _card(**kw) -> TradeCard:
    base = dict(ticker="AAPL", name="Apple Inc.", cluster="Technology", earnings_date=date(2026, 8, 11),
                report_time_label="PM", direction_score=20.0, confidence_score=65.0, bias_label="BULL",
                expected_move_pct=5.0, final_action="OPTIONS_BULL",
                rule_audit={"rule2_guidance": {"reasons": [], "available": True}})
    base.update(kw)
    return TradeCard(**base)


def _decision(html: str) -> dict:
    return json.loads(re.search(r"const DECISION_DATA = (\{.*?\});</script>", html, re.S).group(1))


def test_the_quadrant_badge_takes_no_score():
    assert list(inspect.signature(report._quadrant_badge).parameters) == ["quadrant"]
    assert "rotation_score" not in inspect.getsource(report)


def _structure(card, tmp_path) -> str:
    """The drawer's structure card, rendered by the page's own script under node."""
    from RevenueChaser.core.tests.board_js import run_board_js
    html = report.export_html([card], [], {}).read_text(encoding="utf-8")
    out = run_board_js(html, "CUR_DETAIL.ticker = 'AAPL'; renderDecision();"
                             "console.log(JSON.stringify({s: _els['dr-structure'].innerHTML}))", tmp_path)
    return out["s"]


def test_the_drawer_knows_the_flow_request_failed(tmp_path):
    card = _card(inst_flow_label="Flow unavailable (provider failure)", flow_source="failed")
    html = report.export_html([card], [], {}).read_text(encoding="utf-8")
    assert _decision(html)["AAPL"]["flow_source"] == "failed"
    shown = _structure(card, tmp_path)
    assert "Flow unavailable (provider failure)" in shown and "net" not in shown


def test_a_measured_flow_still_shows_its_net(tmp_path):
    shown = _structure(_card(inst_flow_label="Institutional bullish lean", inst_flow_net=2_500_000.0,
                             flow_source="flow_recent"), tmp_path)
    assert "Institutional bullish lean" in shown and "net" in shown


def test_no_lean_shows_no_zero_net(tmp_path):
    """Review S4: a tape without a print side has no lean, so the drawer prints no "· $0 net" after its label."""
    shown = _structure(_card(inst_flow_label="Lean unavailable (no print side)", inst_flow_net=0.0,
                             flow_source="flow_recent"), tmp_path)
    assert "Lean unavailable (no print side)" in shown and "net" not in shown
