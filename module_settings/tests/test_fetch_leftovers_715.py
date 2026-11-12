"""#715 (avo, 12 Nov 2026): every Fetch control and all status live in the Grafana board. The HTML under the
Fetch/Health frame keeps only what that board does not carry (the Universe, Fill and Backfill, Recount folders, the
IBKR login link); the reference parts and the forms sit folded under Database."""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control, control_render, report
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd
from RevenueChaser.module_settings.tests.test_report import _payload

#: The presses the Grafana board carries (home-server ``homeops/act.py``): none of them is drawn in HTML any more.
MOVED = ("switch", "timer", "one", "fmenu", "stage", "pause", "stop", "cadence", "automation")


def _fetch() -> str:
    return control_render.render_fetch(control.build(control_nd(), now=NOW))


def _panel(page: str, name: str) -> str:
    start = page.index(f'<div id="panel-{name}"')
    nxt = [page.find(f'<div id="panel-{n}"', start + 1) for n in ("watch", "data", "health", "logs")]
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


def test_only_a_dataset_with_a_fill_or_a_backfill_gets_a_box():
    view = control.build(control_nd(), now=NOW)
    want = {d["dataset"] for f in view["files"] for d in f["datasets"] if d["fill"]["cells"] or d["calendar"]}
    html = control_render.render_fetch(view)
    drawn = set(re.findall(r'class="dset sbox" data-dataset="([^"]+)"', html))
    assert drawn == want
    assert want, "the fixture has no dataset with a Fill or a Backfill: this test would prove nothing"
    every = {d["dataset"] for f in view["files"] for d in f["datasets"]}
    assert every - want, "the fixture has no dataset without one"


def test_a_view_that_could_not_be_built_is_one_line():
    html = control_render.render_fetch({"error": "ops.sqlite is locked"})
    assert "Could not read" in html and "ops.sqlite is locked" in html


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
