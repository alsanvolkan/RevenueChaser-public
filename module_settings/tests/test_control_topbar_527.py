"""#527 step 4: the top bar, always above Health right now: the fetch switch, the backup switch and the IBKR
gateway's state (moved up from the Sources tiles, avo 6 Nov 2026)."""
from __future__ import annotations

from RevenueChaser.module_settings import control
from RevenueChaser.module_settings.tests.test_control_render_c1 import NOW
from RevenueChaser.module_settings.tests.test_control_view import control_nd


def test_the_view_carries_both_switches_on_without_a_row_and_who_turned_one_off():
    v = control.build(control_nd(), now=NOW, switches=lambda: {"backup": {"name": "backup", "off_at": NOW.timestamp(),
                                                                          "off_by": "avo"}})
    assert v["switches"]["fetch"] == {"on": True, "by": None, "at": None}
    assert v["switches"]["backup"]["on"] is False and v["switches"]["backup"]["by"] == "avo"
    assert v["switches"]["backup"]["at"]
    assert control.build(control_nd(), now=NOW, switches=lambda: 1 / 0)["switches"]["fetch"]["on"] is None, \
        "an unreadable store says not known, never on"
