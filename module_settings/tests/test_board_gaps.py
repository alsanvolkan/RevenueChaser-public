"""The gaps left after #531 (avo, 6 Nov 2026): a fixed 1600 px layout zoomed to the shell's pane, never centred and
never scrolled sideways (Q48, Q54, Q55); one-line dataset heads; no repeated status words; the builder timer read
from its systemd system unit (#528)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from RevenueChaser.module_settings import control, control_render
from RevenueChaser.module_settings.tests.test_control_render_c1 import _box, _sources, _view

ROOT = Path(control.__file__).resolve().parents[1]
STATIC = ROOT / "module_settings" / "static"


def _css(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_shell_gives_the_settings_board_the_pane_not_its_natural_width():
    html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    fill = html.split("var FILL_WIDTH")[1].split("};")[0]
    assert "'module_settings': true" in fill, "a grown frame is what scrolled sideways on avo's 969 px pane"


def test_both_tabs_share_one_left_edge_never_centred():
    assert ".ctl .wrap{max-width:1600px;margin:0;" in _css("control.css")
    needs = _css("needs.css")
    assert "#needs .wrap{max-width:1600px;margin:0;" in needs
    assert "#settings-tabs .tabs{display:flex;gap:4px;max-width:1600px;margin:0}" in needs
    for name in ("control.css", "needs.css"):
        assert not re.search(r"\.wrap\{[^}]*margin:0 auto", _css(name)), f"{name} centres a page container"


def test_the_page_is_zoomed_to_the_pane_and_reflows_only_on_a_phone():
    js = _css("control.js")
    fit = js[js.index("var DESIGN = 1600"):]
    assert "MIN_ZOOM" not in fit, "no lower bound: only the phone reflows (avo 20:25)"
    assert "window.parent.isNarrow" in fit, "the shell's own phone breakpoint decides the reflow"
    assert "clientWidth || DESIGN) / DESIGN" in fit and "new ResizeObserver(fit)" in fit
    assert "'#ctl, #needs, #settings-tabs'" in fit


def test_the_tree_lines_divide_the_zoom_out():
    js = _css("data_tree.js")
    assert "hr.width / h.offsetWidth" in js
    draw = js[js.index("var hr = h.getBoundingClientRect();"):js.index("follow(j);\n  }")]
    assert draw.count(".getBoundingClientRect()") == 2, "every other rect goes through R(), in unzoomed units"


def test_the_dataset_head_keeps_its_buttons_on_the_name_line():
    box = _box(_sources(_view(), "prices.duckdb"), "alpaca/bars")
    head = box[:box.index('class="sb2"')]
    assert 'class="dsname"' in head and 'class="sbbtn"' in head, "the buttons sit in the same head as the name"
    css = _css("control.css")
    assert css.index(".ctl .sb1{flex-wrap:nowrap}") > css.index(".ctl .sb1{display:flex;flex-wrap:wrap"), \
        "the later desktop rule keeps the head one line"
    assert ".ctl .sb1 .dsname{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis" in css


def test_the_status_line_drops_what_the_select_and_the_switch_already_say():
    v = _view()
    for f in v["files"]:
        for d in f["datasets"]:
            d["next_words"] = control_render.SAID_BY_THE_SELECT
            d["since_words"] = "fetch on"
    state = (lambda b: b[b.index('class="sb2"'):b.index('class="sbody"')])(
        _box(_sources(v, "prices.duckdb"), "alpaca/bars"))
    assert "automation off" not in state and "fetch on" not in state
    assert "⏱ daily" in state, "the cadence stays"
    for f in v["files"]:
        for d in f["datasets"]:
            d["since_words"] = "paused · since Fri 6 Nov, 20:00 by avo"
    paused = _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    assert "paused · since Fri 6 Nov, 20:00 by avo" in paused, "a pause is news: it stays"


def test_the_schedule_row_is_one_line():
    css = _css("control.css")
    assert css.index(".ctl .schedset{flex-wrap:nowrap}") > css.index(".ctl .schedset{display:flex;flex-wrap:wrap")
    assert '.ctl .schedset > [role="status"]{flex:1 1 auto;min-width:0' in css


def _runner(out: str = "", fail: bool = False):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if fail:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    run.calls = calls
    return run


STOPPED = ("LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled\nNextElapseUSecRealtime=\n"
           "LastTriggerUSec=@1793887620\n")


def test_the_timer_is_read_from_its_system_unit_and_stopped_is_off():
    run = _runner(STOPPED)
    got = control.read_timer(run=run)
    assert run.calls[0][:3] == ["systemctl", "show", "life-rc-builder.timer"] and "--user" not in run.calls[0]
    assert got["state"] == "off", "enabled but stopped starts nothing: off (#528)"
    assert got["last"] == "2026-11-05T14:07:00+00:00" and got["next"] is None
    assert isinstance(got["minute"], int)
    assert control.read_timer(run=_runner(STOPPED.replace("=inactive", "=active")))["state"] == "on"


def test_a_timer_that_is_not_there_or_cannot_be_read_is_not_known():
    assert control.read_timer(run=_runner("LoadState=not-found\nActiveState=inactive\n")) == control.NO_TIMER
    assert control.read_timer(run=_runner(fail=True)) == control.NO_TIMER


def test_the_off_timer_names_the_builders_last_run_from_the_queue():
    v = {"switches": {}, "gateway": {}, "timer": {"state": "off"}, "builder_run_at": "2026-11-06T15:26:00+00:00"}
    bar = control_render._topbar(v)
    assert '<span class="swt tmr none"><b>Builder timer: off</b>' in bar, "grey, no alarm"
    assert "last run 16:26, from the queue" in bar


def test_the_example_rows_press_is_a_small_refresh_under_the_table_title():
    from RevenueChaser.module_settings.tests.test_control_render_c2 import _file, _fund_sample
    html = control_render._samples(_file([_fund_sample()]))
    assert 'class="btn sm"' in html and ">↻ Refresh</button>" in html and 'aria-label="Refresh the example rows"' in html


def test_the_run_conditions_say_only_what_systemd_has_off_windows():
    from RevenueChaser.core import automations
    s = automations.entry("RC-Build").settings
    assert s.summary(windows=False) == "catches up a missed run, 120 min limit"
    assert "only when logged on" in s.summary(windows=True), "Windows keeps its Task Scheduler words"


def test_two_dataset_cards_a_row_on_every_desktop_pane_and_one_on_a_phone():
    css = _css("control.css")
    tail = css[css.index("iki kutu yan yana"):]
    assert "@media(max-width:900px){.ctl .sgrid{grid-template-columns:1fr}}" in tail
    assert ".ctl .sgrid{grid-template-columns:repeat(2,minmax(0,1fr))}" in tail
    assert ".ctl .sbox .btn{min-height:30px" in tail, "denser on a desktop; the phone keeps 44 px targets"


def _desktop_breakpoints(css: str):
    """Each ``@media(max-width:N)`` / ``@media(min-width:N)`` block with 901 < N <= 1600, as (kind, N, its rules)."""
    for m in re.finditer(r"@media\s*\((max|min)-width:([\d.]+)px\)\s*\{", css):
        n = float(m.group(2))
        if not 901 < n <= 1600:   # 900 / 901 is the phone breakpoint, the one reflow there is
            continue
        j, depth = m.end(), 1
        while depth:
            depth += (css[j] == "{") - (css[j] == "}")
            j += 1
        yield m.group(1), n, re.findall(r"([^{}]+)\{[^{}]*\}", css[m.end():j - 1]), j


def test_the_zoomed_1600_layout_takes_no_breakpoint_between_the_phone_and_1600():
    """At a 969 px pane the page is the 1600 layout zoomed down: a breakpoint that sees the pane must not reflow it."""
    js = _css("control.js")
    assert "document.documentElement.classList.toggle('z1600', !phone)" in js
    found = []
    for name in ("control.css", "needs.css", "data_tree.css"):
        css = _css(name)
        for kind, n, selectors, end in _desktop_breakpoints(css):
            for sel in (s.strip() for group in selectors for s in group.split(",")):
                found.append((name, kind, n, sel))
                if kind == "max":
                    assert sel.startswith(":where(html:not(.z1600)) "), f"{name} @{kind}-width:{n}: {sel}"
                else:
                    twin = ":where(html.z1600) " + sel
                    assert twin in css[end:end + 400], f"{name} @{kind}-width:{n} has no z1600 twin: {sel}"
    assert len(found) >= 30, f"the scan found the breakpoints: {len(found)}"
