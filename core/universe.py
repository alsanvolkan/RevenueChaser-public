"""The shared classification universe — one ticker set for every board.

Co-movement, Value Chain and Relatedness must all read the *same* set so a ticker
present in one board is present in all (and `company_profile` can join them per
ticker). The single source of truth is `equity_universe`; this module is the only
place the membership rule lives.

The rule has two parts:

* **the floor** — `market_cap >= mc_min`, `mc_min` defaulting to $300M (the liquid
  slice used by the boards; pass a lower value, or 0, for the full universe);
* **the security type** (Identity R4, #87) — names the identity master
  (`security_master.security_type`) types as `identity.types.EXCLUDED_TYPES`
  (funds, ETFs, SPACs, preferreds, warrants, units) are not operating companies and
  leave the universe at every cap. A ticker with no master row, a NULL type, or a
  type that is not excluded (`unknown`, `other`, ...) stays: an untyped name is
  never hidden, only counted (`unknown_type`).

The types are read with a separate query, never a JOIN: `rawstore.query` answers
`[]` for a missing table, so a JOIN against an absent `security_master` would
silently empty the universe. `_security_types` is the one seam for them.

**The source** (build plan PR 5; the default since PR 6): ``RC_UNIVERSE_SOURCE`` unset (or empty, or ``spine``) reads
the identity spine through ``rcdata.read``: the members are the spine's listed names (Tier A and B, the old
membership rule), with the spine's name, sector, industry and security type, and the same two-part rule applied to
them; only the market fields (``market_cap``, ``price``, ``quote``) still come from the old ``equity_universe``, by
ticker, because the spine holds no market data yet. The index badges of ``filter_fields`` come from the spine's
current membership (``index_registry``). Any other value (``store`` by convention) reads ``raw.duckdb`` as above:
the escape hatch, kept until the old path is retired (plan PR 21). With the spine asked for, by default or by the
variable, but no spine file, or a last ``spine/security`` build that failed, the old store is read and a warning is
logged (``source_state``).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING

from . import rawstore
from .identity.types import EXCLUDED_TYPES, OPERATING_TYPES

if TYPE_CHECKING:
    from .gics_map import GicsPath

# The spine is `equity_universe`; the rename lands in docs/plan_rest.md.
_UNIVERSE_TABLE = ("equity_universe" if "equity_universe" in rawstore.MANIFEST
                   else "equity_universe")

DEFAULT_MC_MIN = 300_000_000.0

# The source switch (PR 5): read at call time, so a test or a restart with the variable set changes it.
SOURCE_ENV = "RC_UNIVERSE_SOURCE"
SPINE = "spine"
STORE = "store"          # the value that selects the old store (any value but ``spine`` does)
logger = logging.getLogger(__name__)

UNIVERSE_META_KEY = "__meta__"

# The excluded types, grouped into the funnel rows boards show for them:
# ``(count key, row label, types)``. Disjoint, and together exactly EXCLUDED_TYPES
# (a test pins it), so a new excluded type cannot be added without a funnel row.
TYPE_GROUPS = (
    ("funds_etfs", "Funds / ETFs", frozenset({"cef", "etp"})),
    ("spacs", "SPACs", frozenset({"spac"})),
    ("other_securities", "Preferred / warrants / units",
     frozenset({"preferred", "warrant", "unit"})),
)


_FALLBACK_LOGGED: set[tuple[str, str]] = set()   # (how the spine was asked for, why not), logged once


def source_state() -> dict:
    """``{requested, effective, reason, variable}`` of the universe source, read at call time. ``variable`` is
    ``RC_UNIVERSE_SOURCE`` as set (None when unset or empty). Unset or ``spine`` asks for the spine; any other value
    asks for the old store. The spine is honoured only while it is there and ``spine/security``'s newest check that is
    not a skip built it (changed or unchanged); otherwise the old store is read and ``reason`` says why (review
    should-fix 7)."""
    variable = (os.getenv(SOURCE_ENV) or "").strip() or None
    if variable is not None and variable.lower() != SPINE:
        return {"requested": STORE, "effective": STORE, "reason": None, "variable": variable}
    from ..rcdata import ops, serving
    reason = None
    if not serving.live_path(SPINE).exists():
        reason = "no spine.duckdb yet"
    else:
        last = ops.latest_unskipped_check("spine/security")
        if last is None:
            reason = "spine/security has not built yet"
        elif last["result"] not in ("changed", "unchanged"):
            reason = f"spine/security's last check {last['result']}"
    asked = (f"{SOURCE_ENV}={SPINE} is set" if variable
             else f"The universe reads the spine by default ({SOURCE_ENV} unset)")
    if reason and (asked, reason) not in _FALLBACK_LOGGED:
        _FALLBACK_LOGGED.add((asked, reason))
        logger.warning("%s, but the universe falls back to the old store: %s", asked, reason)
    return {"requested": SPINE, "effective": STORE if reason else SPINE, "reason": reason, "variable": variable}


def universe_source() -> str:
    """``"spine"`` when the spine is asked for (``RC_UNIVERSE_SOURCE`` unset or ``spine``) and built, else ``"store"``
    (``source_state``)."""
    return source_state()["effective"]


def _spine() -> bool:
    return universe_source() == SPINE


def spine_provenance() -> dict | None:
    """The spine build the boards read: ``{checked_on, result, source_as_of, rows}`` of ``spine/security``'s newest
    check that is not a skip (``result`` ``changed``: built that day; ``unchanged``: checked, nothing new), or None
    while the boards read the old store. ``source_as_of`` is the SEC file's day."""
    if not _spine():
        return None
    from ..rcdata import ops
    last = ops.latest_unskipped_check("spine/security") or {}
    return {k: last.get(k) for k in ("checked_on", "result", "source_as_of", "rows")}


