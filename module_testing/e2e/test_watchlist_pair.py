"""The "add to list" menu (#786 step 5; the Stock / Option pair of PR 4 until then), used in a real browser on a real
board (SEC 13F), and the Watch lists board after it.

The name is pinned through the old route (the stock list now), one 13F Fetch renders its row from the seeded
ownership store, and the page counts the lists that hold it. The bookmark opens the menu; ticking **Option chains**
puts it on the option list with ``sec_13f`` as ``added_by``, unticking **Stock picks** unpins it. The Watch lists
board then shows it on the option list the jobs read. On a 390 px phone the button and each menu row are at least
44 px tall and the page does not scroll sideways.

It was pinned on LEAPS until 12 Nov 2026; that board serves no rows (#730), so the same clicks are made on
13F, which renders the same shared pair.
"""
from __future__ import annotations

import os

import pytest

from .harness.cdp_browser import Page, find_chrome
from .harness.shared import ops_rows
from .test_board_sec_13f import TOTALS

PHONE = (390, 844)
_BTN = 'button[data-rc-watch-menu][data-ticker="NVDA"]'
_ITEM = '#rc-watch-menu [data-rc-wm-list="{lst}"]'


@pytest.fixture(scope="module")
def chrome():
    path = find_chrome()
    if path is None:
        msg = "no Chrome/Chromium for the watch-list click: set RC_CHROME or install chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg + " (a skip on CI would leave the check unenforced)")
        pytest.skip(msg)
    return path


@pytest.fixture
def browser(chrome):
    with Page(chrome) as page:
        yield page


def _history(dashboard, kind: str) -> list[tuple]:
    return [(r["ticker"], r["action"], r["by"]) for r in ops_rows(dashboard.env["RC_MARKET_DIR"],
                                                                    f"watchlist_{kind}_changes")]


def _q(selector: str) -> str:
    return "document.querySelector('" + selector.replace("'", "\\'") + "')"


def test_the_menu_on_a_board_puts_a_name_on_each_list_and_the_watch_lists_board_shows_it(
        dashboard, browser, serve_13f_totals):
    serve_13f_totals(TOTALS)
    r = dashboard.client.post("/api/watchlist/toggle/NVDA", headers={"X-RC-Panel": "sec_13f"})
    assert r.status_code == 200 and r.json()["in_watchlist"] is True
    result = dashboard.run_job("/run/module_sec_13f")
    assert result.ok, f"13F fetch failed: {result}\n{dashboard.logs()}"

    browser.emulate(*PHONE, scheme="dark")
    browser.open(f"{dashboard.url}/view/module_sec_13f", ready=f"!!{_q(_BTN)}")
    browser.wait_for(f"{_q(_BTN)}.getAttribute('data-on') === '1'")     # the live read counted the stock list
    w, h = browser.eval(f"(function(b){{ var r = b.getBoundingClientRect(); return [r.width, r.height]; }})"
                        f"({_q(_BTN)})")
    assert w >= 44 and h >= 44, (w, h)

    browser.eval(f"{_q(_BTN)}.click()")
    browser.wait_for(f"!!{_q(_ITEM.format(lst='jobs.option'))}")
    assert browser.eval(f"{_q(_ITEM.format(lst='jobs.stock'))}.getAttribute('aria-checked')") == "true"
    rh = browser.eval(f"{_q(_ITEM.format(lst='jobs.option'))}.getBoundingClientRect().height")
    assert rh >= 44, rh
    assert browser.eval("document.documentElement.scrollWidth") <= PHONE[0]

    browser.eval(f"{_q(_ITEM.format(lst='jobs.option'))}.click()")
    browser.wait_for(f"{_q(_ITEM.format(lst='jobs.option'))}.getAttribute('aria-checked') === 'true'")
    browser.eval(f"{_q(_ITEM.format(lst='jobs.stock'))}.click()")
    browser.wait_for(f"{_q(_ITEM.format(lst='jobs.stock'))}.getAttribute('aria-checked') === 'false'")
    browser.wait_for(f"{_q(_BTN)}.getAttribute('data-on') === '1'")

    got = dashboard.client.get("/api/watchlists").json()
    assert (got["option"], got["stock"]) == (["NVDA"], [])
    assert _history(dashboard, "option") == [("NVDA", "add", "sec_13f")]
    assert _history(dashboard, "stock") == [("NVDA", "add", "sec_13f"), ("NVDA", "remove", "sec_13f")]

    # The list the menu wrote is the one the Watch lists board shows under "Read by the jobs" (13 Nov 2026; it was
    # a card on Settings until then).
    board = dashboard.client.get("/view/module_watchlists?list=jobs.option").text
    data = board[board.index("var WL_DATA = "):]
    assert '"id": "jobs.option"' in data or '"id":"jobs.option"' in data
    assert '"sym": "NVDA"' in data or '"sym":"NVDA"' in data
    assert 'id="nd-watch-option"' not in dashboard.view("module_settings", refresh=True).text
