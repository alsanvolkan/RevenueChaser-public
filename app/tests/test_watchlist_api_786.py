"""The Watch lists board's own routes (#786): store-only writes, and the answer the page redraws from.

Every write here goes through ``module_watchlists.store``, so the tests assert what the file holds afterwards as
well as what the route answered. No provider is stubbed because none is called.
"""
from __future__ import annotations

import pytest

from RevenueChaser.app import dashboard
from RevenueChaser.core.tests.panel_client import panel_test_client
from RevenueChaser.module_watchlists import store

pytestmark = pytest.mark.rawstore_schema_from_scratch


@pytest.fixture
def client(monkeypatch):
    """The dashboard with every rcdata read empty: the routes are about the writes, not the numbers."""
    import pandas as pd
    from RevenueChaser.module_watchlists import board
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: pd.DataFrame(
        columns=["ticker", "session_date", "close", "volume"]))
    monkeypatch.setattr(board.read, "latest_market", lambda *a, **k: {})
    monkeypatch.setattr(board.read, "market_series", lambda *a, **k: [])
    monkeypatch.setattr(board.read, "identity", lambda *a, **k: [])
    dashboard.app.config["TESTING"] = True
    return panel_test_client(dashboard.app)


@pytest.fixture
def a_list():
    return store.create_list("Growth", description="AI and growth")


# ── the board itself ─────────────────────────────────────────────────────────

def test_the_tab_shows_its_empty_page_while_no_list_has_been_made(client):
    body = client.get("/view/module_watchlists").get_data(as_text=True)
    assert "No watch list yet." in body


def test_the_tab_renders_the_list_named_in_the_query(client, a_list):
    store.create_list("Dividends")
    store.add_symbol(a_list, "NASDAQ:NVDA")
    body = client.get(f"/view/module_watchlists?list={a_list}").get_data(as_text=True)
    assert "NVDA" in body and "Growth" in body


def test_an_unknown_list_in_the_query_opens_the_first_one_rather_than_404(client, a_list):
    r = client.get("/view/module_watchlists?list=no-such-list")
    assert r.status_code == 200
    assert "Growth" in r.get_data(as_text=True)


# ── the writes ───────────────────────────────────────────────────────────────

def test_a_write_without_the_panel_header_is_refused_and_stores_nothing(client, a_list):
    """The same call twice: refused without the header, accepted with it.

    Both halves matter. The refusal alone proves nothing — ``_local_only`` runs before routing, so a path that
    does not exist at all is refused the same way — so the test says the route is there and that the header is
    what divides the two.
    """
    dashboard.app.config["TESTING"] = True
    bare = dashboard.app.test_client()
    refused = bare.post(f"/api/watchlists/{a_list}/symbol/add", json={"symbol": "NASDAQ:NVDA"})
    assert refused.status_code == 403
    assert store.symbols(a_list) == []
    accepted = client.post(f"/api/watchlists/{a_list}/symbol/add", json={"symbol": "NASDAQ:NVDA"})
    assert accepted.status_code == 200
    assert store.symbols(a_list) == ["NASDAQ:NVDA"]


def test_adding_a_symbol_stores_it_and_answers_with_the_whole_list(client, a_list):
    r = client.post(f"/api/watchlists/{a_list}/symbol/add", json={"symbol": "nasdaq:nvda"})
    assert r.status_code == 200
    answer = r.get_json()
    assert answer["ok"] is True
    assert answer["payload"]["sections"][0]["rows"][0]["t"] == "NVDA"
    assert store.symbols(a_list) == ["NASDAQ:NVDA"]


