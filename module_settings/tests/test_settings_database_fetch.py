"""The two Settings tabs (avo, 10 Nov 2026): Database says what the data is and where it goes, with no press that
starts a job; Fetch/Health holds every control, the universe once at the top, and one folded group a served file."""
from __future__ import annotations

import re

from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.board_js import assert_scripts_parse
from RevenueChaser.module_settings import control, report
from RevenueChaser.module_settings.tests.test_control_view import control_nd


def _page() -> str:
    return report.render_settings_dashboard({"newdata": control_nd()})


def _between(html: str, start: str, end: str) -> str:
    a = html.index(start)
    return html[a:html.index(end, a)]


def test_the_tabs_are_database_and_fetch_health():
    strip = _between(_page(), 'id="settings-tabs"', "</nav>")
    assert ">Database</button>" in strip and ">Fetch/Health</button>" in strip
    assert strip.index('id="tab-data"') < strip.index('id="tab-health"')


def test_the_database_tab_starts_no_job():
    html = _page()
    db = _between(html, 'id="panel-data"', 'id="panel-health"')
    assert "data-job=" not in db
    assert 'id="sec-flow"' in db and "<h2>Data flow</h2>" in db and "plan step" not in db.lower()


def test_fetch_health_holds_the_controls_the_universe_once_and_one_group_a_file():
    html = _page()
    fh = _between(html, 'id="panel-health"', 'id="needs"')
    for job in ("one", "switch", "picture", "fetch"):
        assert f'data-job="{job}"' in fh, job
    assert html.count('id="universe"') == 1 and 'id="universe"' in fh
    assert fh.index('id="sec-providers"') < fh.index('id="sec-universe"'), "providers sit above the universe"
    assert len(re.findall(r'<details class="sec fgrp" data-file="[a-z]+\.duckdb"', fh)) == 6
    assert "data-automation" not in fh, "the file-level Automated row is gone"


def test_the_flow_has_board_files_and_no_plan_step():
    flow = report._control_view(control_nd(), {})["flow"]
    assert "steps" not in flow and "srcStep" not in flow["edges"] and flow["edges"]["srcTab"]
    names = [f["name"] for f in flow["boardFiles"]]
    assert sorted(names) == sorted(f"{db}.duckdb" for db in rawstore.BOARD_DBS)
    assert len({f["color"] for f in flow["boardFiles"]}) == len(names)
    owned = dict(flow["edges"]["boardOwn"])
    assert owned.get("Relatedness") == "relatedness.duckdb"
    # a dashed read is never a board's own file
    assert not any(owned.get(b) == f for b, f in flow["edges"]["boardRead"])


def test_board_files_follow_the_manifest():
    files, own, _ = control.board_files()
    by = {f["name"]: f for f in files}
    for name, m in rawstore.MANIFEST.items():
        if m.get("db") in rawstore.BOARD_DBS:
            assert name in by[f'{m["db"]}.duckdb']["tables"], name
    assert all(f in by for _, f in own)


def test_the_journey_has_a_board_column_and_folded_paths():
    db = _between(_page(), 'id="panel-data"', 'id="panel-health"')
    assert 'class="bdh"' in db and '<td class="bdc"' in db
    assert '<details class="pf">' in db


def test_the_page_scripts_still_parse():
    assert_scripts_parse(_page())
