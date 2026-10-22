"""Comps peers from the spine universe (#385 FIN-3) — no static list.

The ladder, one rung at a time, inside ``core.universe.classification_universe_rows`` (the same floor and the
same excluded-security-type rule every other board reads):

1. the spine's ``industry``, nearest market cap first;
2. widening to the spine's ``sector`` until there are ``k`` peers.

``sub_industry`` is not a rung: the identity master writes it (and ``industry_group``) NULL, so a name has
nothing to match on there. A target the universe does not carry (no market cap to measure "nearest" from), or
one whose industry and sector are exhausted, gets fewer peers than ``k`` or none — the caller says so in a
warning rather than substituting a list of its own.
"""
from __future__ import annotations

from .config import PEER_K
from RevenueChaser.core.gics_map import UNCLASSIFIED, canonical_sector, is_unclassified


def _industry_key(row: dict) -> str:
    """The rung a name joins, ``""`` when it has no usable industry (never "all the blanks match")."""
    industry = (row.get("industry") or "").strip()
    return "" if not industry or is_unclassified(industry) else industry


def _nearest(target_cap: float, rows: list[dict], limit: int) -> list[dict]:
    """``limit`` rows at the closest market cap to ``target_cap``; a name with no cap goes last.

    Ties keep the universe's own order (cap-descending), so a run is reproducible.
    """
    ranked = sorted(rows, key=lambda r: (r.get("market_cap") is None,
                                         abs((r.get("market_cap") or 0.0) - target_cap)))
    return ranked[:limit]


def target_row(ticker: str, rows: list[dict]) -> dict | None:
    """The universe row the ladder starts from: ``ticker`` with a market cap, else None.

    The precondition of the whole ladder: a name the spine does not carry (below the market-cap floor, or an
    excluded security type) or one with no cap has no "nearest market cap" to rank peers from, so it gets no
    peer at all — and the caller says that, rather than reporting an exhausted industry.
    """
    wanted = (ticker or "").strip().upper()
    row = next((r for r in rows if r["ticker"] == wanted), None)
    return row if row is not None and row.get("market_cap") is not None else None


def select_peers(ticker: str, rows: list[dict], k: int = PEER_K) -> list[str]:
    """The ``k`` peer tickers of ``ticker`` from the universe ``rows``; the target is never one of them."""
    wanted = (ticker or "").strip().upper()
    target = target_row(ticker, rows)
    if target is None:
        return []
    candidates = [r for r in rows if r["ticker"] != wanted]

    picked: list[dict] = []
    industry = _industry_key(target)
    if industry:
        picked = _nearest(target["market_cap"], [r for r in candidates if _industry_key(r) == industry], k)

    sector = canonical_sector(target.get("sector"))
    if len(picked) < k and sector != UNCLASSIFIED:
        taken = {r["ticker"] for r in picked}
        picked += _nearest(target["market_cap"],
                           [r for r in candidates
                            if r["ticker"] not in taken and canonical_sector(r.get("sector")) == sector],
                           k - len(picked))
    return [r["ticker"] for r in picked]