def _spine_universe() -> list[dict]:
    """The spine's listed names as ``equity_universe`` rows: identity from ``rcdata.read``, market fields from the
    old ``equity_universe`` by ticker (NULL where it has none). No spine file yet: no rows."""
    from ..rcdata import read
    ids = read.universe(tiers=("A", "B"))
    if not ids:
        return []
    market = {r["ticker"]: r for r in rawstore.query(
        f"SELECT ticker, market_cap, price, quote FROM {_UNIVERSE_TABLE}")}
    out = []
    for r in ids:
        m = market.get(r["ticker"]) or {}
        out.append({"ticker": r["ticker"], "market_cap": m.get("market_cap"), "price": m.get("price"),
                    "quote": m.get("quote"), "name": r.get("name") or "", "sector": r.get("sector") or "",
                    "industry": r.get("industry") or "", "industry_group": None, "sub_industry": None})
    return out


def _by_cap(rows: list[dict], mc_min: float) -> list[dict]:
    """``COALESCE(market_cap, 0) >= mc_min ORDER BY market_cap DESC`` (NULLs last), as the store query does."""
    kept = [r for r in rows if (r.get("market_cap") or 0) >= mc_min]
    return sorted(kept, key=lambda r: (r.get("market_cap") is None, -(r.get("market_cap") or 0), r["ticker"]))


def _security_types() -> dict[str, str]:
    """``ticker -> security_type`` from the identity master; ``{}`` when it has none.

    A missing or empty ``security_master`` gives ``{}`` (no type filter). Every type
    decision below derives from this one function. On the spine, the spine's types.
    """
    if _spine():
        from ..rcdata import read
        rows = [r for r in read.identity() if r.get("ticker")]
        spine = {r["ticker"]: r["security_type"] for r in rows}
        # A spine name left untyped (no OpenFIGI answer, no Compustat link) takes the identity master's type, so the
        # flag does not let through an ETF the old path excluded (review should-fix 6). A type whose only source is
        # Compustat's tpci is a guess: the master's type wins over it unless the master says "common" (delta S4).
        weak = [r["ticker"] for r in rows if r["security_type"] in (None, "unknown", "other")
                or str(r.get("type_source") or "").startswith("compustat_tpci")]
        if weak:
            master = {r["ticker"]: r["security_type"] for r in rawstore.query(
                "SELECT ticker, security_type FROM security_master") if r.get("security_type")}
            for t in weak:
                m = master.get(t)
                if m and (spine[t] in (None, "unknown", "other") or m != "common"):
                    spine[t] = m
        return spine
    rows = rawstore.query("SELECT ticker, security_type FROM security_master")
    return {r["ticker"]: r["security_type"] for r in rows if r.get("ticker")}


