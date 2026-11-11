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


def test_the_board_fixture_carries_the_section():
    assert "newdata_fixture()" in __import__("inspect").getsource(BOARDS["module_settings"].render)


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


def test_the_phone_rules_hold_in_the_css():
    css = report.ND_CSS
    assert "nowrap" not in css and "overflow-x" not in css
    assert "min-height:44px" in css and "min-width:44px" in css
    assert ".nd-bar{width:100%" in css


def test_the_client_data_holds_only_the_backfill_estimate_inputs():
    client = report._nd_client(newdata_fixture())
    assert client["datasets"]["market_series"] == {"missing": ["2026-10-20"], "calls_per_partition": 1}
    assert client["confirm_over"] == 1000


def test_the_identity_card_says_the_boards_read_the_spine_and_why_it_gives_no_names():
    """#642 PR 6: the spine is the only universe; a reason (no file, a failed build) is shown, never an old store."""
    ok = report._nd_identity({"universe_source": {"requested": "spine", "effective": "spine", "reason": None}})
    assert "Boards read" in ok and "the spine" in ok and "no names yet" not in ok and "old store" not in ok
    bad = report._nd_identity({"universe_source": {"reason": "no spine.duckdb yet"}})
    assert 'class="nd-warn">the spine, no names yet: no spine.duckdb yet' in bad
