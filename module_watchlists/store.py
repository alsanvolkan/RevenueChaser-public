"""The watch lists' own file, ``boards/watchlists.duckdb`` (#786; avo, 12 Nov 2026: "listeleri tutmak icin bir
database ihtiyacin var, duckdb onerim").

The one module that writes the file. Four tables (``core/rawstore.py`` MANIFEST, ``"db": "watchlists"``):

- ``wl_list``: one row per list — its name, avo's free-text description, where it sits in the list of lists, and
  ``feeds``, which job group it serves (empty for now; the jobs still read ``ops.sqlite``'s two lists).
- ``wl_section``: the ordered headings inside a list (avo's own, e.g. "AI ÇIPLERI — GPU VE ACCELERATOR").
  A list with no headings has one section whose name is empty, so a reader never special-cases "no sections".
- ``wl_item``: one row per symbol on a list, in its section, at its position, with avo's spelling kept
  exactly (``module_watchlists/symbols.py``).
- ``wl_change``: one row per add or remove, so a list that loses a name can say when and by whom.

The names are ``wl_*`` and not ``watchlist*`` because the old store already owns ``watchlist`` and
``watchlist_changes`` (the pre-PR-4 tables, unwritten until their retirement).

**This file is not a board result.** Nothing recomputes it: it is what avo typed, and losing it loses the lists.
So no function here writes a table whole, every write is one list, one section or one symbol, and the file goes
into the ``boards`` step of the weekly backup with the board files (``rcdata/backup.py``).

**Order is data.** A list's sections and a section's symbols come back in avo's order, not alphabetically, because
the order is how he reads them. ``position`` is dense within its parent and is rewritten on a move.

Ids are slugs made from the name (``derive_id``), so an exported list keeps a stable id across a re-import; a
clash gets a numeric suffix.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata

from RevenueChaser.core import rawstore

from . import symbols as symbols_mod

logger = logging.getLogger(__name__)

DB = "watchlists"
LISTS = "wl_list"
SECTIONS = "wl_section"
ITEMS = "wl_item"
CHANGES = "wl_change"

#: The section every list has when avo gave no headings: one row, empty name, so a reader always walks sections.
DEFAULT_SECTION = "_"

MAX_NAME = 200
MAX_DESCRIPTION = 4000


class WatchlistError(Exception):
    """A watch-list write that cannot be made: an unknown list, a bad name, a section of another list."""


# ── reads ────────────────────────────────────────────────────────────────────

def _query(sql: str, params: list | None = None) -> list[dict]:
    """A read of the file; a file or a table that is not there yet is no rows, never an error — the first start
    of a dashboard on a machine with no lists must render an empty page, not a stack trace."""
    try:
        return rawstore.query(sql, params, db=DB)
    except Exception as exc:                      # noqa: BLE001 - a missing table is an empty file
        logger.debug("watch-list read: %s", exc)
        return []


def lists() -> list[dict]:
    """Every list, in avo's order, each with its symbol count."""
    return _query(
        "SELECT l.list_id, l.name, l.description, l.position, l.feeds, l.created_at, l.updated_at, "
        "       count(i.symbol) AS symbol_count "
        f"FROM {LISTS} l LEFT JOIN {ITEMS} i ON i.list_id = l.list_id "
        "GROUP BY l.list_id, l.name, l.description, l.position, l.feeds, l.created_at, l.updated_at "
        "ORDER BY l.position, l.name")


def get_list(list_id: str) -> dict | None:
    rows = _query(f"SELECT * FROM {LISTS} WHERE list_id = ?", [list_id])
    return rows[0] if rows else None


def sections(list_id: str) -> list[dict]:
    return _query(f"SELECT section_id, name, position FROM {SECTIONS} WHERE list_id = ? ORDER BY position, name",
                  [list_id])


