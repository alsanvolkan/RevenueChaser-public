"""#492 PR (i), review fixes F3: the render's seams between slices."""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control_render
from RevenueChaser.module_settings.tests.test_control_render_c1 import _file, _view


def test_a_cross_check_step_is_not_a_from_chip():
    v = _view()
    f = _file(v, "prices.duckdb")
    roles = {d["dataset"]: d["role"] for d in f["datasets"]}
    html = control_render._journey(f)
    builds = re.findall(r'<tr class="jr"[^>]*data-ds="([^"]+)"[^>]*>(.*?)</tr>', html)
    chips = "".join(row for ds, row in builds if roles.get(ds) == "build")
    for ds, role in roles.items():
        if role == "cross-check":
            assert f'data-card="{ds}"' not in chips, ds


def test_the_two_universe_cards_are_drawn_from_the_view():
    v = _view()
    assert v.get("identity") is not None, "build() carries the cards' data"
    v["my_universe"] = v.get("my_universe") or {"built": False}
    html = control_render._uni_cards(v)
    assert html.startswith('<div class="ucards">') and html.count("nd-card") >= 2
