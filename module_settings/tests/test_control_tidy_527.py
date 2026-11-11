"""#527 step 8b (avo 6 Nov 2026): the Data page says less at once. The Fill legend and the Fetch / Pause / Resume
text are one visible line each with the rest behind a "?"; Pause shows on an idle dataset too (decision A); the
Cross-check table wears the journey's badges and pills; the tree's glow drops when the pointer leaves and its lines
keep the provider's colour on a light page."""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control_render
from RevenueChaser.module_settings.tests.test_control_render_c1 import _box, _sources, _view


def test_the_button_text_and_the_fill_legend_are_one_line_and_a_question_mark():
    # the fill legend sits once under the universe on Fetch/Health (avo, 10 Nov 2026)
    html = _sources(_view(), "prices.duckdb") + control_render.render_fetch(_view())
    blegend = re.search(r'<div class="blegend">(.*?)</div>', html, re.S)
    assert blegend, "the button legend is one line with a ? beside it"
    visible = blegend.group(1).split("<details", 1)[0]
    assert "Fetch" in visible and "Pause" in visible and "Resume" in visible
    assert len(re.sub(r"<[^>]+>", "", visible)) < 110, visible
    assert '<summary class="q" aria-label="What the buttons do">?</summary>' in blegend.group(1)
    flegend = re.search(r'<div class="flegend">(.*?)</div></div>|<div class="flegend">(.*?)</details></div>', html, re.S)
    assert flegend
    assert '<summary class="q" aria-label="What a Fill cell shows">?</summary>' in html
    assert html.count('class="q"') >= 2


def test_pause_shows_on_an_idle_dataset_and_hides_once_the_run_has_its_own():
    v = _view()
    v["queue"] = {"active": [], "finished": []}
    idle = _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    assert 'class="btn sm idlep" data-job="stop" data-dataset="alpaca/bars"' in idle
    v["queue"] = {"active": [{"id": 4, "dataset": "alpaca/bars", "status": "running"}], "finished": []}
    busy = _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    assert "idlep" not in busy, "a running dataset's one button is Pause already"
    js = control_render.CTL_JS
    assert "querySelector('.idlep')" in js and "ip.hidden = st !== 'fetch'" in js


def test_the_cross_check_wears_the_journeys_badges_pills_and_short_columns():
    v = _view()
    v["cross_check"] = [
        {"field": "prices", "need": "prices.wrds_diff", "second_source": "WRDS (CRSP and Compustat)",
         "provider": "wrds", "checked": "21 sessions of 500 names", "differs": 3, "worst": "0.120%", "as_of": "3 Nov"},
        {"field": "interest rates", "need": "macro.dgs10", "second_source": None, "differs": None,
         "why": "no second source"}]
    html = control_render._cross(v)
    assert 'class="stk xck"' in html
    assert '<span class="srcb"' in html and "WRDS (CRSP and Compustat)" in html
    assert '<span class="pill p-bad">3 differ</span>' in html
    assert '<span class="pill p-mut">no second source</span>' in html
    css = control_render.CTL_CSS
    for label in ("Compared", "Differs", "Largest deviation", "When"):
        assert f'.xck td[data-label="{label}"]' in css, label


def test_the_tree_glow_drops_on_leave_and_on_a_redraw():
    js = control_render.TREE_JS
    enter = js.split("mouseenter", 1)[1].split("});", 1)[0]
    assert "svg.appendChild(g)" not in enter, "moving the hovered node loses its mouseleave"
    draw = js.split("function draw(j)", 1)[1].split("while (svg.firstChild)", 1)[0]
    assert "unlit(svg)" in draw
    assert "classList.remove('dtree-ring')" in js and "classList.remove('dtree-lit')" in js


def test_the_tree_lines_keep_their_colour_on_a_light_page():
    js = control_render.TREE_JS
    assert "if (!dark()) return" in js
    assert re.search(r"svg\.dtree \.vis\{stroke-width:1\.6;opacity:\.9", control_render.CTL_CSS)


def test_each_source_has_an_automated_select_that_defaults_to_as_the_file():
    from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd
    from RevenueChaser.module_settings import control
    v = control.build(control_nd(), now=NOW, ibkr={"state": "up"}, switches=lambda: {},
                      kind_modes=lambda: {"market": {"kind": "market", "mode": "raw"}},
                      dataset_modes=lambda: {"market_series": {"dataset": "market_series", "mode": "full",
                                                               "set_by": "avo", "set_at": 1.0}},
                      timer=lambda: {"state": "on", "last": None, "next": None, "minute": 7})
    ms = next(d for f in v["files"] for d in f["datasets"] if d["dataset"] == "market_series")
    assert ms["automation_own"] == "full"
    html = control_render.render_fetch(v)
    box = html.split('data-dataset="market_series"', 1)[1]
    assert 'data-dsauto' in box and '<option value="full" selected>' in box.split("</select>", 1)[0]
    assert "as the file (raw)" in box.split("</select>", 1)[0]
    assert '"automation_ds": "/api/build/automation-ds/"' in control_render.scripts(v) or \
        control_render.ROUTES["automation_ds"] == "/api/build/automation-ds/"