def items(list_id: str, section_id: str | None = None) -> list[dict]:
    """The symbols of a list, in avo's order; of one section when ``section_id`` is given."""
    # ``position`` counts inside a section, so the list's own order is the section's place first: ordering by
    # ``position`` alone put the second symbol of every section before the third of the first one.
    sql = (f"SELECT i.symbol, i.section_id, i.position, i.exchange, i.ticker, i.coverage, i.note, "
           f"i.added_at, i.added_by FROM {ITEMS} i "
           f"LEFT JOIN {SECTIONS} s ON s.list_id = i.list_id AND s.section_id = i.section_id "
           f"WHERE i.list_id = ?")
    params: list = [list_id]
    if section_id is not None:
        sql += " AND i.section_id = ?"
        params.append(section_id)
    return _query(sql + " ORDER BY s.position, i.position, i.symbol", params)


def read_list(list_id: str) -> dict | None:
    """One list whole: its row, its sections in order, and each section's symbols in order. This is what the
    board renders and what the export writes."""
    row = get_list(list_id)
    if row is None:
        return None
    by_section: dict[str, list[dict]] = {}
    for item in items(list_id):
        by_section.setdefault(item["section_id"], []).append(item)
    row["sections"] = [dict(s, items=by_section.get(s["section_id"], [])) for s in sections(list_id)]
    return row


def symbols(list_id: str) -> list[str]:
    """Just the symbols of a list, in order — what a job or an export asks for."""
    return [r["symbol"] for r in items(list_id)]


def coverage_counts(list_id: str | None = None) -> dict[str, int]:
    """How many symbols of a list (or of every list) fall in each coverage bucket. The Settings card and the
    board's header read this to say "64 symbols have no source yet" without walking the rows."""
    sql = f"SELECT coverage, count(*) AS n FROM {ITEMS}"
    params: list = []
    if list_id is not None:
        sql += " WHERE list_id = ?"
        params.append(list_id)
    rows = _query(sql + " GROUP BY coverage", params)
    return {r["coverage"]: int(r["n"]) for r in rows}


def changes(list_id: str | None = None, limit: int = 200) -> list[dict]:
    sql = f"SELECT list_id, symbol, action, changed_at, changed_by, note FROM {CHANGES}"
    params: list = []
    if list_id is not None:
        sql += " WHERE list_id = ?"
        params.append(list_id)
    return _query(sql + " ORDER BY changed_at DESC LIMIT ?", params + [int(limit)])


# ── names and ids ────────────────────────────────────────────────────────────

def _clean_name(name: str, *, what: str = "name") -> str:
    text = " ".join((name or "").split())
    if not text:
        raise WatchlistError(f"a watch list {what} cannot be empty")
    if len(text) > MAX_NAME:
        raise WatchlistError(f"a watch list {what} is at most {MAX_NAME} characters")
    return text


def derive_id(name: str, taken: set[str] | None = None) -> str:
    """A slug for ``name``, unique against ``taken``. Turkish letters keep their sound (ı -> i, ş -> s), because a
    list called "1- Büyüme Motoru" should read as ``1-buyume-motoru`` and not lose half its letters."""
    folded = (name or "").strip().lower()
    for src, dst in (("ı", "i"), ("İ", "i"), ("ş", "s"), ("ğ", "g"), ("ü", "u"), ("ö", "o"), ("ç", "c")):
        folded = folded.replace(src, dst)
    folded = unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-") or "list"
    slug = slug[:60].strip("-") or "list"
    if not taken or slug not in taken:
        return slug
    for n in range(2, 1000):
        candidate = f"{slug}-{n}"
        if candidate not in taken:
            return candidate
    raise WatchlistError(f"cannot make a free id from {name!r}")


def _taken_list_ids() -> set[str]:
    return {r["list_id"] for r in _query(f"SELECT list_id FROM {LISTS}")}


def _next_position(table: str, where: str, params: list) -> int:
    rows = _query(f"SELECT coalesce(max(position), -1) AS p FROM {table} WHERE {where}", params)
    return int(rows[0]["p"]) + 1 if rows else 0