def test_adding_the_same_symbol_twice_is_not_an_error_and_writes_once(client, a_list):
    client.post(f"/api/watchlists/{a_list}/symbol/add", json={"symbol": "NASDAQ:NVDA"})
    r = client.post(f"/api/watchlists/{a_list}/symbol/add", json={"symbol": "NASDAQ:NVDA"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert store.symbols(a_list) == ["NASDAQ:NVDA"]


def test_an_empty_symbol_is_a_400_and_writes_nothing(client, a_list):
    r = client.post(f"/api/watchlists/{a_list}/symbol/add", json={"symbol": "   "})
    assert r.status_code == 400
    assert store.symbols(a_list) == []


def test_removing_a_symbol_takes_it_off_and_records_the_change(client, a_list):
    store.add_symbol(a_list, "NASDAQ:NVDA")
    r = client.post(f"/api/watchlists/{a_list}/symbol/remove", json={"symbol": "NASDAQ:NVDA"})
    assert r.status_code == 200
    assert store.symbols(a_list) == []
    assert [c["action"] for c in store.changes(a_list)][:1] == ["remove"]


def test_a_dropped_row_moves_into_the_section_and_takes_the_order_it_was_dropped_in(client, a_list):
    chips = store.add_section(a_list, "Chips")
    store.add_symbol(a_list, "NASDAQ:NVDA", section_id=chips)
    store.add_symbol(a_list, "NASDAQ:AMD", section_id=chips)
    store.add_symbol(a_list, "NASDAQ:MSFT")
    r = client.post(f"/api/watchlists/{a_list}/symbol/move",
                    json={"symbol": "NASDAQ:MSFT", "section_id": chips,
                          "order": ["NASDAQ:NVDA", "NASDAQ:MSFT", "NASDAQ:AMD"]})
    assert r.status_code == 200
    assert [i["symbol"] for i in store.items(a_list, chips)] == ["NASDAQ:NVDA", "NASDAQ:MSFT", "NASDAQ:AMD"]


def test_a_move_into_a_section_of_another_list_is_a_400(client, a_list):
    other = store.create_list("Dividends")
    elsewhere = store.add_section(other, "Utilities")
    store.add_symbol(a_list, "NASDAQ:NVDA")
    r = client.post(f"/api/watchlists/{a_list}/symbol/move",
                    json={"symbol": "NASDAQ:NVDA", "section_id": elsewhere})
    assert r.status_code == 400
    assert store.items(a_list)[0]["section_id"] == store.DEFAULT_SECTION


# ── sections ─────────────────────────────────────────────────────────────────

def test_the_plus_button_adds_a_heading_at_the_end(client, a_list):
    store.add_section(a_list, "Chips")
    r = client.post(f"/api/watchlists/{a_list}/section/add", json={"name": "Cloud"})
    assert r.status_code == 200
    assert [s["name"] for s in store.sections(a_list)][-1] == "Cloud"


def test_renaming_a_heading_keeps_its_id_so_its_symbols_do_not_move(client, a_list):
    chips = store.add_section(a_list, "Chips")
    store.add_symbol(a_list, "NASDAQ:NVDA", section_id=chips)
    r = client.post(f"/api/watchlists/{a_list}/section/rename", json={"section_id": chips, "name": "AI chips"})
    assert r.status_code == 200
    assert [i["symbol"] for i in store.items(a_list, chips)] == ["NASDAQ:NVDA"]
    assert {s["section_id"]: s["name"] for s in store.sections(a_list)}[chips] == "AI chips"


def test_deleting_a_heading_keeps_its_symbols_in_the_first_remaining_section(client, a_list):
    chips = store.add_section(a_list, "Chips")
    store.add_symbol(a_list, "NASDAQ:NVDA", section_id=chips)
    r = client.post(f"/api/watchlists/{a_list}/section/delete", json={"section_id": chips})
    assert r.status_code == 200
    assert store.symbols(a_list) == ["NASDAQ:NVDA"]
    assert chips not in {s["section_id"] for s in store.sections(a_list)}


def test_the_last_section_of_a_list_cannot_be_deleted(client, a_list):
    only = store.sections(a_list)[0]["section_id"]
    r = client.post(f"/api/watchlists/{a_list}/section/delete", json={"section_id": only})
    assert r.status_code == 400
    assert store.sections(a_list)


def test_a_dropped_heading_reorders_the_sections(client, a_list):
    chips = store.add_section(a_list, "Chips")
    cloud = store.add_section(a_list, "Cloud")
    first = store.sections(a_list)[0]["section_id"]
    r = client.post(f"/api/watchlists/{a_list}/sections/order", json={"order": [cloud, chips, first]})
    assert r.status_code == 200
    assert [s["section_id"] for s in store.sections(a_list)] == [cloud, chips, first]


def test_an_order_that_is_not_a_list_of_ids_is_a_400(client, a_list):
    r = client.post(f"/api/watchlists/{a_list}/sections/order", json={"order": [1, 2]})
    assert r.status_code == 400


# ── the list itself ──────────────────────────────────────────────────────────

def test_the_menu_renames_the_list_and_edits_its_description(client, a_list):
    r = client.post(f"/api/watchlists/{a_list}", json={"name": "1- Büyüme Motoru", "description": "AI zinciri"})
    assert r.status_code == 200
    row = store.get_list(a_list)
    assert row["name"] == "1- Büyüme Motoru" and row["description"] == "AI zinciri"


def test_a_description_can_be_emptied_but_a_name_cannot(client, a_list):
    assert client.post(f"/api/watchlists/{a_list}", json={"description": ""}).status_code == 200
    assert store.get_list(a_list)["description"] == ""
    assert client.post(f"/api/watchlists/{a_list}", json={"name": "  "}).status_code == 400
    assert store.get_list(a_list)["name"] == "Growth"


def test_a_new_list_answers_its_id_and_a_taken_name_is_a_400(client, a_list):
    r = client.post("/api/watchlists/new", json={"name": "Dividends"})
    assert r.status_code == 200 and r.get_json()["list_id"]
    assert client.post("/api/watchlists/new", json={"name": "Growth"}).status_code == 400


def test_deleting_a_list_removes_it_and_keeps_what_it_held_readable(client, a_list):
    store.add_symbol(a_list, "NASDAQ:NVDA")
    r = client.post(f"/api/watchlists/{a_list}/delete", json={})
    assert r.status_code == 200 and r.get_json()["deleted"] is True
    assert store.get_list(a_list) is None
    assert "NASDAQ:NVDA" in {c["symbol"] for c in store.changes(a_list)}


def test_a_write_to_a_list_that_is_not_there_is_a_404_or_a_400_never_a_silent_ok(client):
    assert client.post("/api/watchlists/ghost/symbol/add", json={"symbol": "NASDAQ:NVDA"}).status_code == 400
    assert client.post("/api/watchlists/ghost/delete", json={}).status_code == 404
    assert client.post("/api/watchlists/ghost/sections/order", json={"order": []}).status_code == 404


# ── the derived tabs ─────────────────────────────────────────────────────────

def test_a_derived_tab_answers_one_row_per_symbol_the_page_can_draw(client, a_list, monkeypatch):
    """The route's shape: the symbol as avo wrote it is the key, so the page can put the row where it belongs.

    The numbers themselves are the board's own tests (``module_watchlists/tests``); what matters here is that the
    lazy fetch hands the page a row for each symbol it has already drawn.
    """
    import datetime as dt

    import pandas as pd
    from RevenueChaser.module_watchlists import board
    start = dt.date(2026, 1, 2)
    rows = [{"ticker": t, "session_date": pd.Timestamp(start + dt.timedelta(days=i)),
             "close": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "volume": 1_000_000.0}
            for t in ("NVDA", "AMD", "SPY") for i in range(120)]
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: pd.DataFrame(rows))
    store.add_symbol(a_list, "NASDAQ:NVDA")
    store.add_symbol(a_list, "NASDAQ:AMD")
    r = client.get(f"/api/watchlists/{a_list}/columns/performance")
    assert r.status_code == 200
    answer = r.get_json()
    assert answer["ok"] is True and answer["tab"] == "performance"
    assert set(answer["values"]) == {"NASDAQ:NVDA", "NASDAQ:AMD"}
    assert answer["values"]["NASDAQ:NVDA"]["p1M"] > 0


def test_the_price_tab_is_not_fetched_because_the_first_payload_carries_it(client, a_list):
    assert client.get(f"/api/watchlists/{a_list}/columns/price").status_code == 404


def test_an_unknown_tab_or_list_is_a_404(client, a_list):
    assert client.get(f"/api/watchlists/{a_list}/columns/nope").status_code == 404
    assert client.get("/api/watchlists/ghost/columns/risk").status_code == 404


# ── the export and the search ────────────────────────────────────────────────

def test_the_export_is_tradingviews_own_format_with_avos_spelling(client, a_list):
    chips = store.add_section(a_list, "Chips")
    store.add_symbol(a_list, "NASDAQ:NVDA", section_id=chips)
    store.add_symbol(a_list, "BIST:THYAO", section_id=chips)
    r = client.get(f"/api/watchlists/{a_list}/export.txt")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "NASDAQ:NVDA" in text and "BIST:THYAO" in text
    assert "filename=" in r.headers["Content-Disposition"]


def test_the_export_of_a_list_that_is_not_there_is_a_404(client):
    assert client.get("/api/watchlists/ghost/export.txt").status_code == 404


def test_the_search_answers_the_spines_names(client, monkeypatch):
    from RevenueChaser.module_watchlists import board
    monkeypatch.setattr(board.read, "identity",
                        lambda *a, **k: [{"ticker": "NVDA", "name": "NVIDIA", "exchange": "NASDAQ"}])
    rows = client.get("/api/watchlists/search?q=NV").get_json()["rows"]
    assert rows[0]["symbol"] == "NASDAQ:NVDA"
