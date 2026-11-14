"""The "add to list" menu every other board carries (#786 step 5).

One button beside a ticker opens a menu of every watch list: avo's own named lists (``boards/watchlists.duckdb``)
and the two lists the jobs read (``ops.sqlite``, shown on the Watch lists board as ``jobs.option`` and
``jobs.stock``). Ticking a row puts the ticker on that list, unticking takes it off.

A board knows a plain ticker (``NVDA``); avo's lists hold TradingView spellings (``NASDAQ:NVDA``). So:

- a ticker is **on** one of avo's lists when a US listing of that ticker is on it, whatever exchange prefix it
  was typed with (``store.items`` keeps the ticker part of every symbol);
- **adding** writes the spine's own spelling (``identity.security``'s exchange, the same mapping the Watch lists
  add box uses), so the row gets its numbers on the Watch lists board at once. It lands in the list's unnamed top
  section (``store.DEFAULT_SECTION``), the "not sorted yet" place: a menu on another board cannot know which of
  avo's headings a name belongs under, and a wrong heading is worse than none. He drags it into place there.
  A ticker the spine does not know is refused rather than stored as a symbol with no source;
- **removing** takes off every US listing of that ticker on the list, so an untick never leaves the box ticked.

The two job lists keep their own rules: a bare ticker, written through ``rcdata.ops``, exactly as the old
Stock / Option pair did. What the options build, LEAPS and Earnings read is unchanged.
"""
from __future__ import annotations

from RevenueChaser.module_watchlists import board, store, symbols
from RevenueChaser.rcdata import ops, read

#: The menu's two groups, in the order it draws them.
OWN, JOBS = "Your lists", "Read by the jobs"


class MenuError(Exception):
    """A tick the menu cannot make: an unknown list, or a ticker the spine cannot place on an exchange."""


def _own_tickers(list_id: str) -> list[str]:
    """The US tickers on one of avo's lists, in the list's order, each once."""
    seen: dict[str, None] = {}
    for item in store.items(list_id):
        if item.get("coverage") == "us_equity" and item.get("ticker"):
            seen.setdefault(str(item["ticker"]).upper(), None)
    return list(seen)


def lists() -> list[dict]:
    """Every list the menu offers, avo's first: ``{id, name, group, job, count, tickers}``.

    ``count`` is the list's whole size (what the Watch lists board's picker shows); ``tickers`` only the US
    listings, the ones a board's ticker can match.
    """
    out = [{"id": row["list_id"], "name": row["name"], "group": OWN, "job": False,
            "count": int(row.get("symbol_count") or 0), "tickers": _own_tickers(row["list_id"])}
           for row in store.lists()]
    both = read.watchlists()
    for list_id, spec in board.JOB_LISTS.items():
        tickers = [r["ticker"] for r in both.get(spec["kind"]) or []]
        out.append({"id": list_id, "name": spec["name"], "group": JOBS, "job": True,
                    "count": len(tickers), "tickers": tickers})
    return out


def spelling(ticker: str) -> str | None:
    """``NASDAQ:NVDA`` for ``NVDA``: the spine's exchange in the Watch lists' own spelling. None when the spine
    does not know the ticker, or lists it somewhere that is not a US exchange this board reads."""
    t = (ticker or "").strip().upper()
    for row in read.identity([t]):
        raw = str(row.get("exchange") or "").upper().strip()
        exchange = board._EXCHANGE_SPELLING.get(raw, raw)
        if exchange in symbols.US_EXCHANGES:
            return f"{exchange}:{t}"
    return None


def is_on(list_id: str, ticker: str) -> bool:
    t = (ticker or "").strip().upper()
    kind = board.job_kind(list_id)
    if kind:
        return t in read.watched(kind)
    return t in _own_tickers(list_id)


def set_on(list_id: str, ticker: str, on: bool, *, by: str) -> bool:
    """Put ``ticker`` on ``list_id`` (``on``) or take it off; whether it is on the list afterwards.

    Idempotent both ways: a tick on a ticker already there, or an untick on one that is not, writes nothing.
    """
    t = (ticker or "").strip().upper()
    kind = board.job_kind(list_id)
    if kind:
        (ops.watch_add if on else ops.watch_remove)(kind, t, by=by)
        return on
    if store.get_list(list_id) is None:
        raise MenuError(f"no watch list {list_id!r}")
    if on:
        if t in _own_tickers(list_id):
            return True
        symbol = spelling(t)
        if symbol is None:
            raise MenuError(f"{t} is not a US listing the spine knows")
        store.add_symbol(list_id, symbol, section_id=store.DEFAULT_SECTION, by=by)
        return True
    for item in store.items(list_id):
        if item.get("coverage") == "us_equity" and str(item.get("ticker") or "").upper() == t:
            store.remove_symbol(list_id, item["symbol"], by=by)
    return False
