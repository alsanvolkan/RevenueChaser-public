"""#492 PR (i), review fixes F3: the render's seams between slices."""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control_render
from RevenueChaser.module_settings.tests.test_control_render_c1 import _file, _view


def test_next_words_prefix_only_a_time():
    assert control_render._next_words("every trading day, 06:07 (00:07 New York)").startswith("next every")
    assert control_render._next_words("Fri 13 Nov, 06:07 (00:07 New York)") == "next Fri 13 Nov, 06:07 (00:07 New York)"
    for whole in ("skips until Resume", "by hand only", "next pass not known", "not before a first Fetch"):
        assert control_render._next_words(whole) == whole
    assert control_render._next_words("timer off · next pass not known") == "timer off · next pass not known"
    assert control_render._next_words(None) == "next: not known"


def test_the_disk_picture_status_is_found_from_its_button_not_a_repeated_id():
    html = control_render.render_data(_view())
    assert 'id="picout"' not in html
    assert html.count('class="hm picout" role="status"') == len(_view()["files"])


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