# ── writes ───────────────────────────────────────────────────────────────────

def create_list(name: str, *, description: str = "", by: str = "avo", feeds: str = "",
                list_id: str | None = None) -> str:
    """Make a list and its default section. Returns the list's id. A name already taken is an error, not a second
    list of the same name: two lists called "Energy" cannot be told apart in a menu."""
    name = _clean_name(name)
    description = (description or "").strip()[:MAX_DESCRIPTION]
    taken = _taken_list_ids()
    if any(r["name"] == name for r in _query(f"SELECT name FROM {LISTS}")):
        raise WatchlistError(f"a watch list called {name!r} is already there")
    list_id = (list_id or derive_id(name, taken)).strip()
    if not list_id:
        raise WatchlistError("a watch list id cannot be empty")
    if list_id in taken:
        raise WatchlistError(f"the watch list id {list_id!r} is already there")
    now = time.time()
    rawstore.write_rows(LISTS, [{
        "list_id": list_id, "name": name, "description": description,
        "position": _next_position(LISTS, "1 = 1", []), "feeds": feeds or "",
        "created_at": now, "created_by": by, "updated_at": now,
    }])
    rawstore.write_rows(SECTIONS, [{
        "list_id": list_id, "section_id": DEFAULT_SECTION, "name": "", "position": 0, "created_at": now,
    }])
    return list_id


def update_list(list_id: str, *, name: str | None = None, description: str | None = None,
                feeds: str | None = None) -> None:
    """Rename a list, give it a description, or say which job group it feeds. Only what is passed changes."""
    if get_list(list_id) is None:
        raise WatchlistError(f"no watch list {list_id!r}")
    sets, params = [], []
    if name is not None:
        name = _clean_name(name)
        clash = _query(f"SELECT list_id FROM {LISTS} WHERE name = ? AND list_id <> ?", [name, list_id])
        if clash:
            raise WatchlistError(f"a watch list called {name!r} is already there")
        sets.append("name = ?")
        params.append(name)
    if description is not None:
        sets.append("description = ?")
        params.append((description or "").strip()[:MAX_DESCRIPTION])
    if feeds is not None:
        sets.append("feeds = ?")
        params.append(feeds or "")
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(time.time())
    rawstore.execute(f"UPDATE {LISTS} SET {', '.join(sets)} WHERE list_id = ?", params + [list_id], db=DB)


def add_section(list_id: str, name: str, *, section_id: str | None = None, position: int | None = None) -> str:
    """Add a heading to a list, at the end unless ``position`` says otherwise. A name the list already carries
    returns that section instead of making a second one with the same heading."""
    if get_list(list_id) is None:
        raise WatchlistError(f"no watch list {list_id!r}")
    name = _clean_name(name, what="section name")
    existing = _query(f"SELECT section_id FROM {SECTIONS} WHERE list_id = ? AND name = ?", [list_id, name])
    if existing:
        return existing[0]["section_id"]
    taken = {r["section_id"] for r in sections(list_id)}
    section_id = (section_id or derive_id(name, taken)).strip() or derive_id(name, taken)
    if section_id in taken:
        raise WatchlistError(f"the section id {section_id!r} is already on {list_id!r}")
    rawstore.write_rows(SECTIONS, [{
        "list_id": list_id, "section_id": section_id, "name": name,
        "position": _next_position(SECTIONS, "list_id = ?", [list_id]) if position is None else int(position),
        "created_at": time.time(),
    }])
    return section_id


def rename_section(list_id: str, section_id: str, name: str) -> None:
    """Give one heading another name. The id stays as it is, so the symbols under it do not move and an export
    keeps the same id for a heading avo only re-worded."""
    if not _query(f"SELECT 1 AS x FROM {SECTIONS} WHERE list_id = ? AND section_id = ?", [list_id, section_id]):
        raise WatchlistError(f"no section {section_id!r} on watch list {list_id!r}")
    name = _clean_name(name, what="section name")
    clash = _query(f"SELECT section_id FROM {SECTIONS} WHERE list_id = ? AND name = ? AND section_id <> ?",
                   [list_id, name, section_id])
    if clash:
        raise WatchlistError(f"the watch list {list_id!r} already has a section called {name!r}")
    rawstore.execute(f"UPDATE {SECTIONS} SET name = ? WHERE list_id = ? AND section_id = ?",
                     [name, list_id, section_id], db=DB)
    _touch(list_id)


