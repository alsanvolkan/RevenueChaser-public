"""Logs tab: read-only tails of the dashboard's own log files (app/logview.py, /view/logs, /api/logs).

start.ps1 starts the dashboard hidden, so what the console window used to show now goes to files under
``.cache/logs/``. This tab is where the user reads them. The browser names a source key, never a path.
"""
from __future__ import annotations

import json
import logging
import re

import pytest

from RevenueChaser.app import dashboard, logview
from RevenueChaser.core import config, provenance

TEMPLATE = dashboard.Path(dashboard.__file__).resolve().parent.parent / "templates" / "dashboard.html"


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def _api(client, query):
    return client.get("/api/logs" + query)


# ── the tab ───────────────────────────────────────────────────────────────────

def test_the_tab_is_registered_in_the_ops_group():
    tabs = dashboard.TABS
    i_head = next(i for i, t in enumerate(tabs) if t["type"] == "header" and t["label"] == "Ops")
    keys = [t["key"] for t in tabs[i_head + 1:] if t["type"] == "tab"]
    assert "logs" in keys
    tab = next(t for t in tabs if t.get("key") == "logs")
    assert tab["url"] == "/view/logs" and tab["label"] == "Logs"


def test_the_shell_lists_the_tab(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-tab="logs"' in html and 'data-url="/view/logs"' in html


def test_the_tab_is_a_read_only_view_with_no_fetch_or_refresh():
    """Not a board backed by data: no Fetch, no Refresh, no run route, no data date."""
    assert "logs" not in dashboard._MODEL_ONLY_RENDERERS
    assert "logs" not in dashboard._STORE_REFRESHABLE
    assert dashboard._module_file("logs") is None
    assert "logs" in provenance.RESOLVERS and provenance.RESOLVERS["logs"] is None
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "'logs'" not in html.split("var STORE_BACKED")[1].split("];")[0]
    assert "logs:" not in html.split("var RUN_URLS")[1].split("};")[0]
    show_tab = html.split("function showTab")[1].split("checkDataAsOf(key)")[0]
    refresh_line = next(ln for ln in show_tab.splitlines() if "rfBtn.style.display" in ln)
    assert "key==='logs'" in refresh_line, "the Refresh button must be hidden on the Logs tab"
    fetch_line = next(ln for ln in show_tab.splitlines() if "ftBtn.style.display" in ln)
    assert "logs" not in fetch_line
    assert "'logs': true" in html.split("var FILL_WIDTH")[1].split("};")[0], "the page fills the pane"


def test_the_page_renders_its_controls(client, log_dir):
    resp = client.get("/view/logs")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert len(re.findall(r"<h1\b", html)) == 1
    for src in ("server", "output", "launcher"):
        assert f'data-src="{src}"' in html
    for label in ("Server", "Output", "Launcher", "Previous run", "Problems only", "Follow", "Pause"):
        assert label in html
    assert str(log_dir) in html, "the page says where the files are"
    low = html.lower()
    for word in ("clear", "delete", "download"):
        assert f">{word}" not in low, f"no {word} button: the tab is read-only"


def test_every_control_is_labelled(client, log_dir):
    html = client.get("/view/logs").get_data(as_text=True)
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    inputs = re.findall(r"<input\b[^>]*>", html)
    assert inputs
    for target in re.findall(r'<label\b[^>]*\bfor="([^"]+)"', html):
        assert target in ids
    assert len(re.findall(r'<label\b[^>]*\bfor="', html)) >= len(inputs)


def test_the_page_embeds_the_server_log_tail_so_the_first_view_needs_no_poll(client, log_dir):
    (log_dir / "dashboard.err.log").write_text("first\nsecond\n", encoding="utf-8")
    html = client.get("/view/logs").get_data(as_text=True)
    m = re.search(r"var INITIAL = (.*?);\n", html)
    assert m, "the page carries the first payload"
    data = json.loads(m.group(1))
    assert data["src"] == "server" and data["lines"] == ["first", "second"]


def test_a_hostile_log_line_is_inert_in_the_page(client, log_dir):
    """Any web page can make the browser GET http://127.0.0.1:8000/<markup>, and werkzeug logs the path."""
    (log_dir / "dashboard.err.log").write_text('"GET /</script><img src=x onerror=alert(1)> HTTP/1.1" 404 -\n',
                                               encoding="utf-8")
    html = client.get("/view/logs").get_data(as_text=True)
    assert "<img src=x" not in html
    assert "</script><img" not in html
    assert ".innerHTML" not in html, "log lines go in through textContent"


# ── /api/logs ─────────────────────────────────────────────────────────────────

def test_api_returns_the_last_lines_of_the_server_log(client, log_dir):
    (log_dir / "dashboard.err.log").write_text("".join(f"line {i}\n" for i in range(10)), encoding="utf-8")
    data = _api(client, "?src=server&tail=3").get_json()
    assert data["exists"] is True
    assert data["file"] == "dashboard.err.log"
    assert data["lines"] == ["line 7", "line 8", "line 9"]
    assert data["size"] > 0 and data["modified"]


def test_each_source_maps_to_its_own_file(client, log_dir):
    (log_dir / "dashboard.log").write_text("printed\n", encoding="utf-8")
    (log_dir / "launcher.log").write_text("09:00:01 Starting the Revenue Chaser dashboard\n", encoding="utf-8")
    assert _api(client, "?src=output").get_json()["lines"] == ["printed"]
    assert _api(client, "?src=launcher").get_json()["lines"] == ["09:00:01 Starting the Revenue Chaser dashboard"]


def test_previous_run_reads_the_prev_file(client, log_dir):
    (log_dir / "dashboard.err.log").write_text("now\n", encoding="utf-8")
    (log_dir / "dashboard.err.prev.log").write_text("before\n", encoding="utf-8")
    data = _api(client, "?src=server&run=prev").get_json()
    assert data["file"] == "dashboard.err.prev.log"
    assert data["lines"] == ["before"]


def test_prev_name_matches_what_start_ps1_renames_to():
    # [IO.Path]::ChangeExtension("dashboard.err.log", ".prev.log")
    assert logview.prev_name("dashboard.err.log") == "dashboard.err.prev.log"
    assert logview.prev_name("launcher.log") == "launcher.prev.log"


def test_missing_file_is_empty_not_an_error(client, log_dir):
    resp = _api(client, "?src=launcher")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["exists"] is False
    assert data["lines"] == []


@pytest.mark.parametrize("query", ["?src=../secrets", "?src=dashboard.err.log", "?src=", "", "?src=server&run=old",
                                   "?src=output&run=..%2Fx", "?src=__class__"])
def test_unknown_source_or_run_is_refused(client, log_dir, query):
    assert _api(client, query).status_code == 400


def test_the_answer_names_the_file_not_its_folder(client, log_dir):
    (log_dir / "launcher.log").write_text("x\n", encoding="utf-8")
    data = _api(client, "?src=launcher").get_json()
    assert data["file"] == "launcher.log"
    assert str(log_dir) not in json.dumps(data)


def test_tail_is_capped_and_bad_values_fall_back(client, log_dir):
    (log_dir / "dashboard.err.log").write_text("".join(f"{i}\n" for i in range(3000)), encoding="utf-8")
    assert len(_api(client, "?src=server&tail=999999").get_json()["lines"]) == logview.MAX_TAIL
    assert len(_api(client, "?src=server&tail=abc").get_json()["lines"]) == logview.DEFAULT_TAIL
    assert len(_api(client, "?src=server&tail=0").get_json()["lines"]) == 1


def test_only_the_end_of_a_large_file_is_read(client, log_dir, monkeypatch):
    monkeypatch.setattr(logview, "READ_BYTES", 64)
    (log_dir / "dashboard.err.log").write_text("x" * 50 + "\n" + "".join(f"tail {i}\n" for i in range(5)),
                                               encoding="utf-8")
    lines = _api(client, "?src=server&tail=100").get_json()["lines"]
    assert lines[-1] == "tail 4"
    assert all(not ln.startswith("x") for ln in lines), "a cut first line must be dropped"


def test_utf8_bom_and_bad_bytes_are_harmless(client, log_dir):
    # PowerShell 5.1's Add-Content -Encoding UTF8 starts launcher.log with a BOM.
    (log_dir / "launcher.log").write_bytes("\ufeffstart\n".encode() + b"bad \xff byte\n")
    lines = _api(client, "?src=launcher").get_json()["lines"]
    assert lines[0] == "start"
    assert lines[1].startswith("bad ")


def test_terminal_colour_codes_are_stripped(client, log_dir):
    """werkzeug colours its request lines (404 yellow, 5xx red) even into a redirected file."""
    (log_dir / "dashboard.err.log").write_text(
        '127.0.0.1 - - [x] "\x1b[33mGET /favicon.ico HTTP/1.1\x1b[0m" 404 -\n'
        '127.0.0.1 - - [x] "\x1b[1m\x1b[31mPOST /run/x HTTP/1.1\x1b[0m" 500 -\n', encoding="utf-8")
    lines = _api(client, "?src=server").get_json()["lines"]
    assert lines == ['127.0.0.1 - - [x] "GET /favicon.ico HTTP/1.1" 404 -',
                     '127.0.0.1 - - [x] "POST /run/x HTTP/1.1" 500 -']


def test_the_api_is_read_only(client, log_dir):
    resp = client.post("/api/logs?src=server", headers={"X-RC-Panel": "test"})
    assert resp.status_code == 405


def test_logs_live_in_the_cache_folder_next_to_the_automation_logs():
    from RevenueChaser.core import automations
    assert config.LOG_DIR == config.OUTPUT_DIR / "logs"
    assert automations.REGISTRY[0].log_path.parent == config.LOG_DIR


# ── the polling filter ────────────────────────────────────────────────────────

def _record(path):
    return logging.LogRecord("werkzeug", logging.INFO, __file__, 1, '%s - - [x] "%s" %s -',
                             ("127.0.0.1", f"GET {path} HTTP/1.1", "200"), None)


def test_log_polling_requests_are_not_logged():
    """The tab polls every few seconds; its own requests must not fill the log it shows."""
    werkzeug = logging.getLogger("werkzeug")
    assert not werkzeug.filter(_record("/api/logs?src=server&tail=1000"))
    assert werkzeug.filter(_record("/view/logs"))
    assert werkzeug.filter(_record("/api/jobs"))


def test_the_filter_is_installed_once():
    logview.quiet_polling()
    logview.quiet_polling()
    filters = [f for f in logging.getLogger("werkzeug").filters if isinstance(f, logview.SkipLogPolling)]
    assert len(filters) == 1
