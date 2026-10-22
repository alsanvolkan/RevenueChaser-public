"""Every dashboard tab carries the shared "What am I looking at?" box (#383 C3, docs/dashboard_ui.md §2b).

``reporting_common.help_html`` is the one producer: a board that writes its own ``<details class="help">`` (the old
forks on Implied Probability, Relatedness and Value Chain) fails, and so does a tab without the box. Not a tab here:
the Logs page (not a data board), and the two sub-pages in the fixture registry.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.tests.board_fixtures import BOARDS

# What help_html writes, byte for byte (a fork spells it with other whitespace or another class).
SHARED = '<details class="help"><summary>What am I looking at?</summary>\n<div class="body">'
NOT_A_DATA_TAB = {"logs"}
SUB_PAGES = {"module_earnings_radar_detail", "module_sector_rotation_trail"}
HELP_TABS = sorted(set(BOARDS) - NOT_A_DATA_TAB - SUB_PAGES)


def test_the_help_tabs_are_every_data_tab():
    from RevenueChaser.app import dashboard
    tabs = {t["key"] for t in dashboard.TABS if t["type"] == "tab"}
    assert tabs - NOT_A_DATA_TAB <= set(HELP_TABS)
    assert len(HELP_TABS) == 16


@pytest.mark.parametrize("key", HELP_TABS)
def test_the_tab_has_the_one_shared_help_box(key, tmp_path, monkeypatch, rawstore_tmp_db):
    html = BOARDS[key].render(tmp_path, monkeypatch)
    assert html.count(SHARED) == 1, "help_html missing (or rendered twice)"
    assert html.count('class="help"') == 1, "a board-local help box (a fork) is still on the page"
    body = html.split(SHARED, 1)[1].split("</div></details>", 1)[0]
    assert "Fetch" in body and "Refresh" in body, "the help says what Fetch and Refresh do on this tab"
