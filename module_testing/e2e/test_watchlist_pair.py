"""PR 4: the watch-list pair, clicked in a real browser on a real board (SEC 13F), and the Settings cards after it.

The name is pinned through the old route (the stock list now), one 13F Fetch renders its row from the seeded
ownership store, and the page marks the pair from the live lists. A click on **Opt** puts it on the option list
with ``sec_13f`` as ``added_by``; a click on **Stk** unpins it. Settings then lists it on the option card, under
the names the next options build takes. On a 390 px phone each button is at least 44 x 44.

It was pinned on LEAPS until 12 Nov 2026; that board is switched off (#730), so the same clicks are made on
13F, which renders the same shared pair.
"""
from __future__ import annotations

import os

import pytest

from .harness.cdp_browser import Page, find_chrome
from .harness.shared import ops_rows
from .test_board_sec_13f import TOTALS

PHONE = (390, 844)
_PAIR = 'button[data-rc-watch="{lst}"][data-ticker="NVDA"]'


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


def _sel(lst: str) -> str:
    return "document.querySelector('" + _PAIR.format(lst=lst).replace("'", "\\'") + "')"


def test_the_pair_on_a_board_puts_a_name_on_each_list_and_settings_shows_it(
        dashboard, browser, serve_13f_totals):
    serve_13f_totals(TOTALS)
    r = dashboard.client.post("/api/watchlist/toggle/NVDA", headers={"X-RC-Panel": "sec_13f"})
    assert r.status_code == 200 and r.json()["in_watchlist"] is True
    result = dashboard.run_job("/run/module_sec_13f")
    assert result.ok, f"13F fetch failed: {result}\n{dashboard.logs()}"

    browser.emulate(*PHONE, scheme="dark")
    browser.open(f"{dashboard.url}/view/module_sec_13f", ready=f"!!{_sel('stock')}")
    browser.wait_for(f"{_sel('stock')}.getAttribute('aria-pressed') === 'true'")    # the live read marked it
    assert browser.eval(f"{_sel('option')}.getAttribute('aria-pressed')") == "false"
    for lst in ("option", "stock"):
        w, h = browser.eval(f"(function(b){{ var r = b.getBoundingClientRect(); return [r.width, r.height]; }})"
                            f"({_sel(lst)})")
        assert w >= 44 and h >= 44, (lst, w, h)

    browser.eval(f"{_sel('option')}.click()")
    browser.wait_for(f"{_sel('option')}.getAttribute('aria-pressed') === 'true'")
    browser.eval(f"{_sel('stock')}.click()")
    browser.wait_for(f"{_sel('stock')}.getAttribute('aria-pressed') === 'false'")

    assert dashboard.client.get("/api/watchlists").json() == {"ok": True, "option": ["NVDA"], "stock": []}
    assert _history(dashboard, "option") == [("NVDA", "add", "sec_13f")]
    assert _history(dashboard, "stock") == [("NVDA", "add", "sec_13f"), ("NVDA", "remove", "sec_13f")]

    html = dashboard.view("module_settings", refresh=True).text
    card = html[html.index('id="nd-watch-option"'):]
    card = card[:card.index("data-nd-msg></div></div>")]
    assert "<b>NVDA</b>" in card and "by sec_13f" in card and "SPX and 1 name from this list: NVDA" in card
