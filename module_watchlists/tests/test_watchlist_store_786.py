"""The watch lists' own file: lists, ordered sections, symbols kept as avo wrote them (#786).

Every test here writes through ``module_watchlists.store`` only, because that module is the file's one writer.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_watchlists import store, symbols

pytestmark = pytest.mark.rawstore_schema_from_scratch


@pytest.fixture
def a_list():
    return store.create_list("1- Büyüme Motoru", description="AI ve büyüme")


# ── the file ─────────────────────────────────────────────────────────────────

def test_the_tables_live_in_their_own_board_file_and_not_in_raw(a_list):
    assert "watchlists" in rawstore.BOARD_DBS
    assert rawstore.board_db_path("watchlists").name == "watchlists.duckdb"
    for table in (store.LISTS, store.SECTIONS, store.ITEMS, store.CHANGES):
        assert rawstore.MANIFEST[table]["db"] == "watchlists"
    with pytest.raises(rawstore.RawStoreError, match="watchlists"):
        rawstore.query(f"SELECT 1 FROM {store.LISTS}")


def test_a_read_before_anything_is_written_is_empty_and_makes_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(rawstore, "DB_PATH", str(tmp_path / "raw.duckdb"))
    assert store.lists() == []
    assert store.read_list("nope") is None
    assert store.coverage_counts() == {}
    assert not rawstore.board_db_path("watchlists").exists()


# ── lists ────────────────────────────────────────────────────────────────────

def test_a_new_list_gets_a_slug_id_a_description_and_a_default_section(a_list):
    assert a_list == "1-buyume-motoru"
    row = store.get_list(a_list)
    assert row["name"] == "1- Büyüme Motoru"
    assert row["description"] == "AI ve büyüme"
    assert row["feeds"] == ""
    assert [s["section_id"] for s in store.sections(a_list)] == [store.DEFAULT_SECTION]


def test_two_lists_cannot_share_a_name(a_list):
    with pytest.raises(store.WatchlistError, match="already there"):
        store.create_list("1- Büyüme Motoru")


def test_a_clashing_slug_gets_a_suffix_so_both_lists_keep_their_own_id():
    first = store.create_list("Energy")
    second = store.create_list("energy!")
    assert (first, second) == ("energy", "energy-2")


def test_an_empty_name_is_refused():
    with pytest.raises(store.WatchlistError, match="empty"):
        store.create_list("   ")


def test_a_list_can_be_renamed_and_given_a_description_without_losing_its_id(a_list):
    store.update_list(a_list, name="Büyüme", description="yeni açıklama")
    row = store.get_list(a_list)
    assert (row["list_id"], row["name"], row["description"]) == (a_list, "Büyüme", "yeni açıklama")


def test_renaming_onto_another_lists_name_is_refused(a_list):
    other = store.create_list("Energy")
    with pytest.raises(store.WatchlistError, match="already there"):
        store.update_list(other, name="1- Büyüme Motoru")
    assert store.get_list(other)["name"] == "Energy"


def test_lists_come_back_in_the_order_they_were_made_with_their_symbol_counts(a_list):
    second = store.create_list("Energy")
    store.add_symbol(a_list, "NASDAQ:NVDA")
    store.add_symbol(a_list, "NASDAQ:AMD")
    store.add_symbol(second, "NYSE:CVX")
    rows = store.lists()
    assert [(r["list_id"], r["symbol_count"]) for r in rows] == [(a_list, 2), (second, 1)]


def test_a_list_with_no_symbol_yet_is_still_one_of_the_lists(a_list):
    """The whole file is built when the first list is made.

    ``lists()`` joins ``wl_item``, so before this a file holding a list and no item row made that read raise, and
    the board answered "no watch list yet" for a list avo had just created (CI, 12 Nov 2026).
    """
    assert [(r["list_id"], r["symbol_count"]) for r in store.lists()] == [(a_list, 0)]
    assert store.sections(a_list) and store.items(a_list) == [] and store.changes(a_list) == []


# ── sections ─────────────────────────────────────────────────────────────────

def test_sections_keep_avos_order_not_the_alphabet(a_list):
    store.add_section(a_list, "AI ÇIPLERI — GPU VE ACCELERATOR")
    store.add_section(a_list, "QUANTUM")
    store.add_section(a_list, "BIST BÜYÜME")
    assert [s["name"] for s in store.sections(a_list)][1:] == [
        "AI ÇIPLERI — GPU VE ACCELERATOR", "QUANTUM", "BIST BÜYÜME"]


def test_adding_a_heading_the_list_already_has_returns_the_same_section(a_list):
    first = store.add_section(a_list, "QUANTUM")
    assert store.add_section(a_list, "QUANTUM") == first
    assert len(store.sections(a_list)) == 2            # the default one and QUANTUM


def test_a_section_on_an_unknown_list_is_refused():
    with pytest.raises(store.WatchlistError, match="no watch list"):
        store.add_section("nope", "QUANTUM")


# ── symbols ──────────────────────────────────────────────────────────────────

def test_a_symbol_is_stored_as_avo_wrote_it_split_into_its_parts(a_list):
    store.add_symbol(a_list, " nasdaq:nvda ")
    row = store.items(a_list)[0]
    assert (row["symbol"], row["exchange"], row["ticker"]) == ("NASDAQ:NVDA", "NASDAQ", "NVDA")
    assert row["coverage"] == "us_equity"


def test_an_unsupported_symbol_is_kept_so_the_list_stays_whole(a_list):
    assert store.add_symbol(a_list, "BIST:THYAO") is True
    row = store.items(a_list)[0]
    assert row["coverage"] == "unsupported"
    assert store.symbols(a_list) == ["BIST:THYAO"]


def test_a_second_add_of_the_same_symbol_changes_nothing_and_is_not_an_error(a_list):
    assert store.add_symbol(a_list, "NASDAQ:NVDA") is True
    assert store.add_symbol(a_list, "nasdaq:nvda") is False
    assert store.symbols(a_list) == ["NASDAQ:NVDA"]


def test_the_same_symbol_can_sit_on_two_lists(a_list):
    other = store.create_list("Energy")
    store.add_symbol(a_list, "NASDAQ:NVDA")
    assert store.add_symbol(other, "NASDAQ:NVDA") is True
    assert store.symbols(other) == ["NASDAQ:NVDA"]


def test_symbols_keep_the_order_they_were_added_in_inside_their_section(a_list):
    chips = store.add_section(a_list, "AI ÇIPLERI")
    for ticker in ("NASDAQ:CRDO", "NASDAQ:SITM", "NASDAQ:LSCC", "NASDAQ:NVDA"):
        store.add_symbol(a_list, ticker, section_id=chips)
    assert store.symbols(a_list) == ["NASDAQ:CRDO", "NASDAQ:SITM", "NASDAQ:LSCC", "NASDAQ:NVDA"]


def test_a_whole_list_reads_section_by_section_not_by_the_position_number(a_list):
    """``position`` counts inside a section, so two sections both have a 0 and a 1. The list's own order is the
    section's place first — otherwise the second symbol of every section came before the third of the first."""
    chips = store.add_section(a_list, "AI ÇIPLERI")
    quantum = store.add_section(a_list, "QUANTUM")
    for ticker in ("NASDAQ:CRDO", "NASDAQ:SITM", "NASDAQ:NVDA"):
        store.add_symbol(a_list, ticker, section_id=chips)
    for ticker in ("NYSE:IONQ", "NASDAQ:QBTS"):
        store.add_symbol(a_list, ticker, section_id=quantum)
    assert store.symbols(a_list) == ["NASDAQ:CRDO", "NASDAQ:SITM", "NASDAQ:NVDA", "NYSE:IONQ", "NASDAQ:QBTS"]


