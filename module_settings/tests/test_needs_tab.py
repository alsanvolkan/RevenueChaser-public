"""The Settings page's second tab, **Needs** (#331): the catalog, the request log, the routes.

The model is read from a fixture log and fixture summaries through ``build``'s seams; the render is checked for
escaping (every string ``H(field)``), the tab strip and both panels, the phone and theme rules, and the one-need
failure rule (a failed summary is that row's ``unreadable``, never a failed tab)."""
from __future__ import annotations

import re
from datetime import date

import pytest

from RevenueChaser.core.tests.board_fixtures import H, hostile
from RevenueChaser.core.tests.board_js import assert_scripts_parse
from RevenueChaser.core.tests.test_board_html_guard import assert_inert
from RevenueChaser.core.tests.test_one_escaper_175 import check_page
from RevenueChaser.module_settings import needs_tab, report
from RevenueChaser.module_settings.tests.test_control_view import control_nd
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.needs import catalog

TODAY = date(2026, 11, 1)


def _summary(name: str) -> dict:
    return {"need": name, "state": "fresh", "row_count": 1200, "key_count": 40, "status": "changed",
            "freshness": {"as_of_session": date(2026, 10, 30), "lag_sessions": 0, "max_lag": 1, "unit": "sessions"}}


LOG = [
    {"need": "prices.bars", "process": "dashboard", "day": "2026-11-01", "reads": 3},
    {"need": "prices.bars", "process": "dashboard", "day": "2026-10-26", "reads": 5},     # the 7-day window's first day
    {"need": "prices.bars", "process": "dashboard", "day": "2026-10-25", "reads": 2},     # one day before it
    {"need": "prices.bars", "process": "__main__", "day": "2026-10-03", "reads": 7},      # the 30-day window's first day
    {"need": "earnings.next", "process": "dashboard", "day": "2026-10-31", "reads": 20},
    {"need": "old.renamed", "process": "dashboard", "day": "2026-10-30", "reads": 1},     # not in the catalog
]


def _model(**kw):
    kw.setdefault("today", TODAY)
    kw.setdefault("summarize", _summary)
    kw.setdefault("read_log", lambda first: [r for r in LOG if r["day"] >= first])
    return needs_tab.build(**kw)


def _page(needs_model) -> str:
    return report.render_settings_dashboard({"newdata": control_nd(), "needs": needs_model})


# ── The model ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_catalog_has_one_row_per_need_with_its_file_dataset_rule_and_state():
    m = _model()
    assert [r["need"] for r in m["catalog"]] == sorted(catalog.NEEDS)
    bars = next(r for r in m["catalog"] if r["need"] == "prices.bars")
    spec = catalog.NEEDS["prices.bars"]
    assert (bars["kind"], bars["table"], bars["dataset"]) == (spec.kind, spec.table, spec.dataset)
    assert (bars["max_lag"], bars["unit"], bars["ask_param"]) == (spec.max_lag, spec.max_lag_unit, spec.ask_param)
    assert (bars["state"], bars["newest_day"], bars["rows"], bars["keys"]) == ("fresh", "2026-10-30", 1200, 40)
    assert m["counts"]["fresh"] == len(catalog.NEEDS)


def test_readers_split_into_declared_and_planned(monkeypatch):
    from RevenueChaser.rcdata.needs import boards
    monkeypatch.setattr(boards, "all_reads", lambda: {"module_earnings_radar": ("earnings.next", "prices.bars")})
    monkeypatch.setattr(boards, "PLANNED_READS", {"module_swing_potential": ("prices.bars",),
                                                  "module_earnings_radar": ("market.vix",)})
    bars = next(r for r in _model()["catalog"] if r["need"] == "prices.bars")
    assert bars["declared"] == ["earnings_radar"] and bars["planned"] == ["swing_potential"]
    vix = next(r for r in _model()["catalog"] if r["need"] == "market.vix")
    assert vix["declared"] == [] and vix["planned"] == [], "a declared board's plan is not drawn beside its READS"


def test_one_failed_summary_is_that_rows_unreadable_never_a_failed_tab():
    def summarize(name):
        if name == "market.cot":
            raise OSError("file is being swapped")
        return _summary(name)
    m = _model(summarize=summarize)
    cot = next(r for r in m["catalog"] if r["need"] == "market.cot")
    assert cot["state"] == "unreadable" and "OSError: file is being swapped" in cot["error"]
    assert m["counts"]["unreadable"] == 1 and m["counts"]["fresh"] == len(catalog.NEEDS) - 1
    html = needs_tab.render(m)
    assert "file is being swapped" in html and html.count('class="unreadable"') == 1


