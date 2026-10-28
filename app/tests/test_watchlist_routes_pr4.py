"""The two watch-list routes (PR 4, D2): toggle per list, the old route on the stock list, the read for the buttons.

Every write goes through ``ops.watch_toggle``; a refused request (no panel header, an unknown list, a ticker
``_valid_ticker`` refuses) writes nothing and does not even create ``ops.sqlite``.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.rcdata import ops

# Each is refused by _valid_ticker: markup, a quote that would end an attribute, a path, a space, too long, empty-ish.
HOSTILE = ["<script>", "A\"B", "A'B", "..", "A B", "ABCDEFGHIJK", "-AAA", "A%3CB", "A&B", "A;B"]


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


def _history(kind: str) -> list[tuple]:
    with ops.connect() as con:
        return [tuple(r) for r in con.execute(f"SELECT ticker, action, by FROM watchlist_{kind}_changes ORDER BY id")]


@pytest.mark.parametrize("kind", ["option", "stock"])
def test_toggle_puts_a_ticker_on_and_off_one_list_with_the_board_as_by(client, kind):
    other = "stock" if kind == "option" else "option"
    r = client.post(f"/api/watchlist/{kind}/toggle/nvda", headers={"X-RC-Panel": "sec_13f"})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "list": kind, "ticker": "NVDA", "on": True}
    assert ops.watch_rows(kind)[0]["added_by"] == "sec_13f" and ops.watch_list(other) == []
    assert client.post(f"/api/watchlist/{kind}/toggle/NVDA").get_json()["on"] is False
    assert _history(kind) == [("NVDA", "add", "sec_13f"), ("NVDA", "remove", "test")]


def test_an_unknown_list_is_404_and_writes_nothing(client):
    r = client.post("/api/watchlist/bond/toggle/NVDA")
    assert r.status_code == 404 and r.get_json()["ok"] is False
    assert not ops.ops_path().exists()


@pytest.mark.parametrize("ticker", HOSTILE)
@pytest.mark.parametrize("route", ["/api/watchlist/option/toggle/", "/api/watchlist/stock/toggle/",
                                   "/api/watchlist/toggle/", "/api/watchlist/option/remove/"])
def test_a_ticker_valid_ticker_refuses_is_400_and_writes_nothing(client, route, ticker):
    from urllib.parse import quote
    r = client.post(route + quote(ticker, safe=""))
    assert r.status_code in (400, 404), r.status_code       # ".." may be normalised away by the router: 404
    assert not ops.ops_path().exists()


@pytest.mark.parametrize("ticker", ["<script>", "A\"B", "ABCDEFGHIJK"])
def test_the_400_comes_from_valid_ticker(client, ticker):
    """Not a router 404: these reach the view, and ``_valid_ticker`` is what refuses them."""
    from urllib.parse import quote
    r = client.post("/api/watchlist/option/toggle/" + quote(ticker, safe=""))
    assert r.status_code == 400 and r.get_json() == {"ok": False, "error": "bad ticker"}


def test_without_the_panel_header_nothing_is_written():
    raw = dashboard.app.test_client()
    for url in ("/api/watchlist/option/toggle/NVDA", "/api/watchlist/stock/toggle/NVDA", "/api/watchlist/toggle/NVDA"):
        r = raw.post(url)
        assert r.status_code == 403, url
    assert not ops.ops_path().exists()


def test_the_old_route_toggles_the_stock_list_and_answers_in_watchlist(client):
    r = client.post("/api/watchlist/toggle/aaa", headers={"X-RC-Panel": "leaps"})
    assert r.get_json() == {"ok": True, "ticker": "AAA", "in_watchlist": True}
    assert ops.watch_list("stock") == ["AAA"] and ops.watch_list("option") == []
    assert client.post("/api/watchlist/toggle/AAA", headers={"X-RC-Panel": "leaps"}).get_json()["in_watchlist"] is False
    assert _history("stock") == [("AAA", "add", "leaps"), ("AAA", "remove", "leaps")]


def test_rawstore_watchlist_accessors_are_the_stock_list():
    assert rawstore.toggle_watchlist("aaa", by="leaps") is True
    assert rawstore.load_watchlist() == {"AAA"} == set(ops.watch_list("stock"))
    rawstore.save_watchlist({"BBB", "CCC"})
    assert rawstore.load_watchlist() == {"BBB", "CCC"}
    assert _history("stock")[1:] == [("AAA", "remove", "save_watchlist"), ("BBB", "add", "save_watchlist"),
                                     ("CCC", "add", "save_watchlist")]
    rawstore.save_watchlist(set())
    assert rawstore.load_watchlist() == set() and ops.watch_list("option") == []


def test_the_lists_read_is_both_tickers_lists_and_never_creates_the_file(client):
    assert client.get("/api/watchlists").get_json() == {"ok": True, "option": [], "stock": []}
    assert not ops.ops_path().exists()
    client.post("/api/watchlist/option/toggle/NVDA")
    client.post("/api/watchlist/stock/toggle/MSFT")
    client.post("/api/watchlist/stock/toggle/AAPL")
    assert client.get("/api/watchlists").get_json() == {"ok": True, "option": ["NVDA"], "stock": ["AAPL", "MSFT"]}


def test_remove_takes_a_name_off_and_never_adds_it(client):
    client.post("/api/watchlist/option/toggle/NVDA", headers={"X-RC-Panel": "leaps"})
    r = client.post("/api/watchlist/option/remove/nvda", headers={"X-RC-Panel": "settings"})
    assert r.get_json() == {"ok": True, "list": "option", "ticker": "NVDA", "removed": True}
    again = client.post("/api/watchlist/option/remove/NVDA", headers={"X-RC-Panel": "settings"})
    assert again.get_json()["removed"] is False and ops.watch_list("option") == []
    assert _history("option") == [("NVDA", "add", "leaps"), ("NVDA", "remove", "settings")]


def test_remove_refuses_an_unknown_list_a_bad_ticker_and_a_missing_header(client):
    assert client.post("/api/watchlist/bond/remove/NVDA").status_code == 404
    assert client.post("/api/watchlist/stock/remove/%3Cb%3E").status_code == 400
    assert dashboard.app.test_client().post("/api/watchlist/stock/remove/NVDA").status_code == 403
    assert not ops.ops_path().exists()
