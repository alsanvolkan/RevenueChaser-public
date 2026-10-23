"""Runs tab: the read-only run log over ``job_runs`` (app/runview.py, /view/runs, /api/runs; #26 PL5-b).

Two writers have filled ``job_runs`` since #282 J4 and #129 A1, and nothing read it. This tab is the reader.
It shows what the row says and nothing more: a run whose writer recorded no trigger stays "not recorded"
rather than being guessed from the lane (the gap is #83), and a row left ``running`` by a skipped end write
(#82) shows as running with its age.
"""
from __future__ import annotations

import logging
import re

import pytest

from RevenueChaser.app import dashboard, runview
from RevenueChaser.core import provenance, rawstore
from RevenueChaser.core.tests.open_issues_shell import strip_open_issues_data

TEMPLATE = dashboard.Path(dashboard.__file__).resolve().parent.parent / "templates" / "dashboard.html"


@pytest.fixture()
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def _row(run_id, **over):
    row = {"run_id": run_id, "job_id": run_id, "module": "rotation", "lane": "short",
           "trigger": None, "source": None, "pid": 4242, "registry_name": None,
           "params": {}, "queued_at": 1_792_700_000.0, "started_at": 1_792_700_001.0,
           "finished_at": 1_792_700_061.0, "status": "done", "error": None, "message": None,
           "counts": None}
    row.update(over)
    return row


def _write(*rows):
    rawstore.write_rows("job_runs", list(rows))


# ── the reader ────────────────────────────────────────────────────────────────

def test_the_newest_run_comes_first():
    _write(_row("old", started_at=1_792_000_000.0), _row("new", started_at=1_792_900_000.0))
    got = runview.read()
    assert [r["run_id"] for r in got["rows"]] == ["new", "old"]
    assert got["total"] == 2 and got["shown"] == 2


def test_the_limit_caps_the_rows_and_total_still_counts_them_all():
    _write(*[_row(f"r{i}", started_at=1_792_000_000.0 + i) for i in range(5)])
    got = runview.read(limit=2)
    assert [r["run_id"] for r in got["rows"]] == ["r4", "r3"]
    assert got["total"] == 5 and got["shown"] == 2 and got["limit"] == 2


def test_a_queued_run_that_never_started_is_still_listed():
    """Ordered on started_at alone, a row with no start would sort last and look ancient."""
    _write(_row("started", started_at=1_792_000_000.0),
           _row("queued", queued_at=1_792_900_000.0, started_at=None, finished_at=None, status="queued"))
    assert [r["run_id"] for r in runview.read()["rows"]] == ["queued", "started"]


def test_a_finished_run_carries_its_duration_and_a_running_one_does_not():
    _write(_row("done", started_at=1_000.0, finished_at=1_091.5),
           _row("going", started_at=2_000.0, finished_at=None, status="running"))
    by_id = {r["run_id"]: r for r in runview.read()["rows"]}
    assert by_id["done"]["duration_s"] == pytest.approx(91.5)
    assert by_id["going"]["duration_s"] is None and by_id["going"]["status"] == "running"


def test_a_run_that_recorded_no_trigger_stays_unrecorded():
    """The dashboard writer leaves trigger/source NULL (#83). The reader must not infer one from the lane."""
    _write(_row("dash", lane="short", trigger=None, source=None),
           _row("task", lane=None, trigger="scheduled", source="RC-Daily-Bars",
                registry_name="RC-Daily-Bars", module="RC-Daily-Bars"))
    by_id = {r["run_id"]: r for r in runview.read()["rows"]}
    assert by_id["dash"]["trigger"] is None and by_id["dash"]["source"] is None
    assert by_id["task"]["trigger"] == "scheduled" and by_id["task"]["source"] == "RC-Daily-Bars"


def test_the_json_columns_come_back_as_objects():
    _write(_row("j", params={"ticker": "RTX"}, counts={"rows": 12}))
    row = runview.read()["rows"][0]
    assert row["params"] == {"ticker": "RTX"} and row["counts"] == {"rows": 12}


def test_the_message_of_a_done_run_and_the_error_of_a_failed_one_both_show():
    _write(_row("said", status="done", message="nothing to do"),
           _row("broke", status="error", error="kaput"))
    by_id = {r["run_id"]: r for r in runview.read()["rows"]}
    assert by_id["said"]["message"] == "nothing to do" and by_id["said"]["error"] is None
    assert by_id["broke"]["error"] == "kaput"


def test_a_silly_limit_is_clamped_not_refused():
    assert runview.clamp_limit("0") == 1
    assert runview.clamp_limit(str(runview.MAX_LIMIT * 10)) == runview.MAX_LIMIT
    assert runview.clamp_limit("abc") == runview.DEFAULT_LIMIT
    assert runview.clamp_limit(None) == runview.DEFAULT_LIMIT


def test_an_empty_table_is_an_empty_page_not_an_error():
    got = runview.read()
    assert got["rows"] == [] and got["total"] == 0


# ── the route ─────────────────────────────────────────────────────────────────

def test_the_api_serves_the_rows(client):
    _write(_row("one", module="spxgamma"))
    data = client.get("/api/runs").get_json()
    assert data["total"] == 1
    assert data["rows"][0]["run_id"] == "one" and data["rows"][0]["module"] == "spxgamma"


