"""#527 step 5 (avo 6 Nov 2026): Health right now answers "do I need to act now?". Only problem cards show; every
fine thing folds into one "all good" line; the full tiles stay one tap away."""
from __future__ import annotations

import copy

from RevenueChaser.module_settings import control, control_render
from RevenueChaser.module_settings.tests.test_control_render_c1 import _view
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd


def _alerts(nd=None, **kw):
    return {a["key"]: a for a in control.build(nd or control_nd(), now=NOW, **kw)["health"]["alerts"]}


def test_a_stale_dataset_is_a_card_that_names_it():
    a = _alerts()
    assert a["stale"]["cls"] == "bad" and "market_series" in a["stale"]["label"]


def test_a_provider_past_80_percent_of_its_cap_is_a_card_and_below_is_not():
    nd = control_nd()
    assert "cap" not in _alerts(nd), "uw at 60 % is fine"
    nd["spend"]["providers"] = [{"provider": "uw", "cap": 100000, "today": 85_000, "week": 1}]
    a = _alerts(nd)["cap"]
    assert a["cls"] == "warn" and "Unusual Whales" in a["label"] and a["n"] == "85%"


def test_a_logged_out_gateway_a_silent_builder_and_an_old_backup_are_cards():
    nd = control_nd()
    nd["verified"]["silent_minutes"] = 95
    nd["backups"] = {"kinds": [{"kind": "market", "newest": "2026-10-27", "age_days": 3, "warn": True}]}
    a = _alerts(nd, ibkr={"state": "down", "text": "no session"})
    assert a["gateway"]["cls"] == "bad" and a["builder"]["cls"] == "bad" and a["backup"]["cls"] == "bad"
    fine = _alerts(ibkr={"state": "up"})
    assert not {"gateway", "builder"} & set(fine)


def test_the_backup_switch_off_is_a_red_card_and_fetch_off_is_not_a_card():
    a = _alerts(switches=lambda: {"backup": {"name": "backup", "off_at": NOW.timestamp(), "off_by": "avo"},
                                  "fetch": {"name": "fetch", "off_at": NOW.timestamp(), "off_by": "avo"}})
    assert a["backup_off"]["cls"] == "bad" and "avo" in a["backup_off"]["label"]
    assert "fetch_off" not in a, "the top bar already says it"


def test_prices_behind_is_one_card_and_its_dataset_is_not_named_twice():
    nd = control_nd()
    for c in nd["datasets"]:
        if c["dataset"] == control.bars_dataset():
            c["status"] = "stale"
            break
    else:
        nd["datasets"].append(dict(nd["datasets"][-1], dataset=control.bars_dataset(), status="stale"))
    a = _alerts(nd)
    assert a["prices"]["cls"] == "bad" and a["prices"]["label"].startswith("prices behind · last session")
    assert control.bars_dataset() not in a["stale"]["label"]


def test_the_page_shows_alert_cards_an_all_good_line_and_the_tiles_folded():
    v = _view()
    v["health"]["alerts"] = [{"key": "stale", "cls": "bad", "n": "1", "label": "stale: market_series"}]
    html = control_render.render_data(v)
    sec = html[html.index('id="sec-health"'):html.index("</details>", html.index('id="sec-health"'))]
    assert 'class="alerts"' in sec and "stale: market_series" in sec
    assert 'class="allgood"' in sec
    assert '<details class="htiles"' in sec and "<summary>Every tile</summary>" in sec
    v["health"]["alerts"] = []
    quiet = control_render.render_data(v)
    assert "Nothing needs you right now" in quiet


def test_the_timer_off_on_purpose_is_a_grey_words_line_not_a_card():
    v = control.build(control_nd(), now=NOW, timer=lambda: {"state": "off", "last": None, "next": None,
                                                            "minute": 7})
    assert "timer" not in {a["key"] for a in v["health"]["alerts"]}
    assert "builder timer off (on purpose)" in v["health"]["allgood"]
