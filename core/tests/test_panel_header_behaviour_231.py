"""The real page JS sends ``X-RC-Panel`` when a write button is pressed (#231).

``test_panel_header_sites_231.py`` reads the source; this runs it. Each board's inline
scripts load under node against the shared DOM stub with ``fetch`` replaced by a spy
that records ``(url, init)``; the test then calls the handler a click would call and
reads the recorded request back. A handler that never reaches ``fetch`` fails too, so
the test cannot pass by testing nothing.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from RevenueChaser.core.tests.board_fixtures import BOARDS
from RevenueChaser.core.tests.board_js import DOM_STUB, function_source, inline_scripts
from RevenueChaser.core.tests.open_issues_shell import strip_open_issues_data

SPY = r"""
var _calls = [];
globalThis.confirm = function(){ return true; };
globalThis.alert = function(){};
globalThis.fetch = function(url, init){
  init = init || {};
  _calls.push({url: String(url), method: init.method || 'GET', headers: init.headers || {}});
  return new Promise(function(){});
};
function _btn(attrs, data){
  var b = _el('btn');
  Object.keys(attrs || {}).forEach(function(k){ b.setAttribute(k, attrs[k]); });
  Object.keys(data || {}).forEach(function(k){ b.dataset[k] = data[k]; });
  return b;
}
"""

# board key -> (JS that presses the buttons, the POST urls it must have sent, in order)
CASES = {
    "module_settings": (
        """
        boardSettingsReset(_btn(), _el('o'), 'module_comovement');
        settingsFetch(_btn({'data-fetch': '/run/universe-stats', 'data-row': 'r1'}));
        var inp = {value: '61', getAttribute: function(k){
          return {'data-setting': 'WINDOW', 'data-orig': '60'}[k]; }};
        document.querySelectorAll = function(s){ return s.indexOf('[data-setting]') >= 0 ? [inp] : []; };
        settingsSave(_btn({'data-board': 'module_comovement'}));
        var named = _btn({'data-name': 'RC-Store-Backup', 'data-act': 'install'});
        autoAct(named);
        """,
        ["/api/settings/board/module_comovement", "/run/universe-stats",
         "/api/settings/board/module_comovement",
         "/api/automations/RC-Store-Backup/install"]),
    "module_leaps": (
        "togglePin(_btn({'data-ticker': 'AAPL'}));",
        ["/api/watchlist/toggle/AAPL"]),
    "module_discord": (
        """
        runJob(_btn({}, {run: '/run/discord', channel: '123'}));
        var box = _el('cb'); box.dataset.channel = '123'; box.checked = true;
        follow(box);
        """,
        ["/run/discord", "/api/discord/follow"]),
    "module_uw_interaction": (
        """
        document.getElementById('uw-tool').value = 'flow_alerts';
        runTool();
        """,
        ["/run/uw"]),
}


def _run(html: str, tail: str, tmp_path) -> list[dict]:
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    path = tmp_path / "board.js"
    path.write_text(DOM_STUB + SPY + "\n;\n".join(inline_scripts(html)) + "\n" + tail
                    + "\nconsole.log(JSON.stringify(_calls));", encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _assert_all_post_with_the_header(calls: list[dict], urls: list[str]) -> None:
    posts = [c for c in calls if c["method"] == "POST"]
    assert [c["url"] for c in posts] == urls
    for c in posts:
        assert str(c["headers"].get("X-RC-Panel", "")).strip(), f"{c['url']} sent no X-RC-Panel: {c}"


@pytest.mark.parametrize("key", sorted(CASES))
def test_every_button_of_a_board_sends_the_panel_header(key, tmp_path, monkeypatch, rawstore_tmp_db):
    tail, urls = CASES[key]
    html = BOARDS[key].render(tmp_path, monkeypatch)
    _assert_all_post_with_the_header(_run(html, tail, tmp_path), urls)


def test_the_shell_sends_the_header_from_fetch_tab_and_cancel_job(tmp_path):
    """The shell's script drives iframes and tabs, so only the two write functions run."""
    from RevenueChaser.app import dashboard
    # Without the open-issues registry block: it is issue text, not the shell's script
    # (core/tests/open_issues_shell.py).
    html = strip_open_issues_data(dashboard.app.test_client().get("/").get_data(as_text=True))
    script = next(s for s in inline_scripts(html) if "function fetchTab" in s)
    prelude = ("var ACTIVE = 'module_leaps', ACTIVE_JOBS = [];"
               "function syncOverlay(){} function startPolling(){} function renderStatus(){}\n")
    funcs = function_source(script, "fetchTab") + "\n" + function_source(script, "cancelJob")
    tail = ("fetchTab('/run/uw', 'tool=x'); fetchTab('/run/swing', null); cancelJob('j1');")
    calls = _run("<script>" + prelude + funcs + "</script>", tail, tmp_path)
    _assert_all_post_with_the_header(calls, ["/run/uw", "/run/swing", "/api/job/j1/cancel"])
