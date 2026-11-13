"""#715 (avo, 12 Nov 2026): every Fetch control and all status live in the Grafana board. The HTML under the
Fetch/Health frame keeps only what that board does not carry (the Universe, Fill and Backfill, Recount folders, the
IBKR login link, and per file the coverage, the open problems row by row and the last three runs); the reference
parts and the forms sit folded under Database."""
from __future__ import annotations

import re

from RevenueChaser.core.tests.board_fixtures import H
from RevenueChaser.core.tests.test_board_html_guard import assert_inert
from RevenueChaser.module_settings import control, control_render, report
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd
from RevenueChaser.module_settings.tests.test_report import _payload

#: The presses the Grafana board carries (home-server ``homeops/act.py``): none of them is drawn in HTML any more.
MOVED = ("switch", "timer", "one", "fmenu", "stage", "pause", "stop", "cadence", "automation")


def _fetch() -> str:
    return control_render.render_fetch(control.build(control_nd(), now=NOW))


def _panel(page: str, name: str) -> str:
    start = page.index(f'<div id="panel-{name}"')
    nxt = [page.find(f'<div id="panel-{n}"', start + 1) for n in ("data", "health", "logs")]
    ends = [i for i in nxt if i > start] + [page.index("<script>", start)]
    return page[start:min(ends)]


def test_the_html_under_the_frame_keeps_no_press_the_grafana_board_carries():
    html = _fetch()
    jobs = set(re.findall(r'data-job="([a-z]+)"', html))
    assert not jobs & set(MOVED), jobs
    for gone in ("sec-queue", "sec-crosscheck", "sec-backups", "sec-providers", "Health right now"):
        assert gone not in html, gone


def test_what_grafana_does_not_carry_stays():
    html = _fetch()
    assert 'id="universe"' in html and 'data-job="picture"' in html
    assert "IBKR gateway" in html
    assert "Universe and fills" in html


def _has_fill(d: dict) -> bool:
    return bool(d["fill"]["cells"] or d["calendar"])


def test_a_dataset_gets_a_box_only_for_what_the_board_does_not_show():
    view = control.build(control_nd(), now=NOW)
    every = [d for f in view["files"] for d in f["datasets"]]
    want = {d["dataset"] for d in every if _has_fill(d) or d["coverage"] or d["issues_open"]}
    html = control_render.render_fetch(view)
    boxes = dict(re.findall(r'class="dset sbox" data-dataset="([^"]+)"(.*?)(?=class="dset sbox"|<details class="blk b-hlt")',
                            html, flags=re.S))
    assert set(boxes) == want
    fills = {d["dataset"] for d in every if _has_fill(d)}
    assert fills and want - fills, "the fixture needs a dataset with a Fill and one kept for another reason"
    for d in every:
        if d["dataset"] not in want:
            continue
        box = boxes[d["dataset"]]
        assert ("Fill by tier set" in box) == bool(d["fill"]["cells"]), d["dataset"]
        assert ('class="meter cov' in box) == bool(d["coverage"]), d["dataset"]
        assert ("Open problems" in box) == bool(d["issues_open"]), d["dataset"]
        assert 'class="meter age' not in box, "the board shows the last check; it is not drawn twice"


def test_every_file_keeps_its_last_runs_and_names_its_open_problems():
    """Review of #800: the board shows a count of open problems, no coverage, and only the last check of a dataset.
    Until it has panels for them, the rows, the coverage bar and the last three runs stay under the frame."""
    view = control.build(control_nd(), now=NOW)
    html = control_render.render_fetch(view)
    groups = re.findall(r'<details class="sec fgrp" data-file="([^"]+)"', html)
    assert groups == [f["name"] for f in view["files"]]
    assert html.count('<details class="blk b-hlt">') == len(groups)
    for f in view["files"]:
        probs = sum(d["issues_open"] for d in f["datasets"])
        assert f"open problems {probs:,} · the last runs" in html, f["name"]


def test_every_string_under_the_frame_is_escaped():
    view = control.build(control_nd(), now=NOW)
    view["gateway"] = {"cls": H("gw_cls"), "n": H("gw_n"), "label": H("gw_label"),
                       "login": "https://gateway.example/?" + H("gw_login")}
    for f in view["files"]:
        f["name"] = H("file")
        for d in f["datasets"]:
            d.update(dataset=H("dataset"), filter=H("filter"), issues_open=1,
                     issues=[{"kind": H("kind"), "detail": H("detail"), "key": H("key"), "found_on": "2026-10-28"}],
                     recent=[{"checked_on": H("checked"), "result": H("result"), "source_as_of": H("as_of"),
                              "note": H("note")}])
            if d["coverage"]:
                d["coverage"] = {**d["coverage"], "text": H("cov_text"), "cls": H("cov_cls")}
    html = control_render.render_fetch(view)
    # the page as it is drawn now: these strings reach the slim boxes, not the old source boxes
    assert "Settings · Universe and fills" in html and 'data-job="one"' not in html
    for tag in ("gw_cls", "gw_n", "gw_label", "gw_login", "file", "dataset", "filter", "kind", "detail", "key",
                "checked", "result", "as_of", "note", "cov_text", "cov_cls"):
        assert f"onerror=__x(&#x27;{tag}T&#x27;)" in html, tag
    assert_inert(html)


def test_the_stale_strip_under_the_frame_is_shown_by_the_same_clock():
    html = _fetch()
    assert re.search(r'<div class="stalebar" data-stale hidden>', html), "the strip under the frame"
    assert "querySelectorAll('[data-stale]')" in control_render.CTL_JS
    assert "getElementById('ctl-stale')" not in control_render.CTL_JS


def test_the_old_needs_address_opens_database_where_the_catalog_is():
    assert report.SETTINGS_TABS["aliases"]["needs"] == "data"
    page = report.render_settings_dashboard(dict(_payload(), newdata=control_nd()))
    assert 'id="needs"' in _panel(page, "data")


def test_a_board_settings_field_is_two_columns_on_a_phone():
    """Review of #800, measured at 390 px with a settings card open: the range hints reached 440 px."""
    assert ("@media(max-width:760px){.sfield{grid-template-columns:minmax(0,1fr) 110px}"
            ".sfield .rng{grid-column:1/-1;overflow-wrap:anywhere}") in report.CSS


def test_a_view_that_could_not_be_built_is_one_line():
    html = control_render.render_fetch({"error": "ops.sqlite is locked"})
    assert "Could not read" in html and "ops.sqlite is locked" in html
    assert "Settings · Universe and fills" in html, "the line names the part that is left, not the old tab"


def test_the_reference_parts_and_the_forms_sit_folded_under_database():
    page = report.render_settings_dashboard(dict(_payload(), newdata=control_nd()))
    data, health = _panel(page, "data"), _panel(page, "health")
    assert 'id="database-more"' in data
    for part in ('id="needs-catalog"', 'id="needs-routes"'):
        assert part in data and part not in health, part
        at = data.index(part)
        assert data.rfind('<details class="hfold">', 0, at) > data.rfind("</details>", 0, at), f"{part} is not folded"


def test_what_the_grafana_boards_show_is_not_drawn_twice():
    page = report.render_settings_dashboard(dict(_payload(), newdata=control_nd()))
    for gone in ('id="needs-log"', 'id="health-more"', 'id="needs-wial"', "Needs, boards and automations"):
        assert gone not in page, gone


def test_the_need_catalog_and_the_routes_come_apart():
    model = {"error": "boom", "read_at": "10:00"}
    catalog, log, routes = report.needs_tab.parts(model)
    assert "boom" in catalog and log == "" and routes == ""