def excluded_tickers() -> set[str]:
    """Tickers whose identity-master type is excluded, at any market cap."""
    return {t for t, ty in _security_types().items() if ty in EXCLUDED_TYPES}


def classification_universe(mc_min: float = DEFAULT_MC_MIN) -> list[str]:
    """Operating-company tickers in `equity_universe` with `market_cap >= mc_min`.

    Cap-descending. Names whose identity-master type is excluded
    (``identity.types.EXCLUDED_TYPES``) are dropped; untyped names stay.
    """
    if _spine():
        excluded = excluded_tickers()
        return [r["ticker"] for r in _by_cap(_spine_universe(), mc_min) if r["ticker"] not in excluded]
    rows = rawstore.query(
        f"SELECT ticker FROM {_UNIVERSE_TABLE} "
        f"WHERE COALESCE(market_cap, 0) >= ? ORDER BY market_cap DESC",
        [mc_min])
    excluded = excluded_tickers()
    return [r["ticker"] for r in rows if r["ticker"] not in excluded]


def classification_universe_rows(mc_min: float = DEFAULT_MC_MIN) -> list[dict]:
    """The ``classification_universe`` slice with the columns a board needs, not just the ticker.

    Same floor, same excluded-type rule and same cap-descending order as
    :func:`classification_universe`; one row per name: ``ticker``, ``market_cap``, ``sector``
    (the stored label, not normalised) and ``industry``. FIN-3's comps peer ladder is the reader.

    ``sub_industry`` is deliberately absent: the identity master writes it and ``industry_group``
    NULL (the GICS-only levels were dropped), so a ladder can never widen through them.
    """
    if _spine():
        rows = _by_cap(_spine_universe(), mc_min)
    else:
        rows = rawstore.query(
            f"SELECT ticker, market_cap, sector, industry FROM {_UNIVERSE_TABLE} "
            f"WHERE COALESCE(market_cap, 0) >= ? ORDER BY market_cap DESC",
            [mc_min])
    excluded = excluded_tickers()
    return [{"ticker": r["ticker"],
             "market_cap": r.get("market_cap"),
             "sector": r.get("sector") or "",
             "industry": r.get("industry") or ""}
            for r in rows if r["ticker"] not in excluded]