def test_the_log_sums_today_7_and_30_days_by_process_busiest_first():
    log = _model()["log"]
    assert log["first_day"] == "2026-10-03" and log["today"] == "2026-11-01"
    rows = {r["need"]: r for r in log["rows"]}
    assert [r["need"] for r in log["rows"]] == ["earnings.next", "prices.bars", "old.renamed"]
    bars = rows["prices.bars"]
    assert (bars["today"], bars["d7"], bars["d30"]) == (3, 8, 17)
    assert bars["by_process"] == [{"process": "dashboard", "reads": 10}, {"process": "__main__", "reads": 7}]
    assert rows["old.renamed"]["in_catalog"] is False and rows["prices.bars"]["in_catalog"] is True
    assert log["processes"] == ["__main__", "dashboard"]
    assert "prices.bars" not in log["never_asked"] and "market.vix" in log["never_asked"]
    assert len(log["never_asked"]) == len(catalog.NEEDS) - 2


def test_the_log_is_read_from_30_days_back_and_a_day_after_today_is_not_counted():
    asked = []
    m = _model(read_log=lambda first: asked.append(first) or [
        {"need": "prices.bars", "process": "dashboard", "day": "2026-11-02", "reads": 9}])
    assert asked == ["2026-10-03"]
    assert m["log"]["rows"] == []


def test_an_unreadable_log_is_one_line_and_the_catalog_still_renders():
    def boom(first):
        raise RuntimeError("ops.sqlite is locked")
    m = _model(read_log=boom)
    assert "RuntimeError: ops.sqlite is locked" in m["log"]["error"]
    html = needs_tab.render(m)
    assert "The read log could not be read" in html and 'id="needs-catalog"' in html


def test_the_default_build_reads_the_real_log_and_does_not_count_its_own_summaries(monkeypatch):
    """No seams: ``ops.need_reads`` against a fixture ops file, and ``needs.summary(count=False)``."""
    ops.put_rows("read_log", [{"day": "2026-11-01", "process": "dashboard", "table_name": "need:prices.bars",
                               "reads": 4}])
    bumped: list[str] = []
    monkeypatch.setattr(ops.read_log, "bump", bumped.append)
    m = needs_tab.build(today=TODAY)
    assert [(r["need"], r["today"]) for r in m["log"]["rows"]] == [("prices.bars", 4)]
    assert not [b for b in bumped if b.startswith("need:")], "the tab counted its own reads"
    assert {r["state"] for r in m["catalog"]} <= {"missing", "fresh", "stale", "unreadable"}


def test_the_log_read_never_creates_the_ops_file():
    """The whole ``build()``, the real ``needs.summary`` included: since #336 no read creates ``ops.sqlite``."""
    assert not ops.ops_path().exists()
    needs_tab.build(today=TODAY)
    assert not ops.ops_path().exists()


def test_every_route_example_names_a_need_in_the_catalog():
    for r in needs_tab.ROUTES:
        m = re.match(r"/needs(?:/([^?]+))?", r["example"])
        assert m, r
        if m.group(1):
            assert m.group(1) in catalog.NEEDS, r["example"]
    paths = [r["path"] for r in needs_tab.ROUTES]
    assert paths == ["/needs", "/needs/<need>", "/needs/<need>?summary=1"]


def test_the_short_interest_example_uses_the_needs_own_ask_parameter():
    spec = catalog.NEEDS["ownership.short_interest"]
    assert f"?{spec.ask_param}=" in needs_tab.ROUTES[1]["example"]


# ── The render ─────────────────────────────────────────────────────────────────────────────────────────────────

def _hostile_model() -> dict:
    m = _model(summarize=lambda n: {**_summary(n), "status": H("status")})
    m["catalog"][0]["error"] = H("error")
    m["catalog"][0]["state"] = "unreadable"
    hm = hostile(m)
    # ``hostile`` leaves dates, states and routes alone (the fixture's skip list); this tab shows them, so they are
    # made hostile here by hand: the need's name, its table, its dataset, its readers, the process, the routes.
    for r in hm["catalog"]:
        r.update(newest_day=H("newest_day"), state=H("state") if r["state"] != "unreadable" else r["state"],
                 kind=H("kind"), unit=H("unit"))
    for r in hm["log"]["rows"]:
        r["by_process"] = [{"process": H("process"), "reads": 3}]
    hm["log"].update(first_day=H("first_day"), today=H("today"))
    hm["routes"] = [{k: H("route_" + k) for k in r} for r in hm["routes"]]
    hm["readers_error"] = H("readers_error")
    return hm


