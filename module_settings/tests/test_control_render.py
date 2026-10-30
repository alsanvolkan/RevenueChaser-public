"""The control board's page (design v28): its order, what can be pressed where, escaping, theme and phone rules.

The page is ``control_render.render`` on top of Settings, with the old sections folded below it."""
from __future__ import annotations

import re

import pytest

from RevenueChaser.core.tests.board_fixtures import H
from RevenueChaser.core.tests.board_js import assert_scripts_parse
from RevenueChaser.core.tests.test_board_html_guard import assert_inert
from RevenueChaser.core.tests.test_one_escaper_175 import check_page
from RevenueChaser.module_settings import control, control_render, report
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd
from RevenueChaser.rcdata.api import screen

SECTIONS = ("sec-health", "sec-fetch", "sec-flow", "sec-providers", "sec-data", "sec-crosscheck", "sec-queue")


def _page(nd=None) -> str:
    return report.render_settings_dashboard({"newdata": control_nd() if nd is None else nd})


def _between(html: str, start: str, end: str) -> str:
    a = html.index(start)
    return html[a:html.index(end, a)]


def _section(html: str, sec: str) -> str:
    a = html.index(f'id="{sec}"')
    nxt = [html.find(f'id="{s}"', a + 1) for s in SECTIONS + ("old-settings",)]
    return html[a:min(n for n in nxt if n > a)]


def test_the_board_is_on_top_in_the_designed_order_and_the_old_sections_fold_below():
    html = _page()
    at = [html.index(f'id="{s}"') for s in SECTIONS]
    assert at == sorted(at), "Health, Fetch, the flow diagram, Providers, Data, then cross-check and queue"
    fold = html.index('id="old-settings"')
    assert at[-1] < fold < html.index('id="newdata"') < html.index('id="ticker-drill"')
    assert '<details class="oldfold" id="old-settings">' in html, "folded, not deleted: closed until pressed"


def test_health_fetch_and_the_flow_open_the_rest_closed():
    html = _page()
    for sec in SECTIONS:
        tag = re.search(r'<details class="sec" id="' + sec + r'"( open)?>', html)
        assert tag, sec
        assert bool(tag.group(1)) == (sec in ("sec-health", "sec-fetch", "sec-flow")), sec


def test_the_data_cards_show_bars_and_no_fetch_fill_or_pause():
    data = _section(_page(), "sec-data")
    assert 'data-job=' not in data, "Fetch, Fill, Backfill and Pause are in the Fetch section only"
    assert "meter cov" in data


def test_fetch_and_pause_carry_their_dataset_and_the_scope_flag():
    fetch = _section(_page(), "sec-fetch")
    bars = re.search(r'<button type="button" class="btn go" data-job="fetch" data-dataset="alpaca/bars" '
                     r'data-scope="(\d)">Fetch</button>', fetch)
    assert bars and bars.group(1) == "1", "alpaca/bars asks the names a scope names (honours_scope)"
    assert 'data-job="fetch" data-dataset="sec/13f" data-scope="0"' in fetch
    assert 'data-job="pause" data-dataset="alpaca/bars" aria-pressed="false"' in fetch
    assert 'data-job="fetch" data-dataset="wrds/prices"' not in fetch, "a hand-run dataset has no live Fetch"
    assert 'data-job="copy"' in fetch


def test_the_fill_cells_of_a_calendar_dataset_open_its_calendar(monkeypatch):
    fetch = _section(_page(), "sec-fetch")
    cells = re.findall(r'<button type="button" class="fc [^"]*fopen" data-bf="alpaca-bars" data-g="(\w)"', fetch)
    assert cells == ["A", "E", "R"]
    assert '<div class="bfcal" id="bf-alpaca-bars" data-bf="alpaca-bars" hidden></div>' in fetch


