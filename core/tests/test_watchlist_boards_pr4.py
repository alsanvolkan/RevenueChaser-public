"""Every board that renders tickers mounts the shared watch-list pair (PR 4, D2); the others say why not.

Three checks per board, on the hostile fixtures of ``board_fixtures`` (every ticker there is ``H(...)``):

- the page mounts the pair once, with its own board key as the ``X-RC-Panel`` value (``rcWatchInit("<board>")``);
- a hostile ticker gets no button: the server never renders ``data-rc-watch="option|stock"`` for it, so the only
  ``data-rc-watch`` in the page is the snippet's own JS (and ``check_page`` holds: one escaper, ADR-X1);
- a board whose rows are built in the browser calls ``rcWatchButtons(`` where it renders the ticker.

The board's own tests show the pair on a good ticker (``module_*/tests``). A new board in ``BOARDS`` must be listed
in one of the two tables below, or ``test_every_board_is_classified`` fails.
"""
from __future__ import annotations

import re

import pytest

from RevenueChaser.core.tests.board_fixtures import BOARDS
from RevenueChaser.core.tests.test_one_escaper_175 import check_page

# board key -> the X-RC-Panel value its pair sends (its ``added_by``)
WITH_PAIR = {
    "module_leaps": "leaps",
    "module_sec_13f": "sec_13f",
}
# board key -> why it has no pair (deliberately left in PR 4)
WITHOUT_PAIR = {
    "module_spx_gamma": "SPX only: the index is not a name either list holds (SPX is always in the options build, D2)",
    "module_settings": "the two watch-list cards live here; the dataset rows carry no ticker",
    "logs": "not a data board",
    "runs": "not a data board",
    # The board groups that get the pair in the later commits of this PR; each commit moves its boards up.
    **{k: "pending in this PR" for k in (
        "module_earnings_radar", "module_earnings_radar_detail", "module_oipd", "module_financials",
        "module_uw_interaction", "module_comovement", "module_value_chain", "module_relatedness",
        "module_sector_rotation", "module_sector_rotation_trail", "module_asymmetric_growth",
        "module_swing_potential", "module_fundamentals")},
}

_SERVER_BUTTON = re.compile(r'data-rc-watch="(?:option|stock)"')


def test_every_board_is_classified():
    assert set(WITH_PAIR) | set(WITHOUT_PAIR) == set(BOARDS), sorted(set(BOARDS) - set(WITH_PAIR) - set(WITHOUT_PAIR))
    assert not set(WITH_PAIR) & set(WITHOUT_PAIR)


@pytest.mark.parametrize("key", sorted(WITH_PAIR))
def test_the_board_mounts_the_pair_once_with_its_own_key(key, tmp_path, monkeypatch, rawstore_tmp_db):
    page = BOARDS[key].render(tmp_path, monkeypatch)
    assert page.count("rcWatchInit(") == 2, "the definition and exactly one mount"
    assert f'rcWatchInit("{WITH_PAIR[key]}");' in page
    assert ".rc-watch button{min-width:44px;min-height:44px" in page
    check_page(page)


@pytest.mark.parametrize("key", sorted(WITH_PAIR))
def test_a_hostile_ticker_gets_no_button(key, tmp_path, monkeypatch, rawstore_tmp_db):
    page = BOARDS[key].render(tmp_path, monkeypatch)
    assert not _SERVER_BUTTON.search(page), _SERVER_BUTTON.search(page)


@pytest.mark.parametrize("key", sorted(WITHOUT_PAIR))
def test_a_board_left_out_does_not_mount_it(key, tmp_path, monkeypatch, rawstore_tmp_db):
    page = BOARDS[key].render(tmp_path, monkeypatch)
    assert "rcWatchInit(" not in page
