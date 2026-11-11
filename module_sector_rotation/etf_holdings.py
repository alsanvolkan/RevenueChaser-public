"""Sector Rotation's ETF holdings: the spine's whole lists (``funds/etf_holdings``), read through ``rcdata.read``.

The board fetches no holdings itself any more (#642 PR 5): the UW and yfinance fallback chain and the "holdings"
job that ran it are gone, and the spine's build is the one writer.
"""
from __future__ import annotations

import logging

from RevenueChaser.core import datahealth
from RevenueChaser.core.datahealth import Health

logger = logging.getLogger(__name__)


def synced_etfs() -> list[str]:
    """The ETFs the board shows holdings for: every sector and theme ETF of the Rotation universe."""
    from .universe import ROTATION_ETFS
    return [t for t, info in ROTATION_ETFS.items() if info.get("group") in ("sector", "theme")]


def stored_holdings(ticker: str) -> list[dict]:
    """An ETF's holdings (``ticker``, ``name``, ``weight``), largest first, from the spine's whole lists
    (``rcdata.read.etf_holdings``, its newest day; #642 PR 4: the holdings job's ``raw.etf_holdings`` is no longer
    read). A line with no US ticker is left out; ``[]`` when the spine holds none (no network)."""
    from RevenueChaser.rcdata import read
    return [{"ticker": r["ticker"], "name": r.get("name") or "", "weight": r.get("weight")}
            for r in read.etf_holdings([ticker]) if r.get("ticker")]


@datahealth.scoped
def health_summary() -> dict:
    """What Data Health says about the board's holdings: the spine's lists, no network.

    ``lines`` counts the lines read; ``newest_session`` is the newest ``as_of`` among them, ``in_newest`` the ETFs
    on it; ``providers`` the provider of every ETF's list; ``missing`` the board's ETFs with no line in the spine;
    ``rs_stored`` the theme ETFs with stored RS rows (``rotation_holdings``, which the Rotation Fetch writes)."""
    from RevenueChaser.core import rawstore
    from RevenueChaser.rcdata import read
    from .universe import ROTATION_ETFS
    expected = synced_etfs()
    lines = read.etf_holdings(expected)
    rows = {r["etf"]: r for r in lines}                 # one per ETF: all of an ETF's lines share as_of and provider
    sessions = {t: r["as_of"] for t, r in rows.items() if r.get("as_of") is not None}
    newest = max(sessions.values()) if sessions else None
    providers: dict[str, int] = {}
    for r in rows.values():
        if r.get("provider"):
            providers[r["provider"]] = providers.get(r["provider"], 0) + 1
    themes = [t for t, info in ROTATION_ETFS.items() if info.get("group") == "theme"]
    with_rs = {r["ticker"] for r in rawstore.query(
        "SELECT ticker FROM rotation_holdings WHERE rows IS NOT NULL AND json_array_length(rows) > 0", db="sector_rotation")}
    return {
        "expected": len(expected),
        "lines": len(lines),
        "stored": sum(1 for t in expected if t in rows),
        "newest_session": newest.isoformat() if newest else None,
        "in_newest": sum(1 for t in expected if newest is not None and sessions.get(t) == newest),
        "providers": providers,
        "missing": sorted(t for t in expected if t not in rows),
        "themes": len(themes),
        "rs_stored": sum(1 for t in themes if t in with_rs),
    }


def health() -> Health:
    """The numbers :func:`health_summary` reports, typed (#124 S4).

    ``covered`` is the ETFs with stored holdings against the ``expected`` sector and theme ETFs;
    ``source_share`` is the provider of each stored list as a share of the stored lists (a list stored without a
    provider is in no numerator, so the shares can sum below 1). ``newest`` is the newest spine day;
    the summary reads no calendar, so it has no ``expected_as_of``.
    """
    n = health_summary()
    stored = n["stored"]
    return Health(
        covered=stored,
        expected=n["expected"],
        newest=n["newest_session"],
        expected_as_of=None,
        source_share={p: c / stored for p, c in n["providers"].items()} if stored else None,
    )
