"""Read TradingView exports into the watch-list file, and write them back out (#786).

``python -m RevenueChaser.module_watchlists import <file-or-folder> ... [--apply]``

**A dry run by default.** Without ``--apply`` nothing is written: the command reads every file, says what each list
would hold, and names every symbol we have no source for, so the numbers can be checked before the store is
touched. ``--apply`` makes the lists.

A file's name becomes the list's name (``1- Büyüme Motoru.txt`` -> "1- Büyüme Motoru"), because that is what avo
called it in TradingView. Re-running an import over a list that is already there **adds what is missing and
changes nothing else** (``--replace`` empties the list first, and asks for it in as many words). A symbol already
on the list keeps its place, its note and the day it was added.

``export`` writes a list back out in the same one-line format, so a list edited here can go back into TradingView.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import store, symbols as symbols_mod, tradingview

logger = logging.getLogger(__name__)

SUFFIXES = (".txt", ".csv")


class ImportReport:
    """What one file's import did, or would do."""

    def __init__(self, path: Path, name: str, sections: list[tradingview.Section]):
        self.path = path
        self.name = name
        self.sections = sections
        self.counts = tradingview.counts(sections)
        self.list_id: str | None = None
        self.added = 0
        self.already_there = 0
        self.unsupported: list[str] = [
            s for section in sections for s in section.symbols
            if symbols_mod.parse(s).coverage == "unsupported"]

    def line(self) -> str:
        where = f" -> {self.list_id}" if self.list_id else ""
        return (f"{self.name}{where}: {self.counts['sections']} sections, {self.counts['symbols']} symbols, "
                f"{len(self.unsupported)} with no source yet "
                f"(added {self.added}, already there {self.already_there})")


def list_name_of(path: Path) -> str:
    """The list's name is the file's name without its suffix, with underscores read as spaces — a file copied off
    a phone or through a mail client comes back as ``1-_Büyüme_Motoru.txt``."""
    return " ".join(path.stem.replace("_", " ").split())


def read_file(path: Path) -> list[tradingview.Section]:
    return tradingview.parse_export(path.read_text(encoding="utf-8"))


def files_under(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.iterdir() if p.is_file() and p.suffix.lower() in SUFFIXES)
    return [target]


def import_file(path: Path, *, apply: bool = False, replace: bool = False, by: str = "avo") -> ImportReport:
    """Read one export. With ``apply`` the list is made (or found) and the missing symbols are added, in order."""
    sections = read_file(path)
    report = ImportReport(path, list_name_of(path), sections)
    if not apply:
        return report

    existing = next((row for row in store.lists() if row["name"] == report.name), None)
    if existing is None:
        report.list_id = store.create_list(report.name, by=by)
    else:
        report.list_id = existing["list_id"]
        if replace:
            for symbol in store.symbols(report.list_id):
                store.remove_symbol(report.list_id, symbol, by=by)

    for section in sections:
        section_id = (store.add_section(report.list_id, section.name) if section.name
                      else store.DEFAULT_SECTION)
        for symbol in section.symbols:
            if store.add_symbol(report.list_id, symbol, section_id=section_id, by=by):
                report.added += 1
            else:
                report.already_there += 1
    return report


def export_list(list_id: str) -> str:
    """One list back out in TradingView's format, sections and order as stored."""
    whole = store.read_list(list_id)
    if whole is None:
        raise store.WatchlistError(f"no watch list {list_id!r}")
    sections = [tradingview.Section(s["name"], [i["symbol"] for i in s["items"]]) for s in whole["sections"]]
    return tradingview.dumps(sections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m RevenueChaser.module_watchlists", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="read TradingView exports into the watch-list file")
    imp.add_argument("targets", nargs="+", type=Path, help="a file or a folder of .txt exports")
    imp.add_argument("--apply", action="store_true", help="write; without it the command only reports")
    imp.add_argument("--replace", action="store_true",
                     help="empty a list that is already there before reading the file into it")
    imp.add_argument("--by", default="avo")

    exp = sub.add_parser("export", help="write one list back out in TradingView's format")
    exp.add_argument("list_id")

    lst = sub.add_parser("lists", help="what is stored")
    lst.add_argument("--coverage", action="store_true", help="also count the symbols by coverage")

    args = parser.parse_args(argv)

    if args.command == "export":
        print(export_list(args.list_id))
        return 0

    if args.command == "lists":
        for row in store.lists():
            print(f"{row['list_id']}\t{row['symbol_count']:>4}\t{row['name']}")
        if args.coverage:
            for key, n in sorted(store.coverage_counts().items()):
                print(f"{key}\t{n}")
        return 0

    paths = [p for target in args.targets for p in files_under(target)]
    if not paths:
        print("no .txt or .csv export found", file=sys.stderr)
        return 2
    reports = [import_file(p, apply=args.apply, replace=args.replace, by=args.by) for p in paths]
    for report in reports:
        print(report.line())
        if report.unsupported:
            print("    no source yet: " + ", ".join(report.unsupported))
    if not args.apply:
        print(f"\nDRY RUN — nothing written. {len(reports)} lists, "
              f"{sum(r.counts['symbols'] for r in reports)} symbols. Re-run with --apply.")
    return 0


if __name__ == "__main__":                                   # pragma: no cover - the CLI entry point
    raise SystemExit(main())
