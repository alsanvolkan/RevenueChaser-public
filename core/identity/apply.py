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
