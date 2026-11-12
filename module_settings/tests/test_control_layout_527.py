"""#527 step 8 (avo 6 Nov 2026): the Data page's layout. The journey's status / role / cadence sit in their own
columns, right-aligned, each as wide as the widest value of its file plus a little; the Health table's short columns
never wrap (only Note does); the universe chips wrap; a table name in Data structure is a heading and its button is
small; the disk picture press says what it does."""
from __future__ import annotations
import re

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


def test_a_source_box_keeps_its_buttons_on_the_name_line():
    """avo 6 Nov 2026 20:19 ("kaydirdin", screenshot): two boxes side by side, one with its Fetch / ▾ / Pause on the
    name line and one with them pushed under it. The head row never wraps on a desktop: the name gives way (it
    shortens with an ellipsis, its full text in the title) and the buttons keep their place. A phone may wrap."""
    css = control_render.CTL_CSS.split("kaydirdin", 1)[-1]
    assert ".ctl .sb1{flex-wrap:nowrap}" in css
    assert re.search(r"\.ctl \.sb1 \.dsname\{[^}]*min-width:0[^}]*text-overflow:ellipsis", css)
    assert re.search(r"\.ctl \.sb1 \.sbbtn,\.ctl \.sb1 \.srcb\{flex:none", css)
    assert re.search(r"@media\(max-width:760px\)\{\.ctl \.sb1\{flex-wrap:wrap\}", css)
    assert "@media(max-width:1199px)" not in css, "avo 20:25: two boxes a row stay"
    assert ".ctl .sgrid{container-type:inline-size;container-name:sgrid}" in css
    assert re.search(r"@media\(min-width:761px\)\{\s*@container sgrid \(max-width:1100px\)\{\.ctl \.sgrid>\.sbox\{zoom:", css)
    from RevenueChaser.module_settings.tests.test_control_render_c1 import _sources, _view
    assert 'class="dsname" title="alpaca/bars">alpaca/bars</span>' in _sources(_view(), "prices.duckdb")


def test_a_crowded_source_box_wraps_its_buttons_instead_of_pushing_them_past_the_page():
    """Live page at 1187 px, 12 Nov 2026: scrollWidth 1266, the one cause a box's Fetch menu beside its Pause."""
    css = control_render.CTL_CSS.split("kaydirdin", 1)[-1]
    assert ".ctl .sb1 .dsname{flex-shrink:1000}" in css
    assert re.search(r"\.ctl \.sb1 \.sbbtn\{flex:0 1 auto;min-width:min-content;flex-wrap:wrap", css)
