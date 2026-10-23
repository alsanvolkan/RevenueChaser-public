"""Apply ``security_master`` to ``equity_universe`` (issue #86, R3; reworked in #265): the master is the single
classification writer.

Two writers used to set the classification in ``equity_universe``: the universe fetch (``_save_universe``, from
yfinance / Unusual Whales strings) and the SIC backfill. Both are gone as sector sources. The identity master
(``security_master``, built by ``core.identity.refresh``) holds the one answer per ticker with its source; this
module copies it onto the universe::

    security_master.sector    -> equity_universe.sector      (UW's Morningstar name)
    security_master.industry  -> equity_universe.industry    (the provider's industry text)
    -                         -> equity_universe.industry_group, sub_industry   (written NULL: GICS-only levels,
                                                              the columns stay so there is no schema bump)
    last good uw_sector row   -> equity_universe.next_earnings_date   (UW's date; raw stays in the source cache)

No other column is touched: cap, price, name and quote are never written here.

The rules:

* a master level that is ``Unclassified`` stays ``Unclassified`` (it replaces a vendor value too: the master is
  the single writer);
* a universe ticker with no master row gets **no entry** and keeps its stored (vendor-derived) values, never
  blanked; so does a ticker whose master row has no sector at all (a blank is not an answer);
* an empty or missing ``security_master`` writes nothing;
* the earnings date comes from the last good ``uw_sector`` cache row (a failed UW run never overwrites the cache,
  so its old date stays). A ticker UW has no row for keeps the date it already has; a UW row without a date
  clears it;
* only rows whose values differ are written, in chunks, with ``rawstore.update_columns`` (one transaction per
  chunk, key + the five columns: a concurrent market-cap refresh is never overwritten).

``plan_apply`` is pure (dicts in, dicts out), so the dry-run evidence script can compute the would-be table
from a read-only connection; ``apply_to_universe`` is the IO wrapper the identity refresh and the universe
fetch call.
"""
from __future__ import annotations

from .. import rawstore
from ..gics_map import UNCLASSIFIED, is_unclassified
from . import cache

COLUMNS = ("sector", "industry", "industry_group", "sub_industry", "next_earnings_date")
CHUNK = 2000          # rows per ``update_columns`` call (one transaction each)
_UNIVERSE = "equity_universe"
_UW_SOURCE = "uw_sector"
# #127: identifiers copied from the CUSIP map we already hold. Not a provider call.
ID_COLS = ("cusip", "isin", "cusip_source", "isin_source")
CUSIP_SOURCE = "openfigi_13f"
ISIN_SOURCE = "derived_cusip"
_EMPTY_IDS = dict.fromkeys(ID_COLS)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _level(value) -> str:
    """A master level as the universe stores it: blank and the legacy ``Other`` read ``Unclassified``."""
    value = _text(value)
    return UNCLASSIFIED if not value or is_unclassified(value) else value


def _usable(master_rows) -> dict[str, dict]:
    """``{ticker: master row}`` for rows that carry an answer (a ticker and a sector)."""
    return {r["ticker"]: r for r in master_rows if r.get("ticker") and _text(r.get("sector"))}


def plan_apply(master_rows: list[dict], universe_rows: list[dict], earnings: dict | None = None) -> list[dict]:
    """The rows to write: ``{ticker, sector, industry, industry_group, sub_industry, next_earnings_date}`` for each
    universe ticker that has a usable master row and whose stored values differ from the would-be ones.

    ``earnings`` maps a ticker to UW's next earnings date (``None`` = UW has a row without a date); a ticker
    that is not in it keeps its stored date. Pure: no store access.
    """
    master = _usable(master_rows)
    earnings = earnings or {}
    plan: list[dict] = []
    for u in universe_rows:
        ticker = u.get("ticker")
        m = master.get(ticker)
        if m is None:
            continue
        new = {"sector": _level(m.get("sector")), "industry": _level(m.get("industry")),
               "industry_group": None, "sub_industry": None,
               "next_earnings_date": earnings[ticker] if ticker in earnings else u.get("next_earnings_date")}
        if any(u.get(c) != new[c] for c in COLUMNS):
            plan.append({"ticker": ticker, **new})
    return plan


def _uw_earnings() -> dict[str, str | None]:
    """``{ticker: next earnings date or None}`` from the last good UW rows."""
    return {r["ticker"]: _text(r.get("next_earnings_date")) or None for r in cache.last_good(_UW_SOURCE)}


def apply_to_universe() -> dict:
    """Write the master's classification and UW's earnings date onto ``equity_universe``. Returns
    ``{applied, unchanged, not_in_master, master_rows}`` (plus ``skipped`` with the reason when nothing could be
    applied). Store-only, no network."""
    cols = ", ".join(("ticker", *COLUMNS))
    master_rows = rawstore.query("SELECT ticker, sector, industry FROM security_master")
    universe_rows = rawstore.query(f"SELECT {cols} FROM {_UNIVERSE}")
    usable = _usable(master_rows)
    known = sum(1 for u in universe_rows if u.get("ticker") in usable)
    counts = {"applied": 0, "unchanged": known, "not_in_master": len(universe_rows) - known,
              "master_rows": len(usable)}
    if not usable:
        counts["skipped"] = "security_master is empty or missing: nothing applied"
        return counts
    plan = plan_apply(master_rows, universe_rows, _uw_earnings())
    for i in range(0, len(plan), CHUNK):
        rawstore.update_columns(_UNIVERSE, "ticker", plan[i:i + CHUNK], list(COLUMNS))
    counts["applied"], counts["unchanged"] = len(plan), known - len(plan)
    return counts


