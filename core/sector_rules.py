"""Sector comparability (Common C17, #383): which names the screeners cannot compare on their fundamentals.

Decided on Screeners card 4 (avo, 2026-10-10 22:41 Amsterdam): Financial Services and Real Estate are not
comparable (balance-sheet businesses; revenue, gross margin, FCF and Rule of 40 do not mean what they mean for an
operating company). The rule reads the **spine** sector (``equity_universe.sector`` through
``core.universe.spine_gics``), never a vendor's ``.info`` sector, so every board decides the same name the same way.

Helper only: Fundamentals excludes these names (F1) and Asymmetric Growth flags them (S6) in their own waves.
"""
from __future__ import annotations

from .gics_map import GicsPath, canonical_sector

COMPARABLE = "comparable"
NOT_COMPARABLE = "not_comparable"
NOT_COMPARABLE_SECTORS = frozenset({"Financial Services", "Real Estate"})


def sector_status(sector: str | None) -> str:
    """``NOT_COMPARABLE`` for a spine sector in ``NOT_COMPARABLE_SECTORS`` (older spellings normalised), else
    ``COMPARABLE``. An unknown sector is not excluded here."""
    return NOT_COMPARABLE if canonical_sector(sector) in NOT_COMPARABLE_SECTORS else COMPARABLE


def ticker_status(ticker: str, spine: dict[str, GicsPath] | None = None) -> str:
    """The status of one name from its spine sector; a name the spine has no row for is ``COMPARABLE``."""
    from .universe import gics_of, spine_gics

    t = (ticker or "").upper()
    return sector_status(gics_of(t, spine if spine is not None else spine_gics([t])).sector)


def not_comparable(tickers: list[str], spine: dict[str, GicsPath] | None = None) -> dict[str, str]:
    """``{ticker: spine sector}`` of the names that are not comparable (one spine read when ``spine`` is None)."""
    from .universe import spine_gics

    wanted = [(t or "").upper() for t in tickers]
    spine = spine if spine is not None else spine_gics(wanted)
    return {t: spine[t].sector for t in wanted
            if t in spine and sector_status(spine[t].sector) == NOT_COMPARABLE}


__all__ = ["COMPARABLE", "NOT_COMPARABLE", "NOT_COMPARABLE_SECTORS", "sector_status", "ticker_status",
           "not_comparable"]