def universe_counts(mc_min: float = DEFAULT_MC_MIN) -> dict:
    """The universe funnel every board states: all caps, the floor slice, the types.

    ``scanned_total`` is the whole spine (the "Scanned Universe" number, the same
    on every board). A NULL market cap is counted apart, never as "below floor",
    so a missing cap is visible instead of silently shrinking the slice.

    Only names at or above the floor are split by type, so no name is counted
    twice::

        scanned_total  = below_floor + no_market_cap + floor_total
        floor_total    = funds_etfs + spacs + other_securities + classification_total

    ``classification_total`` is what the boards read
    (``len(classification_universe(mc_min))``, for ``mc_min > 0``: at 0 the universe
    also keeps NULL-cap names, which the counts have always kept apart in
    ``no_market_cap``). ``unknown_type`` counts, among the
    kept names, those whose type is not an operating type (no master row, NULL,
    ``unknown``, ``other``): kept, but their type is not established.
    """
    if _spine():
        spine = _spine_universe()
        caps = [x.get("market_cap") for x in spine]
        r = {"n": len(spine), "ok": sum(1 for c in caps if c is not None and c >= mc_min),
             "low": sum(1 for c in caps if c is not None and c < mc_min), "nocap": caps.count(None)}
        at_floor = [x for x in spine if x.get("market_cap") is not None and x["market_cap"] >= mc_min]
    else:
        rows = rawstore.query(
            f"SELECT count(*) n, "
            f"count(*) FILTER (WHERE market_cap >= ?) ok, "
            f"count(*) FILTER (WHERE market_cap < ?) low, "
            f"count(*) FILTER (WHERE market_cap IS NULL) nocap "
            f"FROM {_UNIVERSE_TABLE}", [mc_min, mc_min])
        r = rows[0] if rows else {}
        at_floor = rawstore.query(
            f"SELECT ticker FROM {_UNIVERSE_TABLE} WHERE market_cap >= ?", [mc_min])
    floor_total = int(r.get("ok") or 0)

    types = _security_types()
    group_n = {key: 0 for key, _label, _types in TYPE_GROUPS}
    unknown_type = 0
    for row in at_floor:
        ty = types.get(row["ticker"])
        group = next((key for key, _label, members in TYPE_GROUPS if ty in members), None)
        if group:
            group_n[group] += 1
        elif ty not in OPERATING_TYPES:
            unknown_type += 1
    return {"scanned_total": int(r.get("n") or 0),
            "floor_total": floor_total,
            **group_n,
            "classification_total": floor_total - sum(group_n.values()),
            "unknown_type": unknown_type,
            "below_floor": int(r.get("low") or 0),
            "no_market_cap": int(r.get("nocap") or 0),
            "floor": mc_min}


def load_universe_meta() -> dict:
    row = rawstore.get("equity_universe_provenance", UNIVERSE_META_KEY)
    if not row:
        return {}
    return {"generated_at": row.get("generated_at") or "",
            "refreshed_by": row.get("refreshed_by") or ""}


def load_cached_universe() -> list[dict]:
    rows = _spine_universe() if _spine() else rawstore.query("SELECT * FROM equity_universe")
    out = []
    for r in rows:
        rec = {
            "ticker": r["ticker"],
            "market_cap": r.get("market_cap"),
            "sector": r.get("sector") or "",
            "price": r.get("price"),
            "name": r.get("name") or "",
            "industry": r.get("industry") or "",
            "industry_group": r.get("industry_group") or "",
            "sub_industry": r.get("sub_industry") or "",
            "quote": r.get("quote"),
        }
        if isinstance(rec["quote"], str):
            try:
                rec["quote"] = json.loads(rec["quote"])
            except (json.JSONDecodeError, TypeError):
                rec["quote"] = {}
        out.append(rec)
    return out


_SUFFIX_RE = re.compile(r"\s+\(\d+\)$")
# The GICS sector spellings the Morningstar names replaced (#266); stored theme runs may still label a coarse theme so.
_LEGACY_GICS_SECTORS = frozenset({"information technology", "consumer discretionary", "consumer staples",
                                  "financials", "health care", "materials"})
# The id suffix of a sector coarse theme (``<run>:sec-<slug>``, ``module_relatedness.themes.sector_coarse_id``, #108):
# its coarse level IS the sectors, so ``theme_catalog`` keeps those names.
SECTOR_COARSE_PREFIX = "sec-"


def current_theme_run(strict: bool = False) -> str | None:
    """The id of the pinned (``is_current``) Relatedness theme run, or None (none pinned, or a fresh store).

    The one lookup (C23): ``module_relatedness.themes.current_run_id`` and the filter fields use it. A filter
    never breaks a board, so a failed read is None; ``strict=True`` (the Relatedness guards that refuse to
    rebuild or prune the current run) lets the error through instead of reading as "no current run"."""
    try:
        rows = rawstore.query("SELECT id FROM theme_runs WHERE is_current = 1 LIMIT 1")
    except Exception:  # noqa: BLE001 - table absent on a fresh store
        if strict:
            raise
        return None
    return rows[0]["id"] if rows else None


