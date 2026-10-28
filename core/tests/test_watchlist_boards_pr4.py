"""Every board that renders tickers mounts the shared watch-list pair (PR 4, D2); the others say why not.

Three checks per board, on the hostile fixtures of ``board_fixtures`` (every ticker there is ``H(...)``):

- the page mounts the pair once, with its own board key as the ``X-RC-Panel`` value (``rcWatchInit("<board>")``);
- a hostile ticker gets no button: every pair the server rendered carries a ``data-ticker`` the dashboard's ticker
  rule accepts (a fixture's good tickers, XLK say, do get theirs), and ``check_page`` holds (one escaper, ADR-X1);
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
    "module_asymmetric_growth": "asymmetric_growth",
    "module_swing_potential": "swing_potential",
    "module_fundamentals": "fundamentals",
    "module_sector_rotation": "sector_rotation",
    "module_earnings_radar": "earnings_radar",
    "module_earnings_radar_detail": "earnings_radar",
    "module_oipd": "oipd",
    "module_financials": "financials",
}
# board key -> why it has no pair (deliberately left in PR 4)
WITHOUT_PAIR = {
    "module_spx_gamma": "SPX only: the index is not a name either list holds (SPX is always in the options build, D2)",
    "module_settings": "the two watch-list cards live here; the dataset rows carry no ticker",
    "logs": "not a data board",
    "runs": "not a data board",
    "module_sector_rotation_trail": "a chart of the same funds' trails; the board's own table carries their pair",
    "module_uw_interaction": "a lab: raw columns of whatever UW tool was run, not a board of names",
    # The board groups that get the pair in the later commits of this PR; each commit moves its boards up.
    **{k: "pending in this PR" for k in (
        "module_comovement", "module_value_chain", "module_relatedness",
        )},
}

_SERVER_BUTTON = re.compile(r'data-rc-watch="(?:option|stock)" data-ticker="([^"]*)"')


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
    from RevenueChaser.core.reporting_common import WATCH_TICKER_PATTERN
    page = BOARDS[key].render(tmp_path, monkeypatch)
    tickers = _SERVER_BUTTON.findall(page)
    assert all(re.fullmatch(WATCH_TICKER_PATTERN, t) for t in tickers), tickers


@pytest.mark.parametrize("key", sorted(WITHOUT_PAIR))
def test_a_board_left_out_does_not_mount_it(key, tmp_path, monkeypatch, rawstore_tmp_db):
    page = BOARDS[key].render(tmp_path, monkeypatch)
    assert "rcWatchInit(" not in page


# A board that renders its tickers on the server: a good ticker of its fixture gets its pair.
SERVER_GOOD = {"module_sector_rotation": "XLK"}
# A board whose rows the browser builds: the call where the ticker is rendered (its own tests run it where they can).
CLIENT_CALL = {
    "module_sec_13f": "rcWatchButtons(r[0])",
    "module_asymmetric_growth": "rcWatchRow(c.ticker)",
    "module_swing_potential": "rcWatchRow(c.ticker)",
    "module_fundamentals": "rcWatchRow(c.ticker)",
    "module_earnings_radar": "rcWatchButtons(ticker)",          # in the drawer header, not the table
}


@pytest.mark.parametrize("key", sorted(SERVER_GOOD))
def test_a_good_server_rendered_ticker_gets_its_pair(key, tmp_path, monkeypatch, rawstore_tmp_db):
    page = BOARDS[key].render(tmp_path, monkeypatch)
    assert SERVER_GOOD[key] in _SERVER_BUTTON.findall(page)


@pytest.mark.parametrize("key", sorted(CLIENT_CALL))
def test_a_client_rendered_board_calls_the_pair_where_it_renders_the_ticker(key, tmp_path, monkeypatch,
                                                                             rawstore_tmp_db):
    assert CLIENT_CALL[key] in BOARDS[key].render(tmp_path, monkeypatch)


def test_implied_probability_shows_the_pair_for_its_focal_ticker():
    from RevenueChaser.module_oipd.report import render_oipd_dashboard
    page = render_oipd_dashboard({"ticker": "AAPL", "asof": "2026-09-01", "spot": 100.0, "n_contracts": 10,
                                  "horizon_days": 30, "risk_free": 0.04, "error": None, "warnings": [],
                                  "expiries": []})
    assert _SERVER_BUTTON.findall(page) == ["AAPL", "AAPL"]
    assert 'rcWatchInit("oipd");' in page


def test_financials_shows_the_pair_for_the_company_and_each_peer():
    from RevenueChaser.module_financials.reporting.html_builder import build_report_html
    fin = {"ticker": "TEST", "sector": "Technology", "current_price": 100.0, "currency": "USD", "info": {}}
    comps = {"pe_only": True, "peers": [{"ticker": "PEER", "pe": 10.0}, {"ticker": "<b>x</b>", "pe": 11.0}],
             "multiples": {"pe": {"p25": 9.0, "p50": 10.0, "p75": 11.0}},
             "implied": {"equity_pe": 1.0e9, "per_share_pe": 50.0}}
    page = build_report_html({"ticker": "TEST", "financials": fin, "modules": {"comps": comps}, "warnings": []})
    tickers = _SERVER_BUTTON.findall(page)
    assert "TEST" in tickers and "PEER" in tickers and len(tickers) == 4    # the hostile peer gets none
    assert "&lt;b&gt;x&lt;/b&gt;" in page and 'rcWatchInit("financials");' in page
