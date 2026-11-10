"""#527 step 9 (avo 6 Nov 2026): the Health page and the Catalog get the Data page's principles. What needs avo comes
first and open; what is fine is folded. The Catalog's problem groups lead and are open, a group head counts its
missing needs, the short columns never wrap and Read by wraps. The Data page has one "What am I looking at?", the top
bar (switches, IBKR gateway with its login link) heads both tabs, and the Health tab's order puts the queue, the
backups and the catalog before the reference parts, which are closed."""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control_render, needs_tab, report
from RevenueChaser.module_settings.tests.test_needs_tab import _model, _summary
from RevenueChaser.module_settings.tests.test_report import _panel, _payload


def _heads(html: str) -> list[tuple[str, str]]:
    return re.findall(r'<tbody class="cghead" data-file="([^"]+)"(?: data-bad)?><tr><td colspan="7">(.*?)</td></tr></tbody>', html,
                      flags=re.S)


def _state(bad: dict[str, str]):
    return lambda n: {**_summary(n), "state": bad.get(n, "fresh")}


def test_problem_groups_come_first_and_open_and_fresh_groups_stay_closed():
    m = _model()
    files = [f for f in needs_tab.SERVED_FILES if any(r["kind"] == f for r in m["catalog"])]
    last = files[-1]
    victim = next(r["need"] for r in m["catalog"] if r["kind"] == last)
    m = _model(summarize=_state({victim: "missing"}))
    html = needs_tab.render(m)
    heads = _heads(html)
    assert heads[0][0] == last, "the group holding a missing need leads"
    assert 'aria-expanded="true"' in heads[0][1] and "▾" in heads[0][1]
    assert f'<tbody class="cbody" id="cg-{last}">' in html, "its rows show at load"
    for f, head in heads[1:]:
        assert 'aria-expanded="false"' in head, f


def test_a_group_head_counts_its_missing_needs_and_its_problem_rows_lead():
    m = _model()
    rows = [r["need"] for r in m["catalog"] if r["kind"] == "prices"]
    m = _model(summarize=_state({rows[-1]: "missing"}))
    html = needs_tab.render(m)
    head = dict(_heads(html))["prices"]
    assert f"{len(rows)} needs · fresh {len(rows) - 1} · stale 0 · missing 1" in head
    body = html.split('id="cg-prices"', 1)[1].split("</tbody>", 1)[0]
    assert re.findall(r'data-need="([^"]+)"', body)[0] == rows[-1]


def test_the_catalogs_short_columns_never_wrap_and_read_by_wraps():
    css = needs_tab.NEEDS_CSS.split("#527", 1)[-1]
    for label in ("State", "Newest day", "Rows", "Fresh if"):
        assert f'#needs td[data-label="{label}"]' in css, label
    assert "white-space:nowrap" in css
    assert re.search(r'#needs td\[data-label="Read by"\]\{[^}]*min-width:0', css)


def test_the_data_page_has_one_what_am_i_looking_at():
    from RevenueChaser.module_settings.tests.test_control_view import control_nd
    html = report.render_settings_dashboard(dict(_payload(), newdata=control_nd()))
    data = _panel(html, "panel-data")
    assert data.count("What am I looking at?") == 1
    assert 'aria-controls="wial"' not in data
    assert "What a press does." in data, "the board's own help moved into the shared box"


def test_the_top_bar_heads_the_fetch_health_tab_and_carries_the_gateway_login_link():
    """#527 B4 left one top bar; avo, 10 Nov 2026 moved it with every control to Fetch/Health."""
    from RevenueChaser.module_settings.tests.test_control_view import control_nd
    html = report.render_settings_dashboard(dict(_payload(), newdata=control_nd(),
                                                 ibkr_login_url="https://gateway.example/"))
    part = _panel(html, "panel-health")
    assert part.count('class="topbar"') == 1 and 'class="topbar"' not in _panel(html, "panel-data")
    assert 'href="https://gateway.example/"' in part.split('class="topbar"', 1)[1].split("</div>", 1)[0]
    assert html.count('class="topbar"') == 1 and html.count('id="sw-msg"') == 1
    assert "btn.closest('.topbar')" in control_render.CTL_JS


def test_the_health_tab_puts_what_needs_avo_first_and_folds_the_reference_parts():
    from RevenueChaser.module_settings.tests.test_control_view import control_nd
    html = report.render_settings_dashboard(dict(_payload(), newdata=control_nd(),
                                                 ibkr_login_url="https://gateway.example/"))
    health = _panel(html, "panel-health")
    order = ["<h1>Data/Health · Fetch/Health</h1>", "<h1>Needs, boards and automations</h1>",
             'id="health-live-link"',
             'id="needs-catalog"', 'id="ticker-drill"', 'id="needs-log"', 'id="needs-routes"']
    at = [health.index(x) for x in order]
    assert at == sorted(at), order
    assert 'id="ibkr-login-card"' not in health, "the login link is in the top bar now"
    for part in ("needs-log", "needs-routes"):
        assert re.search(rf'<details class="hfold"><summary>[^<]*</summary>\s*<section class="part" id="{part}"',
                         health), part


def test_both_tabs_share_one_centred_column():
    """#527 step 10: one width (``--page-max``), centred, on both tabs and the help box above them."""
    css = control_render.CTL_CSS.split("#527 step 10", 1)[-1]
    assert ".ctl .wrap{max-width:var(--page-max,1280px);margin-inline:auto}" in css
    assert "#needs .wrap{max-width:var(--page-max,1280px);margin-inline:auto}" in needs_tab.NEEDS_CSS
    html = report.render_settings_dashboard(_payload())
    assert 'class="wrap helpwrap" style="max-width:var(--page-max,1280px);margin:0 auto;' in html
