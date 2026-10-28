"""Index membership for the boards — the one place a board asks ``ticker -> index``.

The badges on the cards (``annotate_cards``, ``index_for``), the index list of the shared filter
(``filter_labels``) and ``load_index`` read the identity spine through ``rcdata.read`` (build plan PR 6):

  - ``sp500``, ``sp400``, ``sp600``, ``ndx``  the spine's current members, voted from the index funds' daily
    holdings files (``funds/index_holdings``; ``rcdata.read.membership``)
  - ``nasdaq``                               the spine's listed names (Tier A and B) on Nasdaq, as the spine's
    ``index_constituents`` bridge view has it

No source the spine reads carries the Dow Jones, so on the spine no card carries it and the filter does not offer
it. Each slug with a label in ``LABELS`` shows under that label; a slug without one (``sp400``, ``ndx`` today) is
read by ``load_index`` but shows no badge, so the filter is unchanged by the move.

``RC_UNIVERSE_SOURCE`` set to anything but ``spine`` (or a spine that is missing or whose last build failed, see
``universe.source_state``) reads the old store instead: the ``index_constituents`` table the weekly Unusual Whales
step writes (``core.index_lists``). That module, not this one, fetches and writes; this one only reads.
"""
from __future__ import annotations

# slug -> display label for every stored slug (#214). Display order matters: the badge shows the first match,
# and the filter lists the labels in this order. A SmallCap 600 member listed on Nasdaq reads "S&P SmallCap 600".
LABELS: dict[str, str] = {
    "sp500": "S&P 500",
    "dow": "Dow Jones",
    "sp600": "S&P SmallCap 600",
    "nasdaq": "Nasdaq Composite",
}
# The filter's short text for a label whose full name misleads (Nasdaq Composite ~ listed on the exchange).
SHORT_LABELS: dict[str, str] = {"Nasdaq Composite": "Nasdaq-listed"}

# The slug of the listed-on-Nasdaq names, which the spine derives from the exchange rather than reads from a fund file.
NASDAQ = "nasdaq"


def _spine() -> bool:
    from .universe import SPINE, universe_source
    return universe_source() == SPINE


def _spine_members(listed: list[dict] | None = None) -> dict[str, set[str]]:
    """``{slug: tickers}`` on the spine: each index's current members, and ``nasdaq`` from the listed names."""
    from . import spine_reads
    out: dict[str, set[str]] = {}
    for m in spine_reads.membership():
        out.setdefault(m["index_slug"], set()).add(str(m["ticker"]).upper())
    nasdaq = _listed_on_nasdaq(spine_reads.universe(("A", "B")) if listed is None else listed)
    if nasdaq:
        out[NASDAQ] = nasdaq
    return out


def _listed_on_nasdaq(listed: list[dict]) -> set[str]:
    return {str(r["ticker"]).upper() for r in listed if r.get("exchange") == "Nasdaq"}


def load_index(slug: str) -> set[str] | None:
    """The current members of ``slug`` (upper-cased tickers), or None when it has none.

    On the spine: the fund files' current members (``nasdaq``: the listed Nasdaq names); ``dow`` is always None
    there. On the old store: its ``index_constituents`` rows (``index_lists.stored_index``)."""
    if not _spine():
        from .index_lists import stored_index
        return stored_index(slug)
    from . import spine_reads
    if slug == NASDAQ:
        tickers = _listed_on_nasdaq(spine_reads.universe(("A", "B")))
    else:
        tickers = {str(m["ticker"]).upper() for m in spine_reads.membership(slug)}
    return tickers or None


def _index_memberships(indexes: dict[str, set[str]]) -> dict[str, list[str]]:
    """Invert ``{slug -> tickers}`` into ``{ticker -> [labels]}`` (display order)."""
    memberships: dict[str, list[str]] = {}
    for slug, label in LABELS.items():
        for t in indexes.get(slug, set()):
            memberships.setdefault(t, []).append(label)
    return memberships


def load_registry() -> dict | None:
    """``{ticker: {index: [labels], name, sector}}`` of every ticker with at least one labelled index, the labels in
    ``LABELS`` order; None when there is none. On the spine the name and sector are the spine's; on the old store they
    come from its ``index_tickers`` VIEW (``index_lists.stored_registry``)."""
    if not _spine():
        from .index_lists import stored_registry
        return stored_registry()
    from . import spine_reads
    listed = spine_reads.universe(("A", "B"))
    by = {str(r["ticker"]).upper(): r for r in listed}
    members = _spine_members(listed)
    out: dict[str, dict] = {}
    for slug, label in LABELS.items():
        for t in sorted(members.get(slug, ())):
            row = by.get(t) or {}
            out.setdefault(t, {"index": [], "name": row.get("name") or "",
                               "sector": row.get("sector") or ""})["index"].append(label)
    return out or None


def filter_labels() -> list[str]:
    """The index labels the shared filter offers, in display order (K1, #214).

    On the spine, a label is listed only while some name carries it, so the filter never offers an index no card can
    carry (the Dow Jones, which the spine has no source for). On the old store, a ``FIRST_FILL_GATED`` slug is listed
    only once it has rows and the other slugs always are, as before."""
    if _spine():
        have = set(_spine_members())
        return [lbl for slug, lbl in LABELS.items() if slug in have]
    from .index_lists import FIRST_FILL_GATED, stored_slugs
    have = stored_slugs()
    return [lbl for slug, lbl in LABELS.items() if slug not in FIRST_FILL_GATED or slug in have]


def index_for(ticker: str, registry: dict | None = None) -> list[str]:
    reg = registry if registry is not None else (load_registry() or {})
    return list(reg.get((ticker or "").upper(), {}).get("index", []))


def annotate_cards(cards: list[dict], registry: dict | None = None) -> list[dict]:
    """Set ``card["index"]`` (list of labels) for each card, cache-first."""
    reg = registry if registry is not None else (load_registry() or {})
    for c in cards:
        c["index"] = list(reg.get((c.get("ticker") or "").upper(), {}).get("index", []))
    return cards


def _main(argv: list[str]) -> int:
    """``--refresh`` runs the weekly step (``scripts/index_lists.py --weekly``: lock, floors, run id and provider on
    the change rows); without it the stored registry is only read."""
    if "--refresh" in argv:
        from RevenueChaser.scripts import index_lists
        return index_lists.main(["--weekly"])
    reg = load_registry() or {}
    print(f"registry: {len(reg)} tickers ({'the identity spine' if _spine() else 'the old store, index_tickers'})")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
