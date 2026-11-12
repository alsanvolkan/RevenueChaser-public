"""#527 step 4: the top bar, always above Health right now: the fetch switch, the backup switch and the IBKR
gateway's state (moved up from the Sources tiles, avo 6 Nov 2026)."""
from __future__ import annotations

from RevenueChaser.module_settings import control, control_render
from RevenueChaser.module_settings.tests.test_control_render_c1 import NOW, _view
from RevenueChaser.module_settings.tests.test_control_view import control_nd
from RevenueChaser.module_settings.tests.legacy_fetch import old_fetch


def test_the_view_carries_both_switches_on_without_a_row_and_who_turned_one_off():
    v = control.build(control_nd(), now=NOW, switches=lambda: {"backup": {"name": "backup", "off_at": NOW.timestamp(),
                                                                          "off_by": "avo"}})
    assert v["switches"]["fetch"] == {"on": True, "by": None, "at": None}
    assert v["switches"]["backup"]["on"] is False and v["switches"]["backup"]["by"] == "avo"
    assert v["switches"]["backup"]["at"]
    assert control.build(control_nd(), now=NOW, switches=lambda: 1 / 0)["switches"]["fetch"]["on"] is None, \
        "an unreadable store says not known, never on"


def test_the_top_bar_draws_both_switches_and_the_gateway_above_health():
    v = _view()
    v["switches"] = {"fetch": {"on": True, "by": None, "at": None},
                     "backup": {"on": False, "by": "avo", "at": "17:05"}}
    html = old_fetch(v)
    bar = html[html.index('class="topbar"'):html.index('id="sec-health"')]
    assert 'data-job="switch" data-name="fetch" data-on="1"' in bar and "Fetch: on" in bar
    assert 'data-job="switch" data-name="backup" data-on="0"' in bar and "Backup: off" in bar and "avo 17:05" in bar
    assert "IBKR gateway" in bar
    health = html[html.index('id="sec-health"'):]
    assert 'data-group="sources"' not in health or "IBKR gateway" not in health.split("Data ·")[0], \
        "the gateway tile left the Sources group"
    assert control_render.ROUTES["switch"] == "/api/build/switch/"
    assert "C.routes.switch + nm" in control_render.CTL_JS
