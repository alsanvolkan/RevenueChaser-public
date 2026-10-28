"""PR 4: the watch-list pair, clicked in a real browser on a real board (LEAPS), and the Settings cards after it.

The name is pinned through the old route (the stock list now), one LEAPS Fetch against the mock provider renders
its row, and the page marks the pair from the live lists. A click on **Opt** puts it on the option list with
``leaps`` as ``added_by``; a click on **Stk** unpins it. Settings then lists it on the option card, under the names
the next options build takes. On a 390 px phone each button is at least 44 x 44.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from .harness.cdp_browser import Page, find_chrome

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
    path = Path(dashboard.env["RC_MARKET_DIR"]) / "ops.sqlite"
    con = sqlite3.connect(str(path))
    try:
        return [tuple(r) for r in con.execute(f"SELECT ticker, action, by FROM watchlist_{kind}_changes ORDER BY id")]
    finally:
        con.close()


def _sel(lst: str) -> str:
    return "document.querySelector('#tier-watchlist " + _PAIR.format(lst=lst).replace("'", "\\'") + "')"


def test_the_pair_on_leaps_puts_a_name_on_each_list_and_settings_shows_it(dashboard, browser):
    r = dashboard.client.post("/api/watchlist/toggle/NVDA", headers={"X-RC-Panel": "leaps"})
    assert r.status_code == 200 and r.json()["in_watchlist"] is True
    result = dashboard.run_job("/run/module_leaps")
    assert result.ok, f"LEAPS fetch failed: {result}\n{dashboard.logs()}"

    browser.emulate(*PHONE, scheme="dark")
    browser.open(f"{dashboard.url}/view/module_leaps", ready=f"!!{_sel('stock')}")
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
    assert _history(dashboard, "option") == [("NVDA", "add", "leaps")]
    assert _history(dashboard, "stock") == [("NVDA", "add", "leaps"), ("NVDA", "remove", "leaps")]

    html = dashboard.view("module_settings", refresh=True).text
    card = html[html.index('id="nd-watch-option"'):]
    card = card[:card.index("data-nd-msg></div></div>")]
    assert "<b>NVDA</b>" in card and "by leaps" in card and "SPX and 1 name from this list: NVDA" in card
