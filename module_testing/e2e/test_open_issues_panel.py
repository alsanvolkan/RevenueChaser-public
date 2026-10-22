"""The "Open issues" group in a real browser (#383): collapsed, opens, follows the tab, links are safe.

The unit tier checks the payload and the escaping of the embedded JSON. Only a browser proves what the client
renderer does with it: that the group starts collapsed above the board, that one click reveals every issue's
one-liner, that ``showTab`` switches the list without a page load, that Logs hides the group, that every link opens
in a new tab, and that a hostile title in the registry renders as text and runs nothing (ADR-X1 §5).

The page under test is the live dashboard's ``/``, served by the harness subprocess and then loaded from a file so
Chrome's ``--dump-dom`` can run it with no network. ``fetch`` is stubbed before the page's own scripts run, so the
iframe polling never reaches a provider.
"""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from RevenueChaser.core import open_issues
from RevenueChaser.core.html_safe import script_json

_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
URL_PREFIX = "https://github.com/alsanvolkan/"
# The board the probe starts on: the registry gives it board-scoped entries, so its list is never empty.
BOARD_WITH_ENTRIES = "module_sec_13f"

# Before the shell's own scripts: record script hits, silence alerts, and keep every request pending.
_HOOK = ("<script>window.__x=function(t){var d=document.documentElement;"
         "d.setAttribute('data-xss',(d.getAttribute('data-xss')||'')+t+' ');};"
         "window.alert=function(){};window.confirm=function(){return false;};"
         "window.fetch=function(){return new Promise(function(){});};</script>")

# After load: open the group, read it, then switch tabs and read again; the report lands in <pre id="rc-e2e-probe">.
def _probe(board_with_entries: str, board_without: str) -> str:
    """The probe for these two boards: the first has board-scoped issues, the second has none."""
    return (r"""<script>
window.addEventListener('load', function(){ setTimeout(function(){
  var out = [], box = document.getElementById('rc-open-issues');
  function add(k, v){ out.push(k + '=' + v); }
  function text(el){ return el ? el.textContent : ''; }
  function titles(){
    return Array.from(box.querySelectorAll('.rc-oi-title')).map(function(t){ return t.textContent; }).join(' | ');
  }
  add('initial.hidden', box.hidden);
  add('initial.open', box.open);
  document.getElementById('rc-open-issues-summary').click();
  add('opened.open', box.open);
  add('opened.summary', text(document.getElementById('rc-open-issues-summary')));
  add('opened.sections', Array.from(box.querySelectorAll('.rc-oi-sec h4')).map(text).join(' | '));
  add('opened.titles', titles());
  add('opened.line', text(box.querySelector('.rc-oi-line')));
  var links = Array.from(box.querySelectorAll('a'));
  add('links.count', links.length);
  add('links.bad', links.filter(function(a){
    return a.getAttribute('target') !== '_blank' || a.getAttribute('rel') !== 'noopener' ||
           (a.getAttribute('href') || '').indexOf('https://github.com/alsanvolkan/') !== 0;
  }).length);
  showTab('__WITH__');
  add('with.summary', text(document.getElementById('rc-open-issues-summary')));
  add('with.titles', titles());
  showTab('__WITHOUT__');
  add('without.summary', text(document.getElementById('rc-open-issues-summary')));
  add('without.titles', titles());
  showTab('logs');
  add('logs.hidden', box.hidden);
  showTab('__WITH__');
  add('back.summary', text(document.getElementById('rc-open-issues-summary')));
  var pre = document.createElement('pre'); pre.id = 'rc-e2e-probe';
  pre.textContent = out.join('\n'); document.body.appendChild(pre);
}, 0); });
</script>""").replace("__WITH__", board_with_entries).replace("__WITHOUT__", board_without)

_HOSTILE_PROBE = r"""<script>
window.addEventListener('load', function(){ setTimeout(function(){
  showTab('module_sec_13f');
  var box = document.getElementById('rc-open-issues');
  box.open = true;
  var pre = document.createElement('pre'); pre.id = 'rc-e2e-probe';
  pre.textContent = 'title=' + (box.querySelector('.rc-oi-title') || {}).textContent +
                    '\nline=' + (box.querySelector('.rc-oi-line') || {}).textContent;
  document.body.appendChild(pre);
}, 0); });
</script>"""


def _board_without_its_own_issue() -> str:
    """A data board the registry has no board-scoped entry for (computed, so a re-seeded registry cannot break it).

    The panel's summary says "0 on this board" for that board. Hard-coding one would tie the test to today's seed.
    """
    from RevenueChaser.app import dashboard

    keys = [t["key"] for t in dashboard.TABS
            if t["type"] == "tab" and t["key"] not in open_issues.NOT_A_DATA_BOARD]
    registry = open_issues.load()
    named = {board for e in registry.issues if e.scope == "board" for board in e.boards}
    empty = [k for k in keys if k not in named and k != BOARD_WITH_ENTRIES]
    assert empty, "every data board has a board-scoped issue: the panel's empty branch has no board to show it on"
    return empty[0]


def _chrome() -> str | None:
    override = os.environ.get("RC_CHROME")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    for name in _CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(scope="module")
