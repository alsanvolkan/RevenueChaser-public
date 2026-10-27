"""The Runs tab on a phone: the table stacks instead of scrolling sideways, its controls are 44 px touch
targets, and it renders in light and in dark (#131, the owner's phone rule in CLAUDE.md).

The page is rendered in-process from a payload (``runview.render_page``) and opened as a file, with
``fetch`` stubbed so its 5 s poll stays off the network. Chrome is driven over DevTools
(``harness/cdp_browser.py``) because only device emulation gives an exact 390 px viewport.
"""
from __future__ import annotations

import os

import pytest

from RevenueChaser.app import runview

from .harness.cdp_browser import Page, find_chrome

PHONE = (390, 844)
DESKTOP = (1280, 800)
# The dashboard's dark palette, which the dark scheme must keep exactly (templates/runs.html.j2 --bg).
DARK_BG = "rgb(5, 17, 31)"

_HOOK = "<script>window.fetch=function(){return new Promise(function(){});};</script>"

ROWS = [{"run_id": f"r{i}", "module": name, "source": None, "trigger": trig, "status": status,
         "queued_at": 1_792_700_000.0 + i, "started_at": 1_792_700_001.0 + i,
         "finished_at": 1_792_700_061.0 + i, "duration_s": 60.0 + i,
         "error": "Traceback (most recent call last): a long error line that would not fit" if status == "error"
         else None,
         "message": None, "params": {"args": ["-m", "RevenueChaser.scripts.nightly_inputs", "earnings",
                                              "--max-uw-calls", "200", "--max-yf-calls", "2000"]}}
        for i, (name, trig, status) in enumerate([("RC-Daily-Bars-" + "x" * 74, "scheduled", "done"),   # 88 chars
                                                  ("rotation", "manual", "error"),
                                                  ("module_leaps", None, "running")])]

# What a layout check reads off the page, in one round trip.
MEASURE = r"""(function(){
  var vw = document.documentElement.clientWidth;
  function box(el){ var r = el.getBoundingClientRect(); return {w: r.width, h: r.height, l: r.left, t: r.top}; }
  var row = document.querySelector("#runs-body tr:nth-child(2)");   // the error row: every cell filled
  var cells = Array.prototype.map.call(row.children, function(td){
    return {box: box(td), label: getComputedStyle(td, "::before").content, shown: td.innerText};
  });
  var heads = Array.prototype.map.call(document.querySelectorAll("thead th"), function(th){ return th.textContent; });
  var wide = Array.prototype.filter.call(document.querySelectorAll("body *"), function(el){
    var r = el.getBoundingClientRect(); return r.width > 0 && r.right > vw + 0.5;
  }).map(function(el){ return el.tagName + (el.id ? "#" + el.id : "") + "." + el.className; });
  var targets = {};
  ["runs-filter", "runs-limit", "runs-pause"].forEach(function(id){ targets[id] = box(document.getElementById(id)); });
  targets["runs-problems (label)"] = box(document.querySelector('label[for="runs-problems"]'));
  var cs = getComputedStyle(document.body);
  return {vw: vw, scrollW: document.documentElement.scrollWidth, wide: wide.slice(0, 8),
          theadShown: box(document.querySelector("thead")).h > 0, heads: heads, cells: cells,
          targets: targets, bg: cs.backgroundColor, fg: cs.color,
          cardBg: getComputedStyle(row).backgroundColor};
})()"""


@pytest.fixture(scope="module")
def browser():
    chrome = find_chrome()
    if chrome is None:
        msg = "no Chrome/Chromium for the phone layout check: set RC_CHROME or install google-chrome / chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg + " (a skip on CI would leave the check unenforced)")
        pytest.skip(msg)
    with Page(chrome) as page:
        yield page


@pytest.fixture(scope="module")
def runs_page(tmp_path_factory):
    html = runview.render_page({"initial": {"rows": ROWS, "total": 3, "shown": 3, "limit": 200,
                                            "problems": False, "query": "q" * 100}})   # one long, unbroken value
    i = html.lower().index("<head>") + len("<head>")
    path = tmp_path_factory.mktemp("runs") / "runs.html"
    path.write_text(html[:i] + _HOOK + html[i:], encoding="utf-8")
    return path.as_uri()


def _measure(browser, url, size, scheme, theme=None):
    """``scheme`` is the device's; ``theme`` is the shell's choice, copied onto the page as data-theme."""
    browser.emulate(*size, scheme=scheme)
    browser.open(url)
    if theme is not None:
        browser.eval(f"document.documentElement.setAttribute('data-theme', {theme!r})")
    return browser.eval(MEASURE)


