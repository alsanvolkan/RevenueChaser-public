"""A3 (Fable report review, 5 Nov 2026): the 22:00 close check, in the 02-06 Nov order.

1. Which scenario band the close sits in. 2. VIX against its threshold. 3. The week-gamma sign and the close against
the turning point. Without MenthorQ's VIX HVL the threshold is a stated substitute: the prior week's last VIX close
plus the average one-day VIX move over the stored window.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_spx_gamma import close_check, store_reads
from . import rcdata_fixture as fx

HIST = [{"date": "2026-10-28", "close": 18.0}, {"date": "2026-10-29", "close": 19.0},
        {"date": "2026-10-30", "close": 18.5}, {"date": "2026-11-02", "close": 19.5},
        {"date": "2026-11-03", "close": 21.0}, {"date": "2026-11-04", "close": 20.0}]


def test_vix_threshold_substitute():
    t = close_check.vix_threshold(HIST, "2026-11-04")
    # prior week's last close 18.5 (Fri 30 Oct); mean |daily change| = (1 + .5 + 1 + 1.5 + 1) / 5 = 1.0
    assert t["base"] == 18.5 and t["base_date"] == "2026-10-30"
    assert t["one_day_move"] == pytest.approx(1.0)
    assert t["threshold"] == pytest.approx(19.5)


def test_close_card_in_report_order():
    snap = {"current_level": 7580.0, "asof_date": "2026-11-04",
            "levels": {"regime_threshold": 7750.0, "upper_resistance_band": 8100.0, "lower_support_band": 7500.0},
            "charm_pin_report": {"strike": 7800.0},
            "vix": {"vix": 20.0, "history": HIST},
            "week_gamma": {"net_gamma": -120.0, "turning_point": 7700.0, "target": "2026-11-06"}}
    card = close_check.close_card(snap)
    steps = [s["step"] for s in card["steps"]]
    assert steps == ["Price band", "VIX threshold", "Week gamma"]
    price, vix, gamma = card["steps"]
    assert "Consolidation" in price["reading"] and "7,500" in price["reading"]
    assert "above" in vix["reading"] and "19.50" in vix["reading"] and vix["state"] == "stress"
    assert "negative" in gamma["reading"] and "below the turning point 7,700" in gamma["reading"]
    assert "substitute" in card["note"]


def test_close_card_degrades_without_inputs():
    card = close_check.close_card({"current_level": None})
    assert all(s["reading"].endswith("n/a") for s in card["steps"])


def test_vix_family_keeps_its_history(rcdata_market):
    out = store_reads.vix_family(fx.LAST)
    hist = out["payload"]["history"]
    assert hist and hist[-1]["date"] == "2026-10-26"
    assert [h["date"] for h in hist] == sorted(h["date"] for h in hist)


def test_page_shows_the_close_check():
    from RevenueChaser.module_spx_gamma import render
    from .conftest import make_snapshot
    html = render.render_html(make_snapshot())
    assert "Close check" in html and "Price band" in html and "Week gamma" in html