def theme_catalog(run: str | None = None) -> dict[str, dict]:
    """``theme_id -> {label, level, parent}`` for the pinned Relatedness run (or ``run``, when the caller
    already looked it up).

    The one Theme vocabulary for every board's filter (ADR-2); legacy keyword
    themes are not part of it (decision C-D1). Until the themes are regenerated
    (filters plan T1–T3), two display fixes stop the harm the user saw
    ("Financials (2)", "Health Care (4)"):

    * a coarse theme labelled with a sector name reads as its largest medium
      child instead — a theme that just repeats the Sector filter adds nothing.
      A run built with sector coarses (ids ``<run>:sec-<slug>``, #108 card
      "Sektörden coarse") keeps its sector names: there the coarse level is
      the sectors by design;
    * a " (n)" disambiguation suffix is dropped when the bare label is unique
      at its level.
    """
    from .gics_map import SECTORS_11, UNCLASSIFIED

    run = run or current_theme_run()
    if not run:
        return {}
    try:
        rows = rawstore.query(
            "SELECT theme_id, level, parent_id, label, size FROM relatedness_themes "
            "WHERE theme_id = ? OR starts_with(theme_id, ?)", [run, f"{run}:"])
    except Exception:  # noqa: BLE001 - optional control, never fatal
        return {}
    cat = {r["theme_id"]: {"label": (r.get("label") or "").strip(),
                           "level": r.get("level") or "",
                           "parent": r.get("parent_id") or "",
                           "size": int(r.get("size") or 0)} for r in rows}
    # A theme labelled like the sector unknown bucket repeats the Sector filter too: the legacy ``Other``
    # (stored runs) and the master's ``Unclassified``. A run built before the rename may still carry the GICS
    # spellings, so those count as sector names as well.
    sectors = ({s.lower() for s in SECTORS_11} | _LEGACY_GICS_SECTORS | {"other", UNCLASSIFIED.lower()})
    for tid, t in cat.items():
        if t["level"] != "coarse" or tid.startswith(f"{run}:{SECTOR_COARSE_PREFIX}"):
            continue
        if _SUFFIX_RE.sub("", t["label"]).lower() in sectors:
            kids = [c for c in cat.values() if c["parent"] == tid and c["level"] == "medium"]
            if kids:
                t["label"] = max(kids, key=lambda c: c["size"])["label"]
    for level in ("coarse", "medium", "fine"):
        same = [t for t in cat.values() if t["level"] == level]
        bare = [_SUFFIX_RE.sub("", t["label"]) for t in same]
        for t, b in zip(same, bare):
            if bare.count(b) == 1:
                t["label"] = b
    return {tid: {"label": t["label"], "level": t["level"], "parent": t["parent"]}
            for tid, t in cat.items()}


def _theme_ids_by_ticker(catalog: dict[str, dict], run: str | None = None) -> dict[str, list[str]]:
    """Ticker -> the ids of its fine themes and their medium and coarse parents."""
    run = run or current_theme_run()
    if not run or not catalog:
        return {}
    try:
        rows = rawstore.query(
            "SELECT ticker, theme_id FROM relatedness_theme_members "
            "WHERE theme_id = ? OR starts_with(theme_id, ?)", [run, f"{run}:"])
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, set[str]] = {}
    for r in rows:
        tid = r["theme_id"]
        while tid and tid in catalog:
            out.setdefault(r["ticker"], set()).add(tid)
            tid = catalog[tid]["parent"]
    return {t: sorted(ids) for t, ids in out.items()}