def test_the_fill_switch_is_off_by_default_and_the_page_says_why(monkeypatch):
    monkeypatch.delenv(screen.FILL_ENV, raising=False)
    html = _page()
    data = control_render.client(control.build(control_nd(), now=NOW))
    assert data["fill_enabled"] is False
    assert screen.FILL_OFF_WHY in html
    # The only "Fill this range" is the calendar's, and the script draws it disabled unless the server said so.
    assert "var ready = C.fill_enabled &&" in control_render.CTL_JS
    assert "if (!C.fill_enabled || !rg.picked || !rg.done) return;" in control_render.CTL_JS
    assert "if (!C.fill_enabled) { say(C.fill_off_why); return; }" in control_render.CTL_JS


def test_a_fill_cell_without_a_calendar_is_disabled_until_the_switch(monkeypatch):
    nd = control_nd()
    nd["datasets"].append({**nd["datasets"][0], "dataset": "yfinance/float", "label": "Float (Yahoo)"})
    monkeypatch.delenv(screen.FILL_ENV, raising=False)
    off = _section(_page(nd), "sec-fetch")
    assert re.search(r'data-job="fill" data-dataset="yfinance/float" data-g="A" disabled', off)
    monkeypatch.setenv(screen.FILL_ENV, "1")
    on = _section(_page(nd), "sec-fetch")
    assert re.search(r'data-job="fill" data-dataset="yfinance/float" data-g="A" aria-label', on)


def test_the_universe_bar_offers_a_and_b_and_shows_c_and_d_greyed():
    uni = _between(_page(), 'id="universe"', 'id="uni-draft"')
    offered = re.findall(r'<button type="button" class="useg" data-tier="(\w)"', uni)
    shown = re.findall(r'<span class="useg uoff" role="img" data-tier="(\w)"', uni)
    assert offered == ["A", "E", "R"] and shown == ["C", "D"]
    assert uni.count('<span class="uoffl">not offered</span>') == 2


def test_the_script_uses_only_the_routes_the_page_names():
    js = control_render.CTL_JS
    assert "'/api/" not in js and '"/api/' not in js, "routes come from CTL.routes"
    assert set(control_render.ROUTES.values()) == {
        "/api/build/request/", "/api/build/schedule/", "/api/build/queue", "/api/settings/board/module_settings",
        "/api/settings/sample/", "/api/job/"}
    assert "X-RC-Panel" in js


def test_no_timer_repeats_and_every_storage_access_is_guarded():
    js = control_render.CTL_JS
    assert "setInterval" not in js
    for m in re.finditer(r"localStorage\.\w+\(", js):
        before = js[:m.start()]
        assert before.rfind("try") > before.rfind("}"), f"unguarded {m.group(0)}"


def test_every_hostile_string_is_escaped_and_the_scripts_parse(tmp_path):
    nd = control_nd()
    for c in nd["datasets"]:
        c["label"] = H("label")
        c["status_reason"] = H("reason")
        c["issues"] = [{"kind": H("kind"), "key": H("key"), "detail": H("detail"), "found_on": "2026-10-28"}]
        c["recent"] = [{"checked_on": H("checked"), "result": H("result"), "note": H("note")}]
        c["samples"] = [{"table": "price_bar_daily", "built": True, "columns": [{"name": H("col"), "type": "TEXT"}],
                         "rows": [[H("cell")]], "notes": (H("sample_note"),)}]
    nd["queue"]["active"][0]["note"] = H("queue_note")
    nd["universe"]["tickers"]["E"] = [H("ticker")]
    html = _page(nd)
    ctl = _between(html, '<section id="ctl"', '<details class="oldfold"')
    for tag in ("label", "reason", "kind", "detail", "note", "col", "cell", "queue_note"):
        assert f"onerror=__x(&#x27;{tag}T&#x27;)" in ctl or tag in ("ticker",), tag
    assert_inert(html)
    check_page(html)
    assert_scripts_parse(html, tmp_path)


def test_the_css_is_scoped_to_the_board_and_themed_both_ways():
    css = control_render.CTL_CSS
    assert ':root[data-theme="dark"] #ctl{' in css
    assert '@media (prefers-color-scheme: dark){:root:not([data-theme="light"]) #ctl{' in css
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for sel in re.findall(r"(?:^|})\s*([^{}@]+)\{", body):
        for one in sel.split(","):
            one = one.strip()
            if not one or re.match(r"^(from|to|\d+%)$", one):
                continue
            assert one.startswith(("#ctl", ':root[data-theme="dark"] #ctl', ':root:not([data-theme="light"]) #ctl',
                                   "#toast")), f"unscoped rule: {one}"


