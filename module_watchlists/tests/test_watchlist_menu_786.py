"""The "add to list" menu every other board carries (#786 step 5): ``module_watchlists.menu`` and its two routes.

A board knows a plain ticker; avo's lists hold TradingView spellings. These pin how one meets the other: a ticker
is on a list when a US listing of it is there whatever its prefix, a tick writes the spine's spelling into the
list's first section, an untick takes every US listing of it off, and the two job lists keep writing ``ops.sqlite``
the way the old Stock / Option pair did.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.module_watchlists import menu, store
from RevenueChaser.rcdata import ops, read

pytestmark = pytest.mark.rawstore_schema_from_scratch

SPINE = {"NVDA": "NASDAQ", "BRK.B": "NYSE", "SPY": "NYSE ARCA", "SAP": "XETRA"}


@pytest.fixture
def spine(monkeypatch):
    monkeypatch.setattr(menu.read, "identity", lambda tickers=None: [
        {"ticker": t, "exchange": SPINE[t]} for t in (tickers or SPINE) if t in SPINE])


@pytest.fixture
def client(spine):
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


def test_the_menu_offers_avos_lists_first_then_the_two_job_lists_with_their_us_tickers(spine):
    growth = store.create_list("Growth")
    store.add_symbol(growth, "NASDAQ:NVDA")
    store.add_symbol(growth, "TVC:US10Y")          # a series: counted in the size, never matched to a ticker
    store.add_symbol(growth, "BIST:THYAO")         # unsupported: the same
    ops.watch_add("option", "AMD", by="test")
    lists = menu.lists()
    assert [(r["id"], r["group"], r["job"]) for r in lists] == [
        (growth, menu.OWN, False), ("jobs.option", menu.JOBS, True), ("jobs.stock", menu.JOBS, True)]
    assert lists[0]["count"] == 3 and lists[0]["tickers"] == ["NVDA"]
    assert lists[1]["count"] == 1 and lists[1]["tickers"] == ["AMD"]


def test_a_ticker_is_on_a_list_whatever_prefix_it_was_typed_with(spine):
    lid = store.create_list("Mixed")
    store.add_symbol(lid, "AMEX:SPY")
    assert menu.is_on(lid, "spy") is True
    assert menu.is_on(lid, "NVDA") is False


def test_a_tick_writes_the_spines_spelling_into_the_unnamed_top_section(spine):
    lid = store.create_list("Sections")
    store.add_section(lid, "Chips")
    store.add_section(lid, "Software")
    assert menu.set_on(lid, "nvda", True, by="sec_13f") is True
    (item,) = store.items(lid)
    assert (item["symbol"], item["section_id"], item["coverage"], item["added_by"]) == (
        "NASDAQ:NVDA", store.DEFAULT_SECTION, "us_equity", "sec_13f")
    assert menu.set_on(lid, "SPY", True, by="leaps") is True
    assert "AMEX:SPY" in store.symbols(lid), "NYSE Arca is spelled the way the Watch lists add box spells it"


def test_a_second_tick_writes_nothing(spine):
    lid = store.create_list("Once")
    store.add_symbol(lid, "NYSE:BRK.B")
    before = len(store.changes(lid))
    assert menu.set_on(lid, "BRK.B", True, by="test") is True
    assert store.symbols(lid) == ["NYSE:BRK.B"] and len(store.changes(lid)) == before


def test_a_ticker_the_spine_cannot_place_on_a_us_exchange_is_refused_and_nothing_is_written(spine):
    lid = store.create_list("Strict")
    for ticker in ("ZZZZ", "SAP"):
        with pytest.raises(menu.MenuError):
            menu.set_on(lid, ticker, True, by="test")
    assert store.symbols(lid) == []


def test_an_untick_takes_off_every_us_listing_of_the_ticker_and_leaves_the_rest(spine):
    lid = store.create_list("Untick")
    store.add_symbol(lid, "NASDAQ:NVDA")
    store.add_symbol(lid, "OTC:NVDA")
    store.add_symbol(lid, "TVC:US10Y")
    assert menu.set_on(lid, "NVDA", False, by="test") is False
    assert store.symbols(lid) == ["TVC:US10Y"]
    assert menu.set_on(lid, "NVDA", False, by="test") is False     # nothing there: no error, no write


def test_the_job_lists_are_written_through_ops_as_the_pair_did(spine):
    assert menu.set_on("jobs.stock", "nvda", True, by="leaps") is True
    assert read.watched("stock") == {"NVDA"} and read.watched("option") == set()
    assert menu.set_on("jobs.stock", "NVDA", False, by="leaps") is False
    assert read.watched("stock") == set()


def test_the_lists_route_carries_the_menu_and_keeps_the_old_keys(client):
    lid = store.create_list("Growth")
    store.add_symbol(lid, "NASDAQ:NVDA")
    ops.watch_add("option", "AMD", by="test")
    got = client.get("/api/watchlists").get_json()
    assert got["ok"] is True and got["option"] == ["AMD"] and got["stock"] == []
    assert [r["id"] for r in got["lists"]] == [lid, "jobs.option", "jobs.stock"]
    assert got["lists"][0]["tickers"] == ["NVDA"]


def test_the_tick_route_puts_on_and_takes_off_with_the_board_as_added_by(client):
    lid = store.create_list("Growth")
    answer = client.post(f"/api/watchlists/{lid}/tick/nvda", json={"on": True}, headers={"X-RC-Panel": "sec_13f"})
    assert answer.status_code == 200, answer.get_data(as_text=True)
    assert answer.get_json() == {"ok": True, "list_id": lid, "ticker": "NVDA", "on": True}
    assert store.items(lid)[0]["added_by"] == "sec_13f"
    answer = client.post("/api/watchlists/jobs.option/tick/NVDA", json={"on": True}, headers={"X-RC-Panel": "leaps"})
    assert answer.get_json()["on"] is True and read.watched("option") == {"NVDA"}
    answer = client.post(f"/api/watchlists/{lid}/tick/NVDA", json={"on": False})
    assert answer.get_json()["on"] is False and store.symbols(lid) == []


def test_the_tick_route_refuses_what_it_cannot_write(client):
    lid = store.create_list("Growth")
    assert client.post("/api/watchlists/nope/tick/NVDA", json={"on": True}).status_code == 404
    assert client.post(f"/api/watchlists/{lid}/tick/NVDA", json={}).status_code == 400
    assert client.post(f"/api/watchlists/{lid}/tick/NVDA", json={"on": "yes"}).status_code == 400
    assert client.post(f"/api/watchlists/{lid}/tick/%3Cb%3E", json={"on": True}).status_code == 400
    refused = client.post(f"/api/watchlists/{lid}/tick/ZZZZ", json={"on": True})
    assert refused.status_code == 400 and "spine" in refused.get_json()["error"]
    assert store.symbols(lid) == [] and read.watched("option") == set()


def test_the_tick_route_needs_the_panel_header():
    dashboard.app.config["TESTING"] = True
    raw = dashboard.app.test_client()
    assert raw.post("/api/watchlists/jobs.option/tick/NVDA", json={"on": True}).status_code == 403
    assert read.watched("option") == set()
