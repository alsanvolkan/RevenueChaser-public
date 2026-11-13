"""Reading avo's TradingView exports into the watch-list file, and writing them back out (#786).

The fixtures are the real spellings out of his own nine files: both heading forms, a file that opens with symbols
before any heading, a file with no heading at all, and the trailing empty heading his ``index.txt`` ends with.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_watchlists import importer, store, tradingview

pytestmark = pytest.mark.rawstore_schema_from_scratch

GROWTH = ("### AI ÇIPLERI — GPU VE ACCELERATOR,NASDAQ:CRDO,NASDAQ:SITM,NASDAQ:NVDA,"
          "### QUANTUM,NYSE:IONQ,NASDAQ:QBTS,"
          "### BIST BÜYÜME,BIST:THYAO,BIST:TUPRS")
INDEX = ("###INDEX FEATURES,CAPITALCOM:SPX500,NASDAQ:QQQ,"
         "###MACRO MARKET WATCH,TVC:VIX,TVC:US10Y,AMEX:VUG/AMEX:VTV,"
         "###SECTION 1")
NO_HEADING = "AMEX:EWZ,AMEX:EWY,AMEX:FXI"
LEADING_SYMBOLS = "NYSE:RDW,NASDAQ:RKLB,### UZAY,NYSE:PL"


# ── the format ───────────────────────────────────────────────────────────────

def test_both_heading_spellings_are_read():
    assert [s.name for s in tradingview.parse_export(GROWTH)] == [
        "AI ÇIPLERI — GPU VE ACCELERATOR", "QUANTUM", "BIST BÜYÜME"]
    assert [s.name for s in tradingview.parse_export(INDEX)] == [
        "INDEX FEATURES", "MACRO MARKET WATCH", "SECTION 1"]


def test_a_file_with_no_heading_is_one_nameless_section():
    sections = tradingview.parse_export(NO_HEADING)
    assert len(sections) == 1
    assert sections[0].name == ""
    assert sections[0].symbols == ["AMEX:EWZ", "AMEX:EWY", "AMEX:FXI"]


def test_symbols_before_the_first_heading_keep_their_place():
    sections = tradingview.parse_export(LEADING_SYMBOLS)
    assert [(s.name, s.symbols) for s in sections] == [
        ("", ["NYSE:RDW", "NASDAQ:RKLB"]), ("UZAY", ["NYSE:PL"])]


def test_a_trailing_heading_with_no_symbols_survives():
    assert tradingview.parse_export(INDEX)[-1] == tradingview.Section("SECTION 1", [])


def test_an_empty_export_is_no_sections():
    assert tradingview.parse_export("") == []
    assert tradingview.parse_export("  ,, ") == []


@pytest.mark.parametrize("text", [GROWTH, INDEX, NO_HEADING, LEADING_SYMBOLS])
def test_a_round_trip_keeps_the_order_the_headings_and_the_spelling(text):
    once = tradingview.parse_export(text)
    assert tradingview.dumps(once) == text.replace("### ", "###")
    assert tradingview.parse_export(tradingview.dumps(once)) == once


def test_counts_say_what_an_import_would_write():
    assert tradingview.counts(tradingview.parse_export(GROWTH)) == {
        "sections": 3, "symbols": 7, "us_equity": 5, "unsupported": 2}


# ── importing ────────────────────────────────────────────────────────────────

@pytest.fixture
def export_dir(tmp_path):
    (tmp_path / "1- Büyüme Motoru.txt").write_text(GROWTH, encoding="utf-8")
    (tmp_path / "country_etf.txt").write_text(NO_HEADING, encoding="utf-8")
    (tmp_path / "notes.md").write_text("not an export", encoding="utf-8")
    return tmp_path


def test_the_file_name_becomes_the_list_name_with_underscores_read_as_spaces(tmp_path):
    assert importer.list_name_of(tmp_path / "1-_Büyüme_Motoru.txt") == "1- Büyüme Motoru"
    assert importer.list_name_of(tmp_path / "country etf.txt") == "country etf"


def test_a_dry_run_writes_nothing_but_reports_what_it_would_write(export_dir):
    reports = [importer.import_file(p) for p in importer.files_under(export_dir)]
    assert sum(r.counts["symbols"] for r in reports) == 10
    assert store.lists() == []


def test_only_export_suffixes_are_read(export_dir):
    assert [p.name for p in importer.files_under(export_dir)] == ["1- Büyüme Motoru.txt", "country_etf.txt"]


def test_an_import_makes_the_list_its_sections_and_its_symbols_in_order(export_dir):
    report = importer.import_file(export_dir / "1- Büyüme Motoru.txt", apply=True)
    assert report.added == 7
    whole = store.read_list(report.list_id)
    assert whole["name"] == "1- Büyüme Motoru"
    named = [(s["name"], [i["symbol"] for i in s["items"]]) for s in whole["sections"] if s["name"]]
    assert named == [
        ("AI ÇIPLERI — GPU VE ACCELERATOR", ["NASDAQ:CRDO", "NASDAQ:SITM", "NASDAQ:NVDA"]),
        ("QUANTUM", ["NYSE:IONQ", "NASDAQ:QBTS"]),
        ("BIST BÜYÜME", ["BIST:THYAO", "BIST:TUPRS"]),
    ]


def test_a_file_with_no_heading_lands_in_the_default_section(export_dir):
    report = importer.import_file(export_dir / "country_etf.txt", apply=True)
    assert store.symbols(report.list_id) == ["AMEX:EWZ", "AMEX:EWY", "AMEX:FXI"]
    assert [i["section_id"] for i in store.items(report.list_id)] == [store.DEFAULT_SECTION] * 3


def test_the_symbols_with_no_source_yet_are_named_so_they_can_be_checked(export_dir):
    report = importer.import_file(export_dir / "1- Büyüme Motoru.txt")
    assert report.unsupported == ["BIST:THYAO", "BIST:TUPRS"]


def test_importing_the_same_file_twice_adds_nothing_and_keeps_the_first_order(export_dir):
    first = importer.import_file(export_dir / "1- Büyüme Motoru.txt", apply=True)
    before = store.symbols(first.list_id)
    again = importer.import_file(export_dir / "1- Büyüme Motoru.txt", apply=True)
    assert (again.list_id, again.added, again.already_there) == (first.list_id, 0, 7)
    assert store.symbols(first.list_id) == before
    assert len(store.lists()) == 1


def test_a_second_import_adds_only_what_is_new(export_dir):
    path = export_dir / "1- Büyüme Motoru.txt"
    report = importer.import_file(path, apply=True)
    path.write_text(GROWTH + ",NASDAQ:AMD", encoding="utf-8")
    again = importer.import_file(path, apply=True)
    assert (again.added, again.already_there) == (1, 7)
    assert store.symbols(report.list_id)[-1] == "NASDAQ:AMD"


def test_replace_empties_the_list_first(export_dir):
    path = export_dir / "1- Büyüme Motoru.txt"
    first = importer.import_file(path, apply=True)
    path.write_text("### QUANTUM,NYSE:IONQ", encoding="utf-8")
    importer.import_file(path, apply=True, replace=True)
    assert store.symbols(first.list_id) == ["NYSE:IONQ"]


# ── exporting ────────────────────────────────────────────────────────────────

def test_a_list_goes_back_out_in_the_format_it_came_in(export_dir):
    report = importer.import_file(export_dir / "1- Büyüme Motoru.txt", apply=True)
    assert importer.export_list(report.list_id) == GROWTH.replace("### ", "###")


def test_exporting_an_unknown_list_is_an_error():
    with pytest.raises(store.WatchlistError, match="no watch list"):
        importer.export_list("nope")


# ── the command ──────────────────────────────────────────────────────────────

def test_the_command_writes_nothing_without_apply(export_dir, capsys):
    assert importer.main(["import", str(export_dir)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert store.lists() == []


def test_the_command_with_apply_makes_the_lists(export_dir, capsys):
    assert importer.main(["import", str(export_dir), "--apply"]) == 0
    assert "DRY RUN" not in capsys.readouterr().out
    assert {r["name"] for r in store.lists()} == {"1- Büyüme Motoru", "country etf"}


def test_the_command_refuses_a_folder_with_no_export(tmp_path, capsys):
    assert importer.main(["import", str(tmp_path)]) == 2
    assert "no .txt or .csv export found" in capsys.readouterr().err
