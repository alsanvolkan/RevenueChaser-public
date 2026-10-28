"""The LEAPS pin's watchlist toggle writes one ticker and leaves a change row per click (#306, D36).

Since PR 4 the route toggles the stock watch list in ops.sqlite; its history is ``watchlist_stock_changes``.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.rcdata import ops


@pytest.fixture
def client():
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


def _actions(ticker: str) -> list[str]:
    with ops.connect() as con:
        return [r[0] for r in con.execute("SELECT action FROM watchlist_stock_changes WHERE ticker = ? ORDER BY id",
                                          (ticker,))]


def test_off_then_on_in_one_session_keeps_both_rows(client):
    answers = [client.post("/api/watchlist/toggle/AAA").get_json()["in_watchlist"] for _ in range(3)]
    assert answers == [True, False, True]
    assert rawstore.load_watchlist() == {"AAA"}
    assert _actions("AAA") == ["add", "remove", "add"]


def test_a_toggle_on_a_stale_read_keeps_a_ticker_added_meanwhile(client, monkeypatch):
    """Two clicks race: the other request added BBB after this one read the list. Only AAA may move."""
    ops.watch_add("stock", "BBB", by="leaps")
    monkeypatch.setattr(rawstore, "load_watchlist", lambda: set())
    r = client.post("/api/watchlist/toggle/AAA")
    assert r.get_json() == {"ok": True, "ticker": "AAA", "in_watchlist": True}
    assert ops.watch_list("stock") == ["AAA", "BBB"]
    assert _actions("BBB") == ["add"]


def test_the_route_never_rewrites_the_whole_list(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the toggle rewrote the whole watchlist")
    monkeypatch.setattr(ops, "watch_replace", boom)
    monkeypatch.setattr(rawstore, "save_watchlist", boom)
    assert client.post("/api/watchlist/toggle/AAA").status_code == 200
    assert client.post("/api/watchlist/toggle/AAA").status_code == 200


def test_a_bad_ticker_writes_nothing(client):
    assert client.post("/api/watchlist/toggle/..").status_code in (400, 404)
    assert not ops.ops_path().exists()
