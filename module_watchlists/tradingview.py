"""TradingView's watch-list text format, read and written (#786).

A TradingView export is **one line**: items separated by commas, where an item beginning with ``###`` is a section
heading and everything else is a symbol. avo's own nine exports show both spellings of a heading, with and without
a space after the hashes::

    ###INDEX FEATURES,CAPITALCOM:SPX500,...,###MACRO MARKET WATCH,TVC:VIX,...
    ### AI ÇIPLERI — GPU VE ACCELERATOR,NASDAQ:CRDO,...

and both are read. A file may open with symbols before any heading (``country etf.txt``, ``6 - Space Defense.txt``
carry no heading at all), and those symbols go into the list's default section.

What this module guarantees, because the export is how avo gets his lists back into TradingView:

- **Order is kept.** Sections in the order they appear, symbols in the order inside their section.
- **Spelling is kept.** A symbol goes back out exactly as it came in (``parse`` upper-cases and trims, nothing else).
- **A round trip is lossless** for order, headings and symbols: ``dumps(parse_export(text))`` returns ``text`` up to
  whitespace around items and the heading spelling. A trailing empty heading (avo's ``index.txt`` ends with
  ``###SECTION 1`` and no symbols) survives as an empty section.

Nothing here touches the store; ``module_watchlists/importer.py`` is what writes.
"""
from __future__ import annotations

from typing import NamedTuple

from . import symbols as symbols_mod

HEADING_PREFIX = "###"


class Section(NamedTuple):
    """One heading and the symbols under it. ``name`` is empty for the symbols before the first heading."""

    name: str
    symbols: list[str]


def parse_export(text: str) -> list[Section]:
    """Read a TradingView export into ordered sections. An empty text is no sections."""
    sections: list[Section] = []
    current = Section("", [])
    for raw_item in (text or "").replace("\r", "\n").replace("\n", ",").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if item.startswith(HEADING_PREFIX):
            name = item[len(HEADING_PREFIX):].strip()
            if current.name or current.symbols:
                sections.append(current)
            current = Section(name, [])
            continue
        current.symbols.append(symbols_mod.parse(item).raw)
    if current.name or current.symbols:
        sections.append(current)
    return sections


def dumps(sections: list[Section]) -> str:
    """Write ordered sections back out in TradingView's one-line format. A section with an empty name contributes
    its symbols alone, with no heading, which is how a list that never had headings goes back out."""
    items: list[str] = []
    for section in sections:
        if section.name:
            items.append(f"{HEADING_PREFIX}{section.name}")
        items.extend(section.symbols)
    return ",".join(items)


def counts(sections: list[Section]) -> dict[str, int]:
    """What an import is about to write: how many sections, how many symbols, and how many of each coverage."""
    out = {"sections": len(sections), "symbols": 0}
    for section in sections:
        for symbol in section.symbols:
            out["symbols"] += 1
            key = symbols_mod.parse(symbol).coverage
            out[key] = out.get(key, 0) + 1
    return out
