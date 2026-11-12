"""PR 4 / #774: the watch symbol and its dropdown, clicked in a real browser on a real board (SEC 13F), and the
Settings cards after it.

The name is pinned through the old route (the stock group's default list), one 13F Fetch renders its row from the
seeded ownership store, and the page marks the symbol from the live lists. Opening the menu shows both groups;
ticking the Options list puts the name on it with ``sec_13f`` as ``added_by``, unticking the Stocks list takes it
off, and "+ New list" makes a second Stocks list with that name already on it. Settings then lists the name on the
option card, under the names the next options build takes. On a 390 px phone the symbol and every menu row are at
least 44 px tall.

It was pinned on LEAPS until 12 Nov 2026; that board serves no rows (#730), so the same clicks are made on
13F, which renders the same shared symbol.
"""
from __future__ import annotations

import os

import pytest

from .harness.cdp_browser import Page, find_chrome
from .harness.shared import ops_rows
from .test_board_sec_13f import TOTALS

PHONE = (390, 844)
_SYMBOL = 'button[data-rc-watch-open][data-ticker="NVDA"]'
_ROW = '.rc-watch-menu button[data-rc-watch-list="{id}"]'
_NEW = '.rc-watch-menu button[data-rc-watch-new="stock"]'
_NAME = '.rc-watch-menu input[data-rc-watch-name="stock"]'
_CREATE = '.rc-watch-menu button[data-rc-watch-create="stock"]'


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


def _sel(css: str) -> str:
    return "document.querySelector('" + css.replace("'", "\\'") + "')"


def _list_id(dashboard, kind: str, name: str) -> int:
    [row] = [r for r in ops_rows(dashboard.env["RC_MARKET_DIR"], "watch_list")
             if r["kind"] == kind and r["name"] == name]
    return int(row["id"])


def test_the_menu_on_a_board_puts_a_name_on_named_lists_and_settings_shows_it(
        dashboard, browser, serve_13f_totals):
    serve_13f_totals(TOTALS)
    r = dashboard.client.post("/api/watchlist/toggle/NVDA", headers={"X-RC-Panel": "sec_13f"})
    assert r.status_code == 200 and r.json()["in_watchlist"] is True
    stock_main = _list_id(dashboard, "stock", "Main")                 # the default list the old route wrote
    option_main = dashboard.client.post("/api/watchlists", json={"kind": "option", "name": "Main"},
                                        headers={"X-RC-Panel": "cli"}).json()["list"]["id"]
    result = dashboard.run_job("/run/module_sec_13f")
    assert result.ok, f"13F fetch failed: {result}\n{dashboard.logs()}"

    browser.emulate(*PHONE, scheme="dark")
    browser.open(f"{dashboard.url}/view/module_sec_13f", ready=f"!!{_sel(_SYMBOL)}")
    browser.wait_for(f"{_sel(_SYMBOL)}.getAttribute('data-rc-watch-on') === '1'")   # the live read marked it
    w, h = browser.eval(f"(function(b){{ var r = b.getBoundingClientRect(); return [r.width, r.height]; }})"
                        f"({_sel(_SYMBOL)})")
    assert w >= 44 and h >= 44, (w, h)

    browser.eval(f"{_sel(_SYMBOL)}.click()")
    browser.wait_for(f"!!{_sel(_ROW.format(id=option_main))}")
    assert browser.eval(f"{_sel(_ROW.format(id=stock_main))}.getAttribute('aria-checked')") == "true"
    assert browser.eval("document.querySelectorAll('.rc-watch-menu h4').length") == 2
    for sel in (_ROW.format(id=option_main), _ROW.format(id=stock_main)):
        rh = browser.eval(f"{_sel(sel)}.getBoundingClientRect().height")
        assert rh >= 44, (sel, rh)

    browser.eval(f"{_sel(_ROW.format(id=option_main))}.click()")
    browser.wait_for(f"{_sel(_ROW.format(id=option_main))}.getAttribute('aria-checked') === 'true'")
    browser.eval(f"{_sel(_ROW.format(id=stock_main))}.click()")
    browser.wait_for(f"{_sel(_ROW.format(id=stock_main))}.getAttribute('aria-checked') === 'false'")

    # "+ New list" in the Stocks group: the name is typed, and NVDA goes onto the new list at once.
    browser.eval(f"{_sel(_NEW)}.click()")
    browser.wait_for(f"!!{_sel(_NAME)}")
    browser.eval(f"{_sel(_NAME)}.value = 'Biotech'")
    browser.eval(f"{_sel(_CREATE)}.click()")
    browser.wait_for("document.querySelectorAll('.rc-watch-menu button[data-rc-watch-list]').length === 3")
    bio = _list_id(dashboard, "stock", "Biotech")
    browser.wait_for(f"{_sel(_ROW.format(id=bio))}.getAttribute('aria-checked') === 'true'")

    got = dashboard.client.get("/api/watchlists").json()
    assert got["option"] == ["NVDA"] and got["stock"] == ["NVDA"]      # the union of the group's lists
    assert [(l["name"], l["tickers"]) for l in got["groups"]["stock"]] == [("Main", []), ("Biotech", ["NVDA"])]
    assert _history(dashboard, "option") == [("NVDA", "add", "sec_13f")]
    assert _history(dashboard, "stock") == [("NVDA", "add", "sec_13f"), ("NVDA", "remove", "sec_13f"),
                                            ("NVDA", "add", "sec_13f")]

    html = dashboard.view("module_settings", refresh=True).text
    card = html[html.index('id="nd-watch-option"'):]
    card = card[:card.index("data-nd-msg></div></div>")]
    assert "<b>NVDA</b>" in card and "by sec_13f" in card and "SPX and 1 name from this list: NVDA" in card