def delete_section(list_id: str, section_id: str) -> int:
    """Remove one heading and move its symbols to the list's first remaining section, at the end of it.

    The symbols are kept, never deleted with the heading: a list is avo's and a heading is only how it is read.
    Returns how many symbols moved. The last section of a list cannot go, because every symbol lives under one.
    """
    rows = sections(list_id)
    if not any(s["section_id"] == section_id for s in rows):
        raise WatchlistError(f"no section {section_id!r} on watch list {list_id!r}")
    rest = [s for s in rows if s["section_id"] != section_id]
    if not rest:
        raise WatchlistError("a watch list keeps at least one section")
    into = rest[0]["section_id"]
    moved = [r["symbol"] for r in items(list_id, section_id)]
    at = _next_position(ITEMS, "list_id = ? AND section_id = ?", [list_id, into])
    with rawstore.transaction(db=DB) as tx:
        for step, symbol in enumerate(moved):
            tx.execute(f"UPDATE {ITEMS} SET section_id = ?, position = ? WHERE list_id = ? AND symbol = ?",
                       [into, at + step, list_id, symbol])
        tx.execute(f"DELETE FROM {SECTIONS} WHERE list_id = ? AND section_id = ?", [list_id, section_id])
        for position, section in enumerate(rest):
            tx.execute(f"UPDATE {SECTIONS} SET position = ? WHERE list_id = ? AND section_id = ?",
                       [position, list_id, section["section_id"]])
    _touch(list_id)
    return len(moved)


def reorder_sections(list_id: str, ordered: list[str]) -> None:
    """Give a list's sections the order of ``ordered``; a section not named keeps its place after them."""
    current = [s["section_id"] for s in sections(list_id)]
    wanted = [s for s in ordered if s in current]
    rest = [s for s in current if s not in wanted]
    with rawstore.transaction(db=DB) as tx:
        for at, section_id in enumerate(wanted + rest):
            tx.execute(f"UPDATE {SECTIONS} SET position = ? WHERE list_id = ? AND section_id = ?",
                       [at, list_id, section_id])
    _touch(list_id)


def add_symbol(list_id: str, symbol: str, *, section_id: str = DEFAULT_SECTION, by: str = "avo",
               note: str | None = None, position: int | None = None) -> bool:
    """Put one symbol on a list. False when it was already on that list — a second add is not an error, because a
    menu that ticks a box twice should do nothing, not fail.

    The symbol is stored as avo wrote it; ``coverage`` is read from the spelling (``symbols.parse``), so an
    unsupported one (#785) is kept with the rest and simply shows no data.
    """
    if get_list(list_id) is None:
        raise WatchlistError(f"no watch list {list_id!r}")
    parsed = symbols_mod.parse(symbol)
    if not parsed.raw:
        raise WatchlistError("a watch-list symbol cannot be empty")
    known = {s["section_id"] for s in sections(list_id)}
    if section_id not in known:
        if section_id != DEFAULT_SECTION:
            raise WatchlistError(f"no section {section_id!r} on watch list {list_id!r}")
        rawstore.write_rows(SECTIONS, [{"list_id": list_id, "section_id": DEFAULT_SECTION, "name": "",
                                        "position": 0, "created_at": time.time()}])
    if _query(f"SELECT 1 AS x FROM {ITEMS} WHERE list_id = ? AND symbol = ?", [list_id, parsed.raw]):
        return False
    now = time.time()
    rawstore.write_rows(ITEMS, [{
        "list_id": list_id, "symbol": parsed.raw, "section_id": section_id,
        "position": (_next_position(ITEMS, "list_id = ? AND section_id = ?", [list_id, section_id])
                     if position is None else int(position)),
        "exchange": parsed.exchange, "ticker": parsed.ticker, "coverage": parsed.coverage,
        "note": note, "added_at": now, "added_by": by,
    }])
    _record(list_id, parsed.raw, "add", by, note)
    _touch(list_id)
    return True