def test_a_symbol_put_in_a_section_of_another_list_is_refused(a_list):
    other = store.create_list("Energy")
    chips = store.add_section(a_list, "AI ÇIPLERI")
    with pytest.raises(store.WatchlistError, match="no section"):
        store.add_symbol(other, "NASDAQ:NVDA", section_id=chips)
    assert store.symbols(other) == []


def test_removing_a_symbol_takes_it_off_that_list_only(a_list):
    other = store.create_list("Energy")
    store.add_symbol(a_list, "NASDAQ:NVDA")
    store.add_symbol(other, "NASDAQ:NVDA")
    assert store.remove_symbol(a_list, "NASDAQ:NVDA") is True
    assert store.remove_symbol(a_list, "NASDAQ:NVDA") is False
    assert store.symbols(a_list) == []
    assert store.symbols(other) == ["NASDAQ:NVDA"]


def test_a_move_puts_a_symbol_in_another_section_of_the_same_list(a_list):
    chips = store.add_section(a_list, "AI ÇIPLERI")
    quantum = store.add_section(a_list, "QUANTUM")
    store.add_symbol(a_list, "NYSE:IONQ", section_id=chips)
    store.move_symbol(a_list, "NYSE:IONQ", section_id=quantum)
    assert [i["symbol"] for i in store.items(a_list, quantum)] == ["NYSE:IONQ"]
    assert store.items(a_list, chips) == []