def test_the_phone_rules_hold():
    css = control_render.CTL_CSS
    assert "min-height:44px" in css
    assert re.search(r"@media\(max-width:\d+px\)", css), "the layout narrows to one column on a phone"
    assert "overflow-x:hidden" not in css


def test_a_board_that_could_not_be_built_is_one_line_and_the_rest_of_the_page_stays(monkeypatch):
    monkeypatch.setattr(control, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    html = _page()
    assert "Could not read</b> · RuntimeError: boom" in html
    assert 'id="old-settings"' in html and 'id="newdata"' in html


def test_the_page_names_the_icons_licence():
    assert "Tabler Icons (MIT licence)" in _page()


def test_the_calendar_counts_only_the_universes_sets_on_each_session(tmp_path):
    """Review M1, on the page: a session's per-set counts are summed over the applied sets only, so a fund-only
    universe is not painted filled by Tier A's bars."""
    import json
    import shutil
    import subprocess
    from RevenueChaser.core.tests.board_js import function_source
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    src = function_source(control_render.CTL_JS, "sessionN")
    day = {"A": 5600, "E": 480, "R": 1300, "C": 40, "-": 2}
    probe = tmp_path / "s.js"
    probe.write_text(src + "\nconsole.log(JSON.stringify([sessionN(%s, {E: true}), sessionN(%s, {A: true, E: true, R: true}),"
                     " sessionN(undefined, {A: true}), sessionN(%s, {})]));" % ((json.dumps(day),) * 3),
                     encoding="utf-8")
    out = subprocess.run(["node", str(probe)], capture_output=True, text=True, timeout=60)
    assert json.loads(out.stdout) == [480, 7380, 0, 0], out.stderr
    assert "c.universe ||" not in control_render.CTL_JS, "the fill state compares with the applied sets' size"


def test_the_bits_apply_saves_are_the_servers_own_maps():
    """Review S4: the page computes what Apply saves from these maps, so they are the server's, not a copy."""
    from RevenueChaser.core import board_settings
    data = control_render.client(control.build(control_nd(), now=NOW))
    assert data["universe"]["bits"] is board_settings.UNIVERSE_SET_BITS
    assert data["universe"]["inc_bits"] is board_settings.UNIVERSE_INCLUDE_BITS


def _flow_js() -> str:
    """The flow diagram's own block of ``control.js`` (it has its own ``paint`` and ``hit``)."""
    js = control_render.CTL_JS
    return js[js.index("/* The flow diagram is the filter"):]


def test_a_press_in_the_flow_diagram_leaves_the_page_where_it_is():
    """avo, 30 Oct: pressing a value must not carry the page to the cards; only the press on 'open them' does."""
    from RevenueChaser.core.tests.board_js import function_source
    flow = _flow_js()
    assert "scrollIntoView" not in function_source(flow, "paint"), "a pick never scrolls"
    assert flow.count("scrollIntoView") == 1, "only the 'open them ↓' button carries the reader down"


def test_a_health_badge_picks_its_own_file():
    """avo, 30 Oct: the 6/7 on spine.duckdb picks that file, not every file whose health reads the same."""
    from RevenueChaser.core.tests.board_js import function_source
    flow = _flow_js()
    assert "picks.hs" not in flow, "the badge picks a file, so there is no health-state pick left"
    hit = function_source(flow, "hit")
    assert "'file:' + t.getAttribute('data-file')" in hit, "the badge picks the file it sits in"


def test_a_pick_closes_the_data_cards_and_opens_the_one_that_is_left():
    """avo, 30 Oct: everything below collapses and only the card holding the pressed value stays open."""
    from RevenueChaser.core.tests.board_js import function_source
    paint = function_source(_flow_js(), "paint")
    assert "c.open = false" in paint, "every card closes on a pick"
    assert "shown === 1" in paint, "the single card that matches opens by itself"