# ── CUSIP / ISIN onto the spine (#127) ───────────────────────────────────────
# ``identity_cusip.current_map`` is keyed by CUSIP. The live store was 1:1 (measured 23 Oct 2026)
# (14,656 CUSIPs, 14,656 tickers); the table does not forbid two present CUSIPs
# naming one ticker (an old and a new CUSIP both still 'present', or two share
# classes OpenFIGI both spell as the same ticker). A single column cannot hold
# both. Tie, deterministic: newest ``valid_from``, then the lowest CUSIP. A
# missing ``valid_from`` (a run outside the holiday table) loses to any date.


def _ticker(raw) -> str:
    """The spine's spelling: upper-case, ``BRK/B`` and ``BRK.B`` both ``BRK-B``."""
    from .sources import norm_ticker
    return norm_ticker(str(raw or "").replace("/", "-"))


def _iso_date(value) -> str:
    if value is None or value == "":
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _best(items: list[tuple[str, dict]]) -> tuple[str, dict]:
    """The one CUSIP a ticker keeps: the newest ``valid_from`` wins (a real date beats none), then the lowest CUSIP.

    The date is picked first and the lowest CUSIP among the survivors second, so a CUSIP of any characters can
    never raise (an earlier sort key built from ``chr(255 - ord(c))`` aborted the identity run on a character
    above U+00FF, and the offending row stays in the append-only ``identity_cusip``).
    """
    dated = [(c, i) for c, i in items if _iso_date(i.get("valid_from")) != ""]
    pool = dated or items
    if dated:
        newest = max(_iso_date(i.get("valid_from")) for _c, i in pool)
        pool = [(c, i) for c, i in pool if _iso_date(i.get("valid_from")) == newest]
    return min(pool, key=lambda item: item[0])


def ticker_identifiers(cmap: dict[str, dict]) -> dict[str, dict]:
    """``{ticker: {cusip, isin, cusip_source, isin_source}}`` from a ``current_map``-shaped dict.

    Each entry may carry ``valid_from`` (the tie). A CUSIP that is not a US CUSIP
    (a CINS, a bad check digit) is still stored; its ISIN stays NULL. Pure.
    """
    from .isin import from_cusip
    groups: dict[str, list[tuple[str, dict]]] = {}
    for cusip, info in cmap.items():
        ticker = _ticker((info or {}).get("ticker"))
        cusip = str(cusip or "").strip().upper()
        if not ticker or not cusip:
            continue
        groups.setdefault(ticker, []).append((cusip, info or {}))
    out: dict[str, dict] = {}
    for ticker, items in groups.items():
        cusip, _info = _best(items)
        isin = from_cusip(cusip)
        out[ticker] = {"cusip": cusip, "isin": isin, "cusip_source": CUSIP_SOURCE,
                       "isin_source": ISIN_SOURCE if isin else None}
    return out


def _map_with_valid_from() -> dict[str, dict] | None:
    """The current CUSIP map enriched with ``valid_from``, or None when it is empty.

    An empty map is not "every ticker lost its CUSIP": the apply writes nothing, so a
    missing table or a map that has not been built yet cannot blank the spine.
    """
    from .. import identity_cusip
    cmap = identity_cusip.current_map()
    if not cmap:
        return None
    dated = {r["cusip"]: r["valid_from"] for r in rawstore.query(
        "SELECT cusip, valid_from FROM identity_cusip "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY cusip ORDER BY run_at DESC) = 1")}
    return {cusip: {**info, "valid_from": dated.get(cusip)} for cusip, info in cmap.items()}


_LOAD = object()


def load_map() -> dict[str, dict] | None:
    """The map the apply steps use; read it once per identity run and pass it to both steps."""
    return _map_with_valid_from()


def stamp_rows(rows: list[dict], cmap=_LOAD) -> list[dict]:
    """The identifier columns on master rows about to be written. Empty map: rows unchanged."""
    if cmap is _LOAD:
        cmap = _map_with_valid_from()
    if cmap is None:
        return rows
    want = ticker_identifiers(cmap)
    return [{**row, **want.get(_ticker(row.get("ticker")), _EMPTY_IDS)} for row in rows]


def apply_identifiers(cmap=_LOAD) -> dict:
    """Write ``cusip`` / ``isin`` onto ``security_master`` from ``identity_cusip.current_map``.

    Inverted by ticker (tie: newest ``valid_from``, then lowest CUSIP). ``cusip_source``
    is ``openfigi_13f``; ``isin`` is derived (ISO 6166) and ``isin_source`` is
    ``derived_cusip``. A ticker the map does not name is cleared. An empty map writes
    nothing. No provider call. Returns ``{cusip_applied, cusip_unchanged, cusip_mapped}``
    (plus ``cusip_skipped`` when the map is empty).
    """
    if cmap is _LOAD:
        cmap = _map_with_valid_from()
    if cmap is None:
        return {"cusip_applied": 0, "cusip_unchanged": 0, "cusip_mapped": 0,
                "cusip_skipped": "identity_cusip has no present rows: nothing applied"}
    want = ticker_identifiers(cmap)
    stored = rawstore.query(f"SELECT ticker, {', '.join(ID_COLS)} FROM security_master")
    updates = []
    for row in stored:
        new = want.get(_ticker(row.get("ticker")), _EMPTY_IDS)
        if any(row.get(col) != new[col] for col in ID_COLS):
            updates.append({"ticker": row["ticker"], **new})
    for i in range(0, len(updates), CHUNK):
        rawstore.update_columns("security_master", "ticker", updates[i:i + CHUNK], list(ID_COLS))
    return {"cusip_applied": len(updates), "cusip_unchanged": len(stored) - len(updates),
            "cusip_mapped": len(want)}
