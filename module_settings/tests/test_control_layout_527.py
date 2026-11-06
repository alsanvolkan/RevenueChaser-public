"""#527 step 8 (avo 6 Nov 2026): the Data page's layout. The journey's status / role / cadence sit in their own
columns, right-aligned, each as wide as the widest value of its file plus a little; the Health table's short columns
never wrap (only Note does); the universe chips wrap; a table name in Data structure is a heading and its button is
small; the disk picture press says what it does."""
from __future__ import annotations

from RevenueChaser.module_settings import control_render
from RevenueChaser.module_settings.tests.test_control_render_c1 import _view


def _file(v):
    return next(f for f in v["files"] if f["datasets"])


def test_the_journey_meta_columns_are_as_wide_as_their_widest_value():
    f = _file(_view())
    html = control_render._journey(f)
    roles = max(len(d["role"]) for d in f["datasets"])
    cads = max(len("⏱ " + (d["cadence"] or "")) for d in f["datasets"])
    assert f"--jw2:{roles + control_render.COL_PAD}ch" in html
    assert f"--jw3:{cads + control_render.COL_PAD}ch" in html
    css = control_render.CTL_CSS
    assert "grid-template-columns:var(--jw1) var(--jw2) var(--jw3)" in css


def test_the_health_tables_short_columns_never_wrap():
    css = control_render.CTL_CSS
    for label in ("When", "Result", "As-of", "Rows", "Calls"):
        assert f'td[data-label="{label}"]' in css.split("#527 step 8")[-1], label
    assert "white-space:nowrap" in css.split("#527 step 8")[-1]


def test_the_universe_chips_wrap_and_the_disk_picture_press_says_what_it_does():
    css = control_render.CTL_CSS.split("#527 step 8")[-1]
    assert ".ctl .ubar{flex-wrap:wrap}" in css
    html = control_render._picture_control(_view())
    assert ">Recount folders</button>" in html and "Take the disk picture" not in html
    assert "last count" in html


def test_a_data_structure_table_name_is_a_heading_and_its_button_is_small():
    v = _view()
    f = next((f for f in v["files"] if any(d["samples"] for d in f["datasets"])), None)
    if f is None:
        f = _file(v)
        f["datasets"][0]["samples"] = [{"table": "fund", "columns": [{"name": "ticker", "type": "VARCHAR"}],
                                        "rows": [["SPY"]], "built": True}]
    html = control_render._samples(f)
    assert '<h3 class="dsh">' in html and 'class="btn sm" data-sample-refresh=' in html