def test_the_api_clamps_the_limit(client):
    data = client.get(f"/api/runs?limit={runview.MAX_LIMIT * 10}").get_json()
    assert data["limit"] == runview.MAX_LIMIT


def test_the_api_takes_no_write(client):
    assert client.post("/api/runs").status_code in (403, 405)


# ── the tab ───────────────────────────────────────────────────────────────────

def test_the_tab_is_registered_in_the_ops_group():
    tabs = dashboard.TABS
    i_head = next(i for i, t in enumerate(tabs) if t["type"] == "header" and t["label"] == "Ops")
    keys = [t["key"] for t in tabs[i_head + 1:] if t["type"] == "tab"]
    assert "runs" in keys
    tab = next(t for t in tabs if t.get("key") == "runs")
    assert tab["url"] == "/view/runs" and tab["label"] == "Runs"


def test_the_shell_lists_the_tab(client):
    html = strip_open_issues_data(client.get("/").get_data(as_text=True))
    assert 'data-tab="runs"' in html and 'data-url="/view/runs"' in html


def test_the_tab_is_a_read_only_view_with_no_fetch_or_refresh():
    """Not a board backed by its own downloaded data: no Fetch, no Refresh, no run route, no data date."""
    assert "runs" not in dashboard._MODEL_ONLY_RENDERERS
    assert "runs" not in dashboard._STORE_REFRESHABLE
    assert dashboard._module_file("runs") is None
    assert "runs" in provenance.RESOLVERS and provenance.RESOLVERS["runs"] is None
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "'runs'" not in html.split("var STORE_BACKED")[1].split("];")[0]
    assert "runs:" not in html.split("var RUN_URLS")[1].split("};")[0]
    show_tab = html.split("function showTab")[1].split("checkDataAsOf(key)")[0]
    refresh_line = next(ln for ln in show_tab.splitlines() if "rfBtn.style.display" in ln)
    assert "key==='runs'" in refresh_line, "the Refresh button must be hidden on the Runs tab"
    fetch_line = next(ln for ln in show_tab.splitlines() if "ftBtn.style.display" in ln)
    assert "runs" not in fetch_line
    assert "'runs': true" in html.split("var FILL_WIDTH")[1].split("};")[0], "the page fills the pane"


def test_the_page_renders_its_controls(client):
    resp = client.get("/view/runs")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert len(re.findall(r"<h1\b", html)) == 1
    for label in ("Problems only", "Pause", "Job", "Status", "Started", "Took"):
        assert label in html
    low = html.lower()
    for word in ("clear", "delete", "run now", "retry"):
        assert f">{word}" not in low, f"no {word} button: the tab is read-only"


def test_the_page_carries_the_first_rows_with_it(client):
    _write(_row("seeded", module="module_leaps", status="error", error="kaput"))
    html = client.get("/view/runs").get_data(as_text=True)
    assert "seeded" in html and "module_leaps" in html


def test_the_page_puts_every_stored_value_in_as_text(client):
    """A job message, an error and a params blob are stored text. The hostile fixture proves the server's
    escaping (core/tests/test_board_html_guard.py); this proves the page's own DOM writes, which that guard
    cannot see because they happen in the browser."""
    html = client.get("/view/runs").get_data(as_text=True)
    for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in html, f"{sink} on the run log: values go in through textContent"


def test_the_page_says_why_a_trigger_can_be_missing(client):
    """An honest page: it names the gap instead of filling the column in."""
    html = client.get("/view/runs").get_data(as_text=True)
    assert "#83" in html


def test_every_control_is_labelled(client):
    html = client.get("/view/runs").get_data(as_text=True)
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    inputs = re.findall(r"<input\b[^>]*>", html)
    assert inputs, "the page has controls to label"
    labels = re.findall(r'<label\b[^>]*\bfor="([^"]+)"', html)
    assert len(labels) >= len(inputs)
    for target in labels:
        assert target in ids, f"label points at a missing id: {target}"


def _request(path):
    return logging.LogRecord("werkzeug", logging.INFO, __file__, 1, '%s - - [x] "%s" %s -',
                             ("127.0.0.1", f"GET {path} HTTP/1.1", "200"), None)


def test_the_tabs_own_polling_stays_out_of_the_log_it_would_fill():
    """The Runs tab polls every few seconds; werkzeug's lines for it must not reach the Logs tab."""
    werkzeug = logging.getLogger("werkzeug")
    assert not werkzeug.filter(_request("/api/runs"))
    assert werkzeug.filter(_request("/view/runs"))
    assert not werkzeug.filter(_request("/api/logs?src=server")), "the Logs tab's own filter still holds"
    assert werkzeug.filter(_request("/api/jobs"))


# ── the overlay (the other half of PL5-b) ─────────────────────────────────────

def test_the_overlay_names_the_running_job_instead_of_a_fixed_sentence():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'id="overlay-msg"' in html, "the overlay message needs an id to be written into"
    assert "function overlayText" in html, "one place decides what the overlay says"
    body = html.split("function overlayText")[1].split("\n}")[0]
    assert "phase" in body, "the phase /api/jobs already serves is rendered"
    assert "elapsed" in body or "started_at" in body, "a long run says how long it has been going"
