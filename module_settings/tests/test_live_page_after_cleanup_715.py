"""#715 cleanup: what the removed Fetch/Health tests also said about the pages that stayed.

The tests of the old Fetch/Health HTML went with its renderers. Several of them held a second half about the
Database page (``render_data``), the Universe and fills page (``render_fetch``) and two sentences in ``report``;
those halves are kept here, read against the pages as they are drawn now.
"""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control_render, report
from RevenueChaser.module_settings.tests.test_control_render_c1 import FILES, _view


def test_render_data_holds_its_parts_in_order_and_no_fetch_control():
    html = control_render.render_data(_view())
    marks = ['<h1>Settings · Database</h1>', 'id="sec-flow"', 'class="sec fpanel" data-file="spine.duckdb"']
    at = [html.index(m) for m in marks]
    assert at == sorted(at)
    assert 'id="sec-health"' not in html and 'id="sec-crosscheck"' not in html and "data-job=" not in html
    assert 'id="openall"' not in html and 'id="shutall"' not in html and 'id="dataempty"' not in html
    assert "No file matches the diagram" not in html
    assert html.count('id="universe"') == 0, "the universe is on the Fetch/Health tab only"
    assert 'class="hm picout"' not in html, "the Recount folders status is not on Database"


def test_each_file_panel_carries_its_colour_and_its_journey_only():
    v = _view()
    html = control_render.render_data(v)
    panels = re.findall(
        r'<details class="sec fpanel" data-file="([^"]+)" id="fp-\w+" style="--fh:#[0-9a-f]{6}"( open)?>', html)
    assert [p[0] for p in panels] == [f["name"] for f in v["files"]]
    assert [bool(p[1]) for p in panels] == [True] + [False] * (FILES - 1)
    a = html.index('class="sec fpanel" data-file="prices.duckdb"')
    one = html[a:html.index('class="sec fpanel"', a + 1)]
    assert 'class="journey"' in one and 'class="sources' not in one and 'class="pictrow"' not in one


def test_the_fetch_page_keeps_the_universe_the_fill_legend_and_one_recount_status():
    html = control_render.render_fetch(_view())
    assert html.count('id="universe"') == 1
    assert "A Fetch of only some tier sets also brings" in html and "press and hold" not in html
    assert '<summary class="q" aria-label="What a Fill cell shows">?</summary>' in html
    assert 'id="picout"' not in html
    assert html.count('class="hm picout" role="status"') == 1, "found from its button, not from a repeated id"
    at = [html.index(m) for m in ('id="sec-universe"', 'class="fgrps"', 'class="pictrow"')]
    assert at == sorted(at)


def test_the_words_say_pause_stops_and_the_old_ones_are_gone():
    for gone in ("cannot be cancelled", "cannot be taken back", "no cancel route"):
        assert gone not in report.ND_PAUSE_STOPS and gone not in control_render.WIAL, gone
    assert ("Pause cancels a queued request, stops a running fetch before its next provider call and holds the "
            "dataset until Resume.") in report.DATA_HELP