def remove_symbol(list_id: str, symbol: str, *, by: str = "avo") -> bool:
    """Take one symbol off a list. False when it was not there."""
    parsed = symbols_mod.parse(symbol)
    if not _query(f"SELECT 1 AS x FROM {ITEMS} WHERE list_id = ? AND symbol = ?", [list_id, parsed.raw]):
        return False
    rawstore.execute(f"DELETE FROM {ITEMS} WHERE list_id = ? AND symbol = ?", [list_id, parsed.raw], db=DB)
    _record(list_id, parsed.raw, "remove", by, None)
    _touch(list_id)
    return True


def delete_list(list_id: str, *, by: str = "avo") -> bool:
    """Remove a list, its sections and its symbols. The change rows are kept, so what the list held is still
    readable afterwards. False when there is no such list."""
    if get_list(list_id) is None:
        return False
    held = symbols(list_id)
    with rawstore.transaction(db=DB) as tx:
        tx.execute(f"DELETE FROM {ITEMS} WHERE list_id = ?", [list_id])
        tx.execute(f"DELETE FROM {SECTIONS} WHERE list_id = ?", [list_id])
        tx.execute(f"DELETE FROM {LISTS} WHERE list_id = ?", [list_id])
    for symbol in held:
        _record(list_id, symbol, "remove", by, "list deleted")
    return True


def move_symbol(list_id: str, symbol: str, *, section_id: str, position: int | None = None) -> None:
    """Move one symbol to another section of the same list, or to another place in its own."""
    parsed = symbols_mod.parse(symbol)
    if not _query(f"SELECT 1 AS x FROM {ITEMS} WHERE list_id = ? AND symbol = ?", [list_id, parsed.raw]):
        raise WatchlistError(f"{parsed.raw!r} is not on watch list {list_id!r}")
    if section_id not in {s["section_id"] for s in sections(list_id)}:
        raise WatchlistError(f"no section {section_id!r} on watch list {list_id!r}")
    at = (_next_position(ITEMS, "list_id = ? AND section_id = ?", [list_id, section_id])
          if position is None else int(position))
    rawstore.execute(f"UPDATE {ITEMS} SET section_id = ?, position = ? WHERE list_id = ? AND symbol = ?",
                     [section_id, at, list_id, parsed.raw], db=DB)
    _touch(list_id)


def reorder(list_id: str, section_id: str, ordered: list[str]) -> None:
    """Give a section's symbols the order of ``ordered``; any symbol of the section not named keeps its place
    after them, in its old order."""
    current = [r["symbol"] for r in items(list_id, section_id)]
    wanted = [s for s in (symbols_mod.parse(x).raw for x in ordered) if s in current]
    rest = [s for s in current if s not in wanted]
    with rawstore.transaction(db=DB) as tx:
        for at, symbol in enumerate(wanted + rest):
            tx.execute(f"UPDATE {ITEMS} SET position = ? WHERE list_id = ? AND symbol = ?", [at, list_id, symbol])
    _touch(list_id)


# ── bookkeeping ──────────────────────────────────────────────────────────────

def _record(list_id: str, symbol: str, action: str, by: str, note: str | None) -> None:
    rawstore.write_rows(CHANGES, [{
        "list_id": list_id, "symbol": symbol, "action": action, "changed_at": time.time(), "changed_by": by, "note": note,
    }])


def _touch(list_id: str) -> None:
    rawstore.execute(f"UPDATE {LISTS} SET updated_at = ? WHERE list_id = ?", [time.time(), list_id], db=DB)
