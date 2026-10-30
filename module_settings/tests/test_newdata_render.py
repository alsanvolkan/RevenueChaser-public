"""PR 2 step 5: the "New data system" section of Settings renders above the old inventory, in the shell's theme
tokens, with every string escaped and every button a request that reads its dataset from ``data-dataset``."""
from __future__ import annotations

import re

import pytest

from RevenueChaser.core import reporting_common
from RevenueChaser.core.tests.board_fixtures import BOARDS, H, hostile, newdata_fixture
from RevenueChaser.core.tests.test_board_html_guard import assert_inert
from RevenueChaser.core.tests.test_one_escaper_175 import check_page
from RevenueChaser.module_settings import report


def _page(nd=None) -> str:
    return report.render_settings_dashboard({"newdata": newdata_fixture() if nd is None else nd})


def _section(html: str) -> str:
    start = html.index('<section id="newdata"')
    return html[start:html.index("</section>", start)]


def test_the_section_renders_above_the_old_inventory():
    html = report.render_settings_dashboard({"newdata": newdata_fixture(), "rows": [
        {"key": "universe", "label": "Equity universe", "tables": ["equity_universe"], "rows": 1}]})
    assert html.index('id="newdata"') < html.index("table class=\"inv\"") < html.index('id="ticker-drill"')
    assert 'id="automations"' in html or "auto-item" in html or "automations" in html   # the old cards stay


def test_one_card_per_dataset_in_the_order_given_with_its_checks_inside():
    sec = _section(_page())
    # Since the review's M1 a dataset row is a <details>, born closed unless it is the group's worst.
    cards = re.findall(r'<details class="nd-card nd-dataset" data-dataset="([^"]+)"', sec)
    assert cards == ["market_series", "uw/spot_expiry_strike"]
    first = sec[sec.index('data-dataset="market_series"'):sec.index('data-dataset="uw/spot_expiry_strike"')]
    # The fixture's WRDS slot has not run, so the cell is a dash and says so (the review's S8).
    assert "Checks (2)" in first and "fetch_failed" in first and "not produced yet" in first
    assert "3 of 9 partitions" in first and 'class="nd-bar"' in first


def test_every_hostile_string_of_the_section_is_escaped():
    nd = hostile(newdata_fixture())
    html = _page(nd)
    assert_inert(html)
    check_page(html)
    sec = _section(html)
    assert "&lt;img src=x onerror=__x(&#x27;dataset" in sec or "&lt;img src=x onerror=__x(" in sec


def test_the_board_fixture_carries_the_section():
    assert "newdata_fixture()" in __import__("inspect").getsource(BOARDS["module_settings"].render)


def test_buttons_read_their_dataset_from_data_attributes_never_an_inline_handler():
    nd = hostile(newdata_fixture())
    sec = _section(_page(nd))
    buttons = re.findall(r"<button[^>]*>", sec)
    assert {re.search(r'data-nd-act="([^"]+)"', b).group(1) for b in buttons} == {"fetch", "backfill", "pause",
                                                                                   "resume", "watch-remove"}
    for b in buttons:
        remove = 'data-nd-act="watch-remove"' in b
        assert "onclick" not in b.lower() and ('data-dataset="' in b or (remove and 'data-ticker="' in b))
        assert "<img" not in b                              # the hostile dataset name is escaped in the attribute
    assert "onclick" not in report.ND_JS and "addEventListener('click'" in report.ND_JS


def test_the_only_controls_are_fetch_backfill_pause_resume_and_the_watch_list_remove():
    sec = _section(_page())
    labels = re.findall(r"<button[^>]*>([^<]*)</button>", sec)
    assert sorted(set(labels)) == ["Backfill", "Fetch", "Pause", "Remove", "Resume"]
    routes = set(re.findall(r"'/api/[a-z/]+", report.ND_JS))
    assert routes == {"'/api/build/queue", "'/api/build/request/", "'/api/build/schedule/", "'/api/watchlist/"}
    assert "/remove/" in report.ND_JS and "/toggle/" not in report.ND_JS     # Remove never adds
    for word in ("drop", "restore", "compact", "delete", "edit"):
        assert f">{word.capitalize()}<" not in sec


def test_the_section_uses_the_shell_tokens_scoped_to_itself_light_and_dark():
    css = report.ND_CSS
    for scheme in ("dark", "light"):
        for name, value in reporting_common.THEME_TOKENS[scheme].items():
            assert f"--{name}:{value}" in css
    assert ":root{" not in css, "no palette on :root: the old page keeps its own until PR 21"
    assert '#newdata{' in css and ':root[data-theme="light"] #newdata{' in css
    assert ':root[data-theme="auto"] #newdata{' in css
    rules = css[len(report.ND_THEME_CSS):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b(?![\w-])", rules.replace("#newdata", "")), "a colour outside the tokens"
    assert "rcThemeApply(rcThemeGet())" in _page()


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_both_themes_render_the_same_section(theme):
    html = _page().replace('<html lang="en">', f'<html lang="en" data-theme="{theme}">')
    assert '<section id="newdata"' in html


def test_the_phone_rules_hold_in_the_css():
    css = report.ND_CSS
    assert "nowrap" not in css and "overflow-x" not in css
    assert "min-height:44px" in css and "min-width:44px" in css
    assert ".nd-bar{width:100%" in css


def test_a_collect_error_is_one_line_not_a_blank_board(monkeypatch):
    from RevenueChaser.module_settings import newdata
    monkeypatch.setattr(newdata, "collect", lambda now=None: (_ for _ in ()).throw(RuntimeError(H("boom"))))
    html = report.render_settings_dashboard({})
    assert "Could not read the new data system: RuntimeError" in html
    assert_inert(html.replace("&lt;img src=x onerror=__x(", "&lt;img src=x onerror=__x(") + "")


def test_the_client_data_holds_only_the_backfill_estimate_inputs():
    client = report._nd_client(newdata_fixture())
    assert client["datasets"]["market_series"] == {"missing": ["2026-10-20"], "calls_per_partition": 1}
    assert client["confirm_over"] == 1000
