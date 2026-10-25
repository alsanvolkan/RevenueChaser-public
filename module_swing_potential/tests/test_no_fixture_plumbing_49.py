"""#49 (Screeners A2): ``demo_payload`` is gone, so nothing builds a fixture payload any more. The flag it set, the
``[DEMO/FIXTURE]`` tag it lit and the history branch that read it are dead plumbing and are removed with it."""
from __future__ import annotations

import pytest

from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_swing_potential.pipeline import build_payload
from RevenueChaser.module_swing_potential.report import render_swing_dashboard

_PAYLOAD = {"is_fixture": True, "no_data": True, "no_data_message": "No universe stored.", "no_data_reason": "",
            "candidates": [], "data_quality": {"spy_stale": False}}


@pytest.mark.parametrize("render", [render_swing_dashboard, render_fundamental_dashboard])
def test_no_board_lights_a_fixture_tag_whatever_the_payload_says(render):
    assert "DEMO/FIXTURE" not in render(dict(_PAYLOAD))


def test_a_swing_payload_carries_no_fixture_flag():
    assert "is_fixture" not in build_payload([], top_n=10)