def test_render_escapes_every_string_of_the_tab():
    hm = _hostile_model()
    html = needs_tab.render(hm)
    assert_inert(html)
    for tag in ("need", "table", "dataset", "declared", "planned", "error", "newest_day", "process", "route_example",
                "route_what", "route_path", "readers_error", "ask_param", "kind"):
        assert f"&lt;img src=x onerror=__x(&#x27;{tag}T&#x27;)" in html, tag
    m = _model()
    m["log"]["rows"] = []
    m["log"]["first_day"], m["log"]["today"] = H("first_day"), H("today")
    assert_inert(needs_tab.render(m))
    assert_inert(needs_tab.render({"error": H("tab_error"), "read_at": H("read_at")}))


def test_the_page_has_the_tab_strip_and_both_panels_and_stays_inert():
    html = _page(_hostile_model())
    assert_inert(html)
    check_page(html)
    strip = html[html.index('id="settings-tabs"'):]
    assert strip.index('role="tablist"') < strip.index('id="tab-data"') < strip.index('id="tab-health"')
    assert 'id="tab-fetch"' not in html and 'id="tab-needs"' not in html and 'id="panel-fetch"' not in html
    assert re.search(r'<div[^>]*id="panel-data"[^>]*role="tabpanel"', html)
    assert re.search(r'<div[^>]*id="panel-health"[^>]*role="tabpanel"[^>]*hidden', html), "Health starts hidden"
    data = html[html.index('id="panel-data"'):html.index('id="panel-health"')]
    assert 'id="sec-health"' in data and 'id="old-settings"' not in html and "oldfold" not in html
    health = html[html.index('id="panel-health"'):]
    for part in ("needs-catalog", "needs-log", "needs-routes"):
        assert f'id="{part}"' in health, part


def test_the_pages_scripts_parse_with_the_tab_switch(tmp_path):
    assert_scripts_parse(_page(_hostile_model()), tmp_path)


def test_the_tab_switch_is_remembered_per_viewer_and_reads_the_hash():
    html = _page(_model())
    assert "localStorage" in html and "try{" in html.replace(" ", ""), "storage wrapped in try"
    assert "aria-selected" in html and "ArrowRight" in html


def test_the_page_still_renders_when_the_tab_cannot_be_built(monkeypatch):
    monkeypatch.setattr(needs_tab, "build", lambda **kw: (_ for _ in ()).throw(RuntimeError("catalog import")))
    html = report.render_settings_dashboard({"newdata": control_nd()})
    assert "The Needs tab could not be read" in html and "RuntimeError: catalog import" in html
    assert 'id="sec-health"' in html


# ── Phone and theme ────────────────────────────────────────────────────────────────────────────────────────────

def _tokens(css: str, selector_start: str) -> dict[str, str]:
    block = css[css.index(selector_start):]
    block = block[block.index("{") + 1:block.index("}")]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))


def test_the_tokens_are_the_control_boards_in_light_and_dark():
    from RevenueChaser.module_settings import control_render
    ctl, nds = control_render.CTL_CSS, needs_tab.NEEDS_CSS
    light_c, light_n = _tokens(ctl, ".ctl{"), _tokens(nds, "#needs,#settings-tabs{")
    dark_c = _tokens(ctl, ':root[data-theme="dark"] .ctl{')
    dark_n = _tokens(nds, ':root[data-theme="dark"] #needs')
    auto_n = _tokens(nds, ':root:not([data-theme="light"]) #needs')
    assert light_n and dark_n
    for k, v in light_n.items():
        assert light_c[k].strip() == v.strip(), k
    for k, v in dark_n.items():
        assert dark_c[k].strip() == v.strip(), k
    assert auto_n == dark_n, "the auto-dark block and the forced-dark block say the same"
    assert "@media (prefers-color-scheme: dark)" in nds


def test_phone_rules_44px_tabs_and_tables_stack_under_600px():
    css = needs_tab.NEEDS_CSS
    assert re.search(r'#settings-tabs \[role="tab"\]\{[^}]*min-height:44px', css)
    stack = css[css.index("@media(max-width:600px){\n"):]
    assert "table.stk thead{display:none}" in stack and "content:attr(data-label)" in stack
    html = needs_tab.render(_model())
    assert html.count('<table class="stk">') == 2
    for td in re.findall(r"<td[^>]*>", html):
        assert "data-label=" in td or td == '<td colspan="7">', td    # a group head: its button is its label
    assert 'class="part"' in html


def test_no_style_or_script_escapes_the_tab_scope():
    """Every rule in needs.css is under #needs, #settings-tabs or a role=tabpanel, so the Data tab keeps its own."""
    css = re.sub(r"/\*.*?\*/", "", needs_tab.NEEDS_CSS, flags=re.S)
    for sel in re.findall(r"(?:^|})\s*([^{}@]+)\{", css):
        sel = sel.strip()
        if not sel or sel.startswith("--") or ":" in sel.split(",")[0][:2]:
            continue
        for part in sel.split(","):
            part = part.strip()
            assert ("#needs" in part or "#settings-tabs" in part or part.startswith('[role="tabpanel"]')), part


