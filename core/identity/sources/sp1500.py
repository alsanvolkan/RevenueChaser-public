"""S&P 1500 GICS snapshot from Wikipedia, plus the diff gate that guards it.

The list is a sector source for its own names only (issue #272, restored after #265 removed it): ``core.identity.gics``
writes its ``sector`` through ``core.gics_map.canonical_sector`` (the 11 Morningstar names) and never takes its
sub-industry. The ``sub_industry`` stays on the cached row as raw data.

Wikipedia is an editable page, so a bad edit (or a layout change) must not rewrite the
sector of half the index. ``diff_gate`` compares a new snapshot with the stored one and
rejects it when more than 3% of the tickers present in both changed sector; the caller keeps
the previous snapshot and raises a conflict.

The three lists are S&P 500, 400 and 600. Base URL: ``RC_WIKIPEDIA_BASE`` (default
``https://en.wikipedia.org``). Tables are read with the standard library parser, so no lxml
dependency.

Ported from the Identity R1 research script (issue #84).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from ...providers import sec_edgar
from . import SourceResult, base_url, fail, norm_ticker, now_iso

SOURCE = "sp1500"
PAGES = {"sp500": "List_of_S%26P_500_companies",
         "sp400": "List_of_S%26P_400_companies",
         "sp600": "List_of_S%26P_600_companies"}
DIFF_THRESHOLD = 0.03
_FOOTNOTE = re.compile(r"\[[^\]]*\]")


class _Tables(HTMLParser):
    """Collects every top-level ``<table>`` as a list of rows of cell text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
        elif self._depth and tag == "tr":
            self._row = []
        elif self._depth and tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._depth = max(0, self._depth - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self.tables:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _col(header: list[str], *words: str) -> int | None:
    return next((i for i, h in enumerate(header) if all(w in h for w in words)), None)


def _parse(html: str, index: str) -> list[dict]:
    """Constituents of the first table that has Symbol, GICS Sector and GICS Sub-Industry columns."""
    parser = _Tables()
    parser.feed(html)
    for table in parser.tables:
        if not table:
            continue
        header = table[0]
        sym = next((i for i, h in enumerate(header) if "Symbol" in h or "Ticker" in h), None)
        sec, sub, name = _col(header, "GICS", "Sector"), _col(header, "Sub"), _col(header, "Security")
        if sym is None or sec is None or sub is None:
            continue
        rows = []
        for cells in table[1:]:
            if len(cells) <= max(sym, sec, sub):
                continue
            ticker = norm_ticker(_FOOTNOTE.sub("", cells[sym]))
            if not ticker:
                continue
            rows.append({"ticker": ticker, "sector": _FOOTNOTE.sub("", cells[sec]).strip(),
                         "sub_industry": _FOOTNOTE.sub("", cells[sub]).strip(), "index": index,
                         "name": cells[name] if name is not None and name < len(cells) else None})
        if rows:
            return rows
    raise ValueError(f"no GICS constituents table found for {index}")


def snapshot() -> SourceResult:
    """The three index lists: ``ticker``, ``sector``, ``sub_industry``, ``index``, ``name``.

    All three pages must parse, otherwise the result is an error (a missing index would look
    like a mass removal to the diff gate). A ticker on two lists keeps its first index.
    """
    base = base_url("WIKIPEDIA", "https://en.wikipedia.org")
    rows: dict[str, dict] = {}
    try:
        for index, page in PAGES.items():
            r = httpx.get(f"{base}/wiki/{page}", headers=sec_edgar._headers(), timeout=60,
                          follow_redirects=True)
            if r.status_code != 200:
                return fail(SOURCE, f"Wikipedia {index}: HTTP {r.status_code}")
            try:
                parsed = _parse(r.text, index)
            except ValueError as exc:
                return fail(SOURCE, str(exc))
            for row in parsed:
                rows.setdefault(row["ticker"], row)
        return SourceResult(SOURCE, "ok", list(rows.values()), None, now_iso())
    except Exception as exc:  # noqa: BLE001 - adapters never raise
        return fail(SOURCE, exc)


def diff_gate(previous_rows, new_rows, threshold: float = DIFF_THRESHOLD):
    """``(accept, changed_share, changed_rows)`` for a new snapshot against the stored one.

    Only tickers present in both count. Reject when the share whose sector changed is above
    ``threshold`` (3% by default). A first run (no previous snapshot) is accepted; an empty
    new snapshot over a real previous one is rejected.
    """
    prev = {r["ticker"]: r.get("sector") for r in previous_rows or []}
    new = {r["ticker"]: r.get("sector") for r in new_rows or []}
    if prev and not new:
        return False, 1.0, []
    common = [t for t in new if t in prev]
    changed = [{"ticker": t, "old_sector": prev[t], "new_sector": new[t]}
               for t in common if prev[t] != new[t]]
    share = round(len(changed) / len(common), 6) if common else 0.0
    return share <= threshold, share, changed
