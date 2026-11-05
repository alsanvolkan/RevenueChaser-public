"""The board follows the approved design mock (avo, 5 Nov 2026: "mock da gosterdigin sey ile alakasi yok").

PR 419 added the report's sections but kept the old look: tables of STEP / READING rows. The mock
(``/mnt/project-files/spx_gamma/spx-gamma-board-mock.html``, approved 5 Nov 07:31 Amsterdam) reads top to bottom
as: header chips, the 22:00 decision as numbered steps, a hero spot with a level grid, the regime list, scenario
cards with probability bars, the week gamma chart with its zones, the trade structures as cards with leg chips,
and every detail group collapsed. These tests pin that structure, not the numbers (other tests own those).
"""
from __future__ import annotations

import re

import pytest

from RevenueChaser.module_spx_gamma import render, run, week_gamma
from .conftest import make_snapshot


@pytest.fixture(autouse=True)
def _no_local_state(monkeypatch):
    monkeypatch.setattr(run, "_load_history", lambda *a, **k: {})
    monkeypatch.setattr(render, "scorecard", lambda *a, **k: {
        "average": None, "n": 0, "verdict": "No scored weeks yet", "calibrated": None, "records": []})


def _html(snap=None) -> str:
    return render.render_html(snap or make_snapshot())


def test_week_map_carries_the_bucketed_bars_of_its_window():
    rows = [{"expiry": "2026-11-06", "strike": float(k), "net_gamma": float(g)}
            for k, g in ((7500, -40), (7525, -30), (7550, 10), (7600, 30), (7605, 5), (7625, 80), (9000, 7))]
    m = week_gamma.week_map(rows, "2026-11-04", 7580.0)
    # 25-point buckets inside +-5 % of spot (9000 is outside); 7600 and 7605 share a bucket
    assert m["bars"] == [(7500.0, -40.0), (7525.0, -30.0), (7550.0, 10.0), (7600.0, 35.0), (7625.0, 80.0)]


def test_header_has_the_regime_chip_and_no_old_report_strip():
    html = _html()
    assert 'class="chips"' in html
    assert re.search(r'class="chip (neg|pos)"[^>]*>[^<]*gamma', html)
    assert 'class="group-h"' not in html           # the old numbered group labels ("1 · DECISION")


def test_decision_is_three_numbered_steps_and_a_verdict_box():
    html = _html()
    sec = html.split('id="h-dec"', 1)[1].split("</section>", 1)[0]
    assert len(re.findall(r'class="step( on)?"', sec)) == 3
    assert '<span class="i">1</span>' in sec and '<span class="i">3</span>' in sec
    assert 'class="decide"' in sec
    assert "STEP" not in sec and "<table" not in sec


def test_glance_is_a_hero_spot_and_an_eight_cell_grid():
    html = _html()
    sec = html.split('id="h-glance"', 1)[1].split("</section>", 1)[0]
    assert 'class="px n"' in sec and "7,784" in sec
    assert sec.count('class="cell"') == 8
    for label in ("HVL (flip)", "Call wall", "Put wall", "Magnet", "IV30", "Straddle", "Net premium", "Put/call volume"):
        assert label in sec, label
    assert "to HVL" in sec                         # spot's distance to the flip beside the hero number


def test_regime_conditions_are_a_list_not_a_table():
    sec = _html().split('id="h-reg"', 1)[1].split("</section>", 1)[0]
    assert sec.count("class=\"li col\"") == 4 and "<table" not in sec


def test_scenarios_are_cards_with_probability_bars():
    sec = _html().split('id="h-sc"', 1)[1].split("</section>", 1)[0]
    assert sec.count('class="sc"') == 3
    assert "width:35%" in sec and "width:30%" in sec
    assert "Brier" in sec


def test_week_gamma_is_an_svg_bar_chart_with_spot_and_zone_tags():
    sec = _html().split('id="h-wk"', 1)[1].split("</section>", 1)[0]
    assert '<svg class="chart"' in sec and sec.count("<rect") >= 3
    assert "spot 7,784" in sec
    assert 'class="tag' in sec and "Air gap" in sec


def test_structures_are_cards_with_leg_chips(monkeypatch):
    monkeypatch.setattr(render, "_structures", lambda snap: {
        "expiry": "2026-08-21", "label": "Report structure, not advice", "live": True,
        "rows": [{"name": "Iron condor", "family": "A", "legs": "+PUT 7,575 / −PUT 7,600 / −CALL 7,800 / +CALL 7,825",
                  "leg_chips": [{"text": "+7575P", "long": True}, {"text": "−7600P", "long": False},
                                {"text": "−7800C", "long": False}, {"text": "+7825C", "long": True}],
                  "net": "credit 4.04", "xsp": "0.40", "max_profit": "4.04", "max_loss": "20.96",
                  "breakevens": "7,596.0, 7,804.0", "reward_risk": "0.19", "missing": ""}]})
    sec = _html().split('id="h-tr"', 1)[1].split("</section>", 1)[0]
    assert sec.count('class="trade"') >= 1
    assert '<span class="leg l">+7575P</span>' in sec and '<span class="leg s">−7600P</span>' in sec
    assert "not advice" in sec


def test_structure_rows_carry_leg_chips():
    from RevenueChaser.module_spx_gamma.structures import build_structures   # noqa: F401  (shape owner)
    chips = render._leg_chips([{"action": "BUY", "type": "PUT", "strike": 7575.0},
                               {"action": "SELL", "type": "CALL", "strike": 7800.0}])
    assert chips == [{"text": "+7575P", "long": True}, {"text": "−7800C", "long": False}]


def test_detail_groups_are_collapsed_cards():
    html = _html()
    for gid in ("g7", "g8", "g9", "g10", "g11"):
        m = re.search(r'<details class="card[^"]*"[^>]*id="%s"' % gid, html)
        assert m, gid
        assert " open" not in m.group(0)


def test_palette_has_light_and_dark_and_44px_targets():
    html = _html()
    assert ':root[data-theme="light"]' in html and '[data-theme="auto"]' in html
    assert "min-height:48px" in html               # the collapsed cards' summary rows


def test_glance_cells_read_the_days_option_figures():
    snap = make_snapshot()
    snap.update({"realized_vol": {"implied_volatility": 0.142, "realized_volatility": 0.1},
                 "iv_rank": {"iv_rank_1y": 44.2},
                 "straddle_move": {"points": 66.6, "expiry": "2026-08-21", "strike": 7785.0},
                 "options_volume": {"call_volume": 283000.0, "put_volume": 327000.0, "pc_ratio": 1.1555,
                                    "net_call_premium": -150e6, "net_put_premium": 253e6}})
    cells = {c["label"]: c for c in render.glance(snap, {})["cells"]}
    assert cells["IV30"]["value"] == "14.2%" and cells["IV30"]["note"] == "IV rank 44.2"
    assert cells["Straddle"]["value"] == "±66.6" and cells["Straddle"]["note"] == "2026-08-21 at 7,785"
    assert cells["Net premium"]["value"] == "-403.0M" and cells["Net premium"]["tone"] == "neg"
    assert cells["Put/call volume"]["value"] == "1.16" and cells["Put/call volume"]["note"] == "327.0K / 283.0K"
    hist = {c["label"]: c for c in render.glance(snap, {"options_volume": True, "straddle": True})["cells"]}
    assert hist["Net premium"]["value"] == "n/a (historical)" and hist["Straddle"]["value"] == "n/a (historical)"