def chrome() -> str:
    path = _chrome()
    if path is None:
        msg = "no Chrome/Chromium for the shell test: set RC_CHROME or install google-chrome / chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg + " (a skip on CI would leave the panel unchecked)")
        pytest.skip(msg)
    return path


def _with_probe(html: str, probe: str) -> str:
    html = re.sub(r"^(\ufeff?\s*<!doctype[^>]*>)?", lambda m: (m.group(1) or "") + _HOOK,
                  html, count=1, flags=re.I)
    i = html.lower().rfind("</body>")
    return html[:i] + probe + html[i:] if i >= 0 else html + probe


def _dump(chrome: str, page: Path, profile: Path) -> str:
    out = subprocess.run(
        [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
         "--disable-extensions", f"--user-data-dir={profile}",
         "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
         "--virtual-time-budget=4000", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert "<html" in out.stdout.lower(), f"Chrome dumped no DOM: {out.stderr[-1500:]}"
    return out.stdout


def _probe_parse(dom: str) -> dict:
    m = re.search(r'<pre id="rc-e2e-probe">(.*?)</pre>', dom, re.S)
    assert m, "the probe never ran (the shell's script failed?)"
    text = html_mod.unescape(m.group(1))
    report = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            report[key] = value
    return report


def test_the_group_is_collapsed_opens_and_follows_the_tab(dashboard, chrome, tmp_path):
    other = _board_without_its_own_issue()
    page = tmp_path / "shell.html"
    page.write_text(_with_probe(dashboard.get("/").text, _probe(BOARD_WITH_ENTRIES, other)), encoding="utf-8")
    report = _probe_parse(_dump(chrome, page, tmp_path / "profile"))

    assert report["initial.hidden"] == "false", "a data board shows the group"
    assert report["initial.open"] == "false", "it starts collapsed"
    assert report["opened.open"] == "true", "one click opens it"
    assert re.fullmatch(r"Open issues — \d+ on this board, \d+ shared · as of \d{1,2} \w{3} \d{4}.*",
                        report["opened.summary"]), report["opened.summary"]
    assert report["opened.sections"].startswith("This board (")
    assert "Affects every board (" in report["opened.sections"]
    assert report["opened.titles"].startswith("#"), "items are listed by number and title"
    assert len(report["opened.line"]) > 40, "the one-liner is visible with the group open"
    assert report["links.count"] and report["links.bad"] == "0", "every link is a safe new-tab link"

    # showTab switches the list with no page load, and the counts are the registry's, not the seed's:
    # the registry gives SEC 13F board-scoped entries and `other` none.
    assert report["with.summary"] != report["without.summary"], "a tab switch re-renders the list"
    assert " on this board" in report["with.summary"]
    assert report["without.summary"].startswith("Open issues — 0 on this board"), \
        f"{other} has no board-scoped entry in the registry, so its list starts at 0: {report['without.summary']}"
    assert report["with.titles"] != report["without.titles"]
    assert report["logs.hidden"] == "true", "Logs is not a data board: no panel"
    assert report["back.summary"] == report["with.summary"], "coming back restores the same list"


def test_a_hostile_issue_title_runs_no_script(dashboard, chrome, tmp_path):
    hostile = "</script><script>window.__x('title')</script>"
    entry = {
        "repo": "RevenueChaser", "number": 1, "url": f"{URL_PREFIX}RevenueChaser/issues/1",
        "title": hostile, "state": "open", "labels": [], "boards": ["module_sec_13f"],
        "scope": "board", "kind": "bug", "status": "open", "stored_status": "open",
        "deferred_until": "", "summary": '"><img src=x onerror=window.__x("line")>',
        "detail": {"whats_wrong": "<img src=x onerror=window.__x(\"detail\")>"},
        "refs": [], "issue_updated_at": "2026-10-22T00:00:00Z", "reviewed_at": "2026-10-22",
    }
    # The shell's shape: every entry once under "entries", each board names its keys (#21).
    payload = {
        "entries": {"RevenueChaser#1": entry},
        "boards": {
            "module_sec_13f": {
                "board": ["RevenueChaser#1"], "shared": [], "repo": [],
                "generated_at": "2026-10-22T10:30:00Z", "stale_days": 0, "dropped_links": 0,
            },
        },
    }
    page_html = dashboard.get("/").text
    head, rest = page_html.split('id="rc-open-issues-data">', 1)
    page_html = head + 'id="rc-open-issues-data">' + script_json(payload) + "</script>" + rest.split("</script>", 1)[1]
    page = tmp_path / "hostile.html"
    page.write_text(_with_probe(page_html, _HOSTILE_PROBE), encoding="utf-8")
    dom = _dump(chrome, page, tmp_path / "profile")

    assert 'data-xss="' not in dom, "the hostile title executed"
    assert "<script>window.__x('title')" not in dom, "the hostile title became a script tag"
    assert "<img src=x" not in dom, "the summary rendered as markup"
    assert "&lt;img src=x" in dom, "the summary is on the page as text"
    report = _probe_parse(dom)
    assert report["title"].endswith(hostile), "the title is still shown, as text"
