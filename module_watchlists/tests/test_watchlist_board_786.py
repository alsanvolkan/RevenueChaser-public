"""The Watch lists board: the payload the page is drawn from, and the search behind the add box (#786).

The numbers come from the rcdata layer, so every test here stubs ``rcdata.read`` rather than building a serving
file: what is under test is which number lands in which key, and that a symbol with no source keeps its row.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.module_watchlists import board, store

pytestmark = pytest.mark.rawstore_schema_from_scratch


def _bars(rows: list[tuple]):
    """A ``ticker_bars``-shaped frame: ``(ticker, session_date, close, volume)`` tuples."""
    import pandas as pd
    return pd.DataFrame([{"ticker": t, "session_date": pd.Timestamp(d), "close": c, "volume": v}
                         for t, d, c, v in rows])


@pytest.fixture
def no_data(monkeypatch):
    """Every rcdata read answers empty, so a test opts in to the one it is about."""
    import pandas as pd
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: pd.DataFrame(
        columns=["ticker", "session_date", "close", "volume"]))
    monkeypatch.setattr(board.read, "latest_market", lambda *a, **k: {})
    monkeypatch.setattr(board.read, "market_series", lambda *a, **k: [])
    monkeypatch.setattr(board.read, "identity", lambda *a, **k: [])


# ── the payload ──────────────────────────────────────────────────────────────

def test_no_list_at_all_is_no_payload_so_the_tab_can_show_its_empty_page(no_data):
    assert board.payload() is None


def test_a_us_listing_carries_its_close_change_volume_and_cap(monkeypatch, no_data):
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "NASDAQ:NVDA")
    today = date.today()
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: _bars([
        ("NVDA", today - timedelta(days=2), 100.0, 1_000_000.0),
        ("NVDA", today - timedelta(days=1), 110.0, 3_000_000.0),
    ]))
    monkeypatch.setattr(board.read, "latest_market",
                        lambda *a, **k: {"NVDA": {"price": 110.0, "market_cap": 4.19e12}})
    monkeypatch.setattr(board.read, "identity", lambda *a, **k: [{"ticker": "NVDA", "name": "NVIDIA Corporation"}])
    row = board.payload(list_id)["sections"][0]["rows"][0]
    assert row["t"] == "NVDA" and row["nm"] == "NVIDIA Corporation"
    assert row["last"] == 110.0
    assert row["chg"] == pytest.approx(10.0)
    assert row["chgp"] == pytest.approx(10.0)
    assert row["vol"] == 3_000_000.0
    assert row["avol"] == pytest.approx(2_000_000.0)       # the window's mean, not the last session
    assert row["cap"] == 4.19e12


def test_one_bar_is_a_price_with_no_change_rather_than_a_change_of_zero(monkeypatch, no_data):
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "NASDAQ:NVDA")
    monkeypatch.setattr(board.read, "ticker_bars",
                        lambda *a, **k: _bars([("NVDA", date.today(), 110.0, 5.0)]))
    row = board.payload(list_id)["sections"][0]["rows"][0]
    assert row["last"] == 110.0
    assert row["chg"] is None and row["chgp"] is None


def test_a_symbol_we_have_no_source_for_keeps_its_row_and_shows_no_number(no_data):
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "BIST:THYAO")
    data = board.payload(list_id)
    row = data["sections"][0]["rows"][0]
    assert row["cov"] == "unsupported"
    assert (row["last"], row["chg"], row["vol"], row["cap"]) == (None, None, None, None)
    assert data["counts"]["unsupported"] == 1


def test_a_market_series_shows_its_level_and_change_and_a_yield_says_percent(monkeypatch, no_data):
    list_id = store.create_list("Macro")
    store.add_symbol(list_id, "TVC:US10Y")
    monkeypatch.setattr(board.read, "market_series", lambda *a, **k: [
        {"series_id": "DGS10", "obs_date": date(2026, 11, 10), "value": 4.13},
        {"series_id": "DGS10", "obs_date": date(2026, 11, 11), "value": 4.11},
    ])
    row = board.payload(list_id)["sections"][0]["rows"][0]
    assert row["last"] == 4.11
    assert row["chg"] == pytest.approx(-0.02)
    assert row["unit"] == "%"
    assert row["vol"] is None and row["cap"] is None       # a series has neither


def test_the_payload_walks_sections_in_avos_order_not_the_symbols_flat(no_data):
    list_id = store.create_list("Growth")
    chips = store.add_section(list_id, "Chips")
    cloud = store.add_section(list_id, "Cloud")
    store.add_symbol(list_id, "NASDAQ:MSFT", section_id=cloud)
    store.add_symbol(list_id, "NASDAQ:NVDA", section_id=chips)
    store.reorder_sections(list_id, [cloud, chips])
    named = [s for s in board.payload(list_id)["sections"] if s["name"]]
    assert [s["name"] for s in named] == ["Cloud", "Chips"]
    assert [row["t"] for s in named for row in s["rows"]] == ["MSFT", "NVDA"]


def test_an_unknown_list_id_opens_the_first_list_instead_of_failing(no_data):
    first = store.create_list("Growth")
    store.create_list("Dividends")
    assert board.payload("no-such-list")["list"]["id"] == first


# ── the add box's search ─────────────────────────────────────────────────────

def test_a_ticker_that_starts_with_the_query_is_offered_before_one_that_only_holds_it(monkeypatch):
    monkeypatch.setattr(board.read, "identity", lambda *a, **k: [
        {"ticker": "ACRD", "name": "Accord Inc", "exchange": "NYSE"},
        {"ticker": "CRDO", "name": "Credo Technology", "exchange": "NASDAQ"},
        {"ticker": "XYZ", "name": "CRD Holdings", "exchange": "NYSE"},
    ])
    assert [r["ticker"] for r in board.search("CRD")] == ["CRDO", "ACRD", "XYZ"]


def test_a_suggestion_is_written_the_way_a_watch_list_spells_it(monkeypatch):
    monkeypatch.setattr(board.read, "identity",
                        lambda *a, **k: [{"ticker": "NVDA", "name": "NVIDIA", "exchange": "NASDAQ"}])
    assert board.search("NVDA")[0]["symbol"] == "NASDAQ:NVDA"


def test_an_empty_query_reads_the_spine_for_nothing(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("an empty query must not read the spine")
    monkeypatch.setattr(board.read, "identity", refuse)
    assert board.search("  ") == []


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_page_says_the_unit_once_in_the_header_and_never_on_a_row(monkeypatch, no_data):
    list_id = store.create_list("Growth")
    store.add_symbol(list_id, "NASDAQ:NVDA")
    monkeypatch.setattr(board.read, "ticker_bars",
                        lambda *a, **k: _bars([("NVDA", date.today(), 110.0, 5.0)]))
    html = board.render_html(board.payload(list_id))
    assert html.count('<span class="wl-hu">USD</span>') == sum(1 for _, _, u in board.PRICE_COLUMNS if u) == 3
    assert '<span class="wl-u">USD</span>' not in html, "a row never repeats the unit"


def test_the_phone_rule_fits_every_column_rather_than_scrolling_sideways():
    phone = board._CSS.split("@media (max-width:720px)", 1)[1]
    assert "overflow-x" not in phone, "the phone shows every column; nothing scrolls sideways"
    assert ".wl-grip,.wl-nm{display:none}" in phone, "the handle and the company name are for the mouse"