def spine_gics(tickers: list[str] | None = None) -> dict[str, GicsPath]:
    """Ticker -> the spine's classification (sector, industry), from ``equity_universe``.

    ``equity_universe`` is what ``security_master`` writes since R3 (#86); this is the one reader the
    screeners use for a sector, never the vendor ``.info`` sector (R5, #88). The sector is a Morningstar name
    (an older GICS spelling is normalised); a blank or legacy ``Other`` sector or industry reads
    ``Unclassified``. A missing table gives ``{}``.
    """
    from .gics_map import GicsPath, UNCLASSIFIED, canonical_sector, is_unclassified

    sql = f"SELECT ticker, sector, industry FROM {_UNIVERSE_TABLE}"
    params: list = []
    if tickers is not None:
        # named tickers are filtered in SQL: per-ticker callers must not scan the whole universe
        wanted = sorted({(t or "").upper() for t in tickers})
        if not wanted:
            return {}
        sql += f" WHERE ticker IN ({', '.join('?' * len(wanted))})"
        params = wanted
    if _spine():
        from ..rcdata import read
        listed = ("A", "B")
        found = read.identity(params) if tickers is not None else read.universe(tiers=listed)
        rows = [r for r in found if r.get("tier") in listed]
    else:
        rows = rawstore.query(sql, params)
    out: dict[str, GicsPath] = {}
    for r in rows:
        t = r["ticker"]
        industry = (r.get("industry") or "").strip()
        out[t] = GicsPath(canonical_sector(r.get("sector")),
                          UNCLASSIFIED if not industry or is_unclassified(industry) else industry)
    return out


def gics_of(ticker: str, spine: dict[str, GicsPath]) -> GicsPath:
    """The spine classification of one ticker; a name the spine has no row for is ``Unclassified`` (no vendor guess)."""
    from .gics_map import GicsPath, UNCLASSIFIED

    return spine.get((ticker or "").upper()) or GicsPath(UNCLASSIFIED, UNCLASSIFIED)


def filter_fields(tickers: list[str] | None = None) -> dict[str, dict]:
    """Per-ticker fields the universal dashboard filters need (ADR-2 contract).

    Joins ``equity_universe`` (name/sector/industry/market_cap) with ``ticker_scores``
    (earnings_date), the index membership via ``annotate_cards`` (``index_registry``: the spine or
    the old store) and the pinned theme run. Every row carries ``gics`` (the filter path: sector,
    then industry when there is one) and ``theme_ids`` (all levels; empty = unthemed).
    Tickers not on the spine still get a row so a picker never drops a known
    name.
    """
    from .gics_map import UNCLASSIFIED, canonical_sector, sector_path
    from .index_registry import annotate_cards

    if _spine():
        rows = _spine_universe()
    else:
        rows = rawstore.query(
            f"SELECT ticker, name, sector, industry, market_cap "
            f"FROM {_UNIVERSE_TABLE}")
    by = {r["ticker"]: r for r in rows}
    try:
        earnings = {r["ticker"]: r["v"] for r in rawstore.query(
            "SELECT ticker, earnings_date v FROM ticker_scores")}
    except Exception:  # noqa: BLE001 - table may be absent on a fresh store
        earnings = {}
    wanted = {(t or "").upper() for t in tickers} if tickers is not None else None
    cards: list[dict] = []
    seen: set[str] = set()
    for t, r in by.items():
        if wanted is not None and t not in wanted:
            continue
        seen.add(t)
        cards.append({
            "ticker": t,
            "name": r.get("name") or t,
            "sector": canonical_sector(r.get("sector")),
            "industry": (r.get("industry") or "").strip(),
            "market_cap": r.get("market_cap"),
            "earnings_date": earnings.get(t),
        })
    if wanted:
        for t in sorted(wanted - seen):
            cards.append({
                "ticker": t, "name": t, "sector": UNCLASSIFIED, "industry": "",
                "market_cap": None, "earnings_date": earnings.get(t),
            })
    run = current_theme_run()
    themes = _theme_ids_by_ticker(theme_catalog(run), run) if run else {}
    for c in cards:
        c["gics"] = sector_path(c["sector"], c["industry"])
        c["theme_ids"] = themes.get(c["ticker"], [])
    annotate_cards(cards)
    return {c["ticker"]: c for c in cards}


__all__ = ["classification_universe", "classification_universe_rows", "spine_gics", "gics_of",
           "DEFAULT_MC_MIN", "TYPE_GROUPS", "excluded_tickers", "filter_fields", "theme_catalog",
           "current_theme_run", "universe_counts", "universe_source", "spine_provenance", "SOURCE_ENV", "SPINE",
           "STORE"]