def _luma(rgb: str) -> float:
    r, g, b = (int(x) for x in rgb[rgb.index("(") + 1:rgb.index(")")].split(",")[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_runs_table_stacks_on_a_phone(browser, runs_page, scheme):
    m = _measure(browser, runs_page, PHONE, scheme, theme=scheme)
    assert m["vw"] == PHONE[0]
    assert m["scrollW"] <= m["vw"], f"the page scrolls sideways ({m['scrollW']} > {m['vw']}): {m['wide']}"
    assert not m["wide"], f"elements reach past the right edge: {m['wide']}"
    assert not m["theadShown"], "the column header row still takes a line of its own"
    tops = [c["box"]["t"] for c in m["cells"]]
    assert tops == sorted(tops) and len(set(tops)) == len(tops), "a row's cells are side by side, not stacked"
    # Each stacked cell names its column, and the names are the header's own.
    assert [c["label"].strip('"') for c in m["cells"]] == m["heads"]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_runs_controls_are_touch_sized_on_a_phone(browser, runs_page, scheme):
    m = _measure(browser, runs_page, PHONE, scheme, theme=scheme)
    small = {k: (round(b["w"]), round(b["h"])) for k, b in m["targets"].items() if b["h"] < 44 or b["w"] < 44}
    assert not small, f"touch targets under 44 px: {small}"


def test_the_runs_tab_has_a_light_and_a_dark_scheme(browser, runs_page):
    dark = _measure(browser, runs_page, PHONE, "dark", theme="dark")
    light = _measure(browser, runs_page, PHONE, "light", theme="light")
    assert dark["bg"] == DARK_BG, "the dark look must stay the dashboard's own"
    assert _luma(light["bg"]) > 200, f"light scheme background is {light['bg']}"
    assert _luma(light["fg"]) < 100, f"light scheme text is {light['fg']}"
    assert _luma(dark["fg"]) > 150


# Dark is the default on every device (docs/dashboard_ui.md, avo 25 Oct 2026): no data-theme is dark even on a
# light device; light shows only when chosen ("light"), or under "auto" on a light device.
@pytest.mark.parametrize("scheme, theme, want", [
    ("light", None, "dark"),
    ("dark", None, "dark"),
    ("light", "dark", "dark"),
    ("dark", "light", "light"),
    ("light", "auto", "light"),
    ("dark", "auto", "dark"),
])
def test_the_runs_tab_is_dark_unless_light_is_chosen(browser, runs_page, scheme, theme, want):
    m = _measure(browser, runs_page, PHONE, scheme, theme=theme)
    if want == "dark":
        assert m["bg"] == DARK_BG, f"device {scheme}, data-theme {theme!r}: background {m['bg']}, not dark"
    else:
        assert _luma(m["bg"]) > 200, f"device {scheme}, data-theme {theme!r}: background {m['bg']}, not light"


def test_the_runs_table_stays_a_table_on_a_desktop(browser, runs_page):
    m = _measure(browser, runs_page, DESKTOP, "dark")
    assert m["theadShown"], "the desktop keeps its header row"
    tops = {round(c["box"]["t"]) for c in m["cells"]}
    assert len(tops) == 1, "the desktop keeps one line per run"
    assert m["scrollW"] <= m["vw"]


# ── Settings → Automations, the same problem one tab over ─────────────────────

AUTOMATIONS = r"""(function(){
  var s = document.querySelector(".auto-item>summary"), panel = s.closest(".panel");
  var btns = Array.prototype.map.call(document.querySelectorAll(".auto-btn"), function(b){
    var r = b.getBoundingClientRect(); return [b.textContent, Math.round(r.width), Math.round(r.height)];
  });
  return {summaryW: s.scrollWidth, panelW: panel.clientWidth, panelScroll: panel.scrollWidth,
          summaryH: s.getBoundingClientRect().height, buttons: btns};
})()"""


@pytest.fixture
def settings_page(tmp_path, monkeypatch):
    from RevenueChaser.core import rawstore
    from RevenueChaser.core.tests.board_fixtures import BOARDS, no_comment_opener
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "raw.duckdb")
    rawstore.ensure_all_tables()
    with no_comment_opener():
        html = BOARDS["module_settings"].render(tmp_path, monkeypatch)
    i = html.lower().index("<head>") + len("<head>")
    path = tmp_path / "settings.html"
    path.write_text(html[:i] + _HOOK + html[i:], encoding="utf-8")
    return path.as_uri()


def test_an_automation_row_fits_a_phone(browser, settings_page):
    """Its one-line summary was a five-column grid ~880 px wide that scrolled sideways inside the panel."""
    browser.emulate(*PHONE, scheme="dark")
    browser.open(settings_page)
    m = browser.eval(AUTOMATIONS)
    assert m["panelScroll"] <= m["panelW"], f"the Automations panel scrolls sideways: {m}"
    assert m["summaryH"] >= 44, "the row's summary is the touch target that opens it"
    small = [b for b in m["buttons"] if b[2] < 44]
    assert not small, f"automation buttons under 44 px: {small}"


ERROR = ROWS[1]["error"]


def test_a_phone_shows_the_whole_note_and_params(browser, runs_page):
    """A touch screen never shows a ``title`` tooltip, so the clipped cell was all a phone could read."""
    m = _measure(browser, runs_page, PHONE, "dark")
    note, params = m["cells"][5]["shown"], m["cells"][6]["shown"]
    assert ERROR in note, f"the phone shows only {note!r}"
    assert "--max-yf-calls" in params and "2000" in params, f"the phone shows only {params!r}"


def test_a_desktop_still_clips_the_long_cells(browser, runs_page):
    m = _measure(browser, runs_page, DESKTOP, "dark")
    assert m["cells"][5]["shown"].endswith("…") and ERROR not in m["cells"][5]["shown"]