def test_reorder_gives_the_named_symbols_their_new_order_and_keeps_the_rest_behind(a_list):
    chips = store.add_section(a_list, "AI ÇIPLERI")
    for ticker in ("NASDAQ:CRDO", "NASDAQ:SITM", "NASDAQ:NVDA", "NASDAQ:AMD"):
        store.add_symbol(a_list, ticker, section_id=chips)
    store.reorder(a_list, chips, ["NASDAQ:NVDA", "NASDAQ:AMD"])
    assert store.symbols(a_list) == ["NASDAQ:NVDA", "NASDAQ:AMD", "NASDAQ:CRDO", "NASDAQ:SITM"]


# ── the whole list, as the board reads it ────────────────────────────────────

def test_read_list_gives_the_sections_in_order_each_with_its_own_symbols(a_list):
    chips = store.add_section(a_list, "AI ÇIPLERI")
    quantum = store.add_section(a_list, "QUANTUM")
    store.add_symbol(a_list, "NASDAQ:NVDA", section_id=chips)
    store.add_symbol(a_list, "NYSE:IONQ", section_id=quantum)
    whole = store.read_list(a_list)
    named = [s for s in whole["sections"] if s["name"]]
    assert [(s["name"], [i["symbol"] for i in s["items"]]) for s in named] == [
        ("AI ÇIPLERI", ["NASDAQ:NVDA"]), ("QUANTUM", ["NYSE:IONQ"])]


def test_coverage_counts_say_how_many_symbols_have_no_source_yet(a_list):
    store.add_symbol(a_list, "NASDAQ:NVDA")
    store.add_symbol(a_list, "BIST:THYAO")
    store.add_symbol(a_list, "SZSE:002594")
    store.add_symbol(a_list, "TVC:US10Y")
    assert store.coverage_counts(a_list) == {"us_equity": 1, "unsupported": 2, "market_series": 1}


# ── history ──────────────────────────────────────────────────────────────────

def test_every_add_and_remove_leaves_a_change_row(a_list):
    store.add_symbol(a_list, "NASDAQ:NVDA", by="avo")
    store.remove_symbol(a_list, "NASDAQ:NVDA", by="avo")
    rows = store.changes(a_list)
    assert [(r["symbol"], r["action"], r["changed_by"]) for r in rows] == [
        ("NASDAQ:NVDA", "remove", "avo"), ("NASDAQ:NVDA", "add", "avo")]


def test_deleting_a_list_takes_its_rows_but_keeps_what_it_held_in_the_history(a_list):
    store.add_symbol(a_list, "NASDAQ:NVDA")
    assert store.delete_list(a_list) is True
    assert store.delete_list(a_list) is False
    assert store.get_list(a_list) is None
    assert store.items(a_list) == []
    assert store.sections(a_list) == []
    assert {r["symbol"] for r in store.changes(a_list)} == {"NASDAQ:NVDA"}


# ── the symbol reader ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, coverage", [
    ("NASDAQ:NVDA", "us_equity"),
    ("NYSE:CVX", "us_equity"),
    ("AMEX:XLE", "us_equity"),
    ("OTC:EVVTY", "us_equity"),
    ("BIST:THYAO", "unsupported"),
    ("SZSE:002594", "unsupported"),
    ("COMEX:GC1!", "unsupported"),
    ("COINBASE:BTCUSD", "unsupported"),
    ("CAPITALCOM:SPX500", "unsupported"),
    ("FRED:SOFR", "unsupported"),
    ("CBOE:VXN", "unsupported"),
    ("INDEX:S5FD", "unsupported"),
    ("TVC:VIX", "market_series"),
    ("TVC:US10Y", "market_series"),
    ("FRED:RRPONTSYD", "market_series"),
    ("TVC:US10Y-TVC:US02Y", "market_series"),
    ("AMEX:VUG/AMEX:VTV", "derived"),
    ("AMEX:USD/IBKR:USDJPY", "derived"),
])
def test_coverage_is_read_from_the_spelling_alone(raw, coverage):
    assert symbols.parse(raw).coverage == coverage


def test_a_served_series_names_the_series_id_a_reader_asks_for():
    assert symbols.parse("TVC:US10Y").series_id == "DGS10"
    assert symbols.parse("TVC:US10Y-TVC:US02Y").series_id == "T10Y2Y"
    assert symbols.parse("NASDAQ:NVDA").series_id is None


def test_a_symbol_with_no_exchange_prefix_keeps_its_ticker():
    assert symbols.split("NVDA") == ("", "NVDA")
    assert symbols.parse("NVDA").coverage == "unsupported"


def test_an_empty_symbol_is_refused_by_the_store(a_list):
    with pytest.raises(store.WatchlistError, match="empty"):
        store.add_symbol(a_list, "   ")