def test_the_board_fixture_carries_the_hostile_payload_into_the_needs_tab(tmp_path, monkeypatch):
    """The board HTML guard renders Settings from ``board_fixtures``; its payload must reach this tab's strings."""
    from RevenueChaser.core.tests.board_fixtures import BOARDS
    html = BOARDS["module_settings"].render(tmp_path, monkeypatch)
    assert_inert(html)
    needs = html[html.index('<div id="panel-health" role="tabpanel"'):]
    for tag in ("need", "table", "dataset", "declared", "planned", "error", "process", "ask_param",
                "route_path", "route_what", "route_example", "readers_error"):
        assert f"&lt;img src=x onerror=__x(&#x27;{tag}T&#x27;)" in needs, tag


def test_the_lag_names_its_unit_and_a_need_breaks_only_after_a_dot():
    m = _model(summarize=lambda n: {**_summary(n), "state": "stale",
                                    "freshness": {"as_of_session": date(2026, 10, 20), "lag_sessions": 6}})
    html = needs_tab.render(m)
    assert "6 sessions behind" in html and "6 days behind" in html     # prices.* in sessions, estimates in days
    assert "fundamentals.<wbr>companies" in html
    assert re.search(r'<td class="d" data-label="Newest day">2026-10-20</td>', html)
    assert re.search(r"#needs td\.d\{white-space:nowrap\}", needs_tab.NEEDS_CSS)


def test_the_legend_names_the_builder_and_says_old_rows_may_say___main__():
    html = needs_tab.render(_model())
    assert "<code>builder</code>" in html and "<code>__main__</code>" in html and "before 1 Nov 2026" in html


# ── #492 (i): the catalog grouped by served file ───────────────────────────────────────────────────────────────

def _groups(html: str) -> list[tuple[str, str]]:
    """``(file, body)`` per group, in page order: each head's ``data-file`` and its own ``tbody.cbody``."""
    cat = html[html.index('id="needs-catalog"'):html.index('id="needs-log"')]
    return re.findall(r'<tbody class="cghead" data-file="([^"]+)">.*?</tbody>\s*(<tbody class="cbody"[^>]*>.*?</tbody>)',
                      cat, flags=re.S)


def test_the_catalog_draws_one_group_per_served_file_in_order_closed_at_load():
    m = _model()
    groups = _groups(needs_tab.render(m))
    files = [f for f, _ in groups]
    present = {r["kind"] for r in m["catalog"]}
    assert files == [f for f in needs_tab.SERVED_FILES if f in present]
    for _, body in groups:
        assert body.startswith('<tbody class="cbody" hidden')
    html = needs_tab.render(m)
    assert html.count('class="cgtog" aria-expanded="false"') == len(groups)
    assert 'aria-expanded="true"' not in html[html.index('id="needs-catalog"'):html.index('id="needs-log"')]


def test_every_need_is_in_exactly_one_group_and_an_unknown_file_goes_last_as_other():
    m = _model()
    m["catalog"][0]["kind"] = "somewhere_else"
    groups = _groups(needs_tab.render(m))
    assert groups[-1][0] == "other"
    seen = [n for _, body in groups for n in re.findall(r'data-need="([^"]+)"', body)]
    assert sorted(seen) == sorted(r["need"] for r in m["catalog"]) and len(seen) == len(set(seen))
    assert m["catalog"][0]["need"] in re.findall(r'data-need="([^"]+)"', groups[-1][1])


def test_a_group_head_says_its_file_and_counts():
    m = _model(summarize=lambda n: {**_summary(n), "state": "stale" if n == "prices.bars" else "fresh"})
    cat = needs_tab.render(m)
    n = sum(1 for r in m["catalog"] if r["kind"] == "prices")
    head = re.search(r'<tbody class="cghead" data-file="prices" data-bad>(.*?)</tbody>', cat, flags=re.S).group(1)
    assert "▾" in head and "prices" in head, "#527: a group with a stale need is open at load"
    assert f"{n} needs · fresh {n - 1} · stale 1" in head
    assert 'colspan="7"' in head


def test_the_group_toggle_script_flips_the_body_and_the_arrow():
    js = needs_tab.NEEDS_JS
    assert ".cgtog" in js and "aria-expanded" in js and "▾" in js and "▸" in js
    html = _page(_model())
    assert js in html


def test_the_group_head_is_a_section_with_a_touch_target():
    css = needs_tab.NEEDS_CSS
    assert re.search(r"#needs \.cgtog\{[^}]*min-height:44px", css)
    assert re.search(r"#needs tbody\.cghead td\{[^}]*border-top", css)
    assert re.search(r"#needs \.cgtog\{[^}]*font-weight:700", css)
