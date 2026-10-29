"""The SEC 13F board's one reader of the ownership store (G9, build plan PR 17): ``ownership.duckdb`` through
``rcdata.read``, which opens the serving file read-only for the call and closes it again.

The board keeps the shape it always rendered (``report.board_entries``): ``{ticker: {period: cell}}``, the period an
ISO quarter end and the cell the old ``sec13f_totals`` metric keys. Each cell also carries what the served row says
about itself: ``complete`` (the quarter's filing deadline has passed, New York today), ``filing_deadline``,
``filed_through`` (the newest filing the quarter holds), ``prev_period`` (the calendar quarter the flows and
``breadth_chg`` compare with), ``exited_confidential`` / ``exited_no_filing`` (exits that may not be sales) and the
row's ``source`` and ``fetched_at``.

- ``totals``: every ticker's quarters (``rcdata.read.holders_13f``).
- ``periods``: one row per quarter (``rcdata.read.periods_13f``): filers, value, the share of value named a ticker.
- ``meta``: name and sector per ticker from the spine (``rcdata.read.identity``), the names the filter bar uses.
- ``short_interest``: FINRA's newest consolidated short interest per ticker, with days to cover and its
  settlement date (``rcdata.read.finra_short_interest``).
- ``builder_request``: the Fetch's queued build of ``ownership/serve`` (D1: a board only puts a request in the queue).

No serving file yet means no rows, never an exception and never a provider call.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import ops, read

logger = logging.getLogger(__name__)

SOURCE = "rcdata:ownership"
SERVE = "ownership/serve"
SHORT = "finra/short_interest"
REQUESTED_BY = "sec_13f"
# The metric keys of a cell (the old sec13f_totals entry, rcdata/build/ownership/serve.py METRICS) and what the
# served row adds.
METRICS = ("shares", "value", "holders", "top10_pct", "hhi", "new_holders", "exited_holders", "buy_flow",
           "sell_flow", "breadth", "breadth_chg")
EXTRA = ("complete", "filing_deadline", "filed_through", "prev_period", "exited_confidential", "exited_no_filing",
         "filers", "source", "fetched_at")
# FINRA's short interest is read this far back: two settlement dates a month, so one month holds the newest.
SHORT_LOOKBACK_DAYS = 45


def _iso(value) -> str | None:
    return None if value is None else str(value)[:10]


def totals(tickers=None) -> dict[str, dict[str, dict]]:
    """``{ticker: {period: cell}}`` of ``tickers`` (every 13F ticker the ownership store serves when None); empty
    before the first derive."""
    out: dict[str, dict[str, dict]] = {}
    for r in read.holders_13f(tickers):
        cell = {k: r.get(k) for k in METRICS}
        for k in EXTRA:
            v = r.get(k)
            cell[k] = _iso(v) if k in ("filing_deadline", "filed_through", "prev_period") else v
        cell["status"] = r.get("status")
        out.setdefault(r["ticker"], {})[_iso(r["period"])] = cell
    return out


def periods() -> list[dict]:
    """``sec13f_periods`` rows with ``complete``, oldest first."""
    return read.periods_13f()


def newest_complete(rows: list[dict]) -> dict | None:
    """The newest quarter of ``periods()`` whose filing deadline has passed, or None."""
    done = [r for r in rows if r.get("complete")]
    return done[-1] if done else None


def meta(tickers) -> dict[str, dict]:
    """``{ticker: {"name", "sector"}}`` from the spine's ``security`` rows (any tier); a ticker the spine does not
    hold is left out, so the board shows its ticker alone and "Unclassified"."""
    wanted = [t for t in tickers if t]
    if not wanted:
        return {}
    return {r["ticker"]: {"name": r.get("name") or "", "sector": r.get("sector")} for r in read.identity(wanted)}


def short_interest(tickers, today: date | None = None) -> dict[str, dict]:
    """``{ticker: {"short_interest", "days_to_cover", "short_as_of", "avg_daily_volume"}}``: the newest FINRA
    settlement date of each ticker in the last ``SHORT_LOOKBACK_DAYS`` days. A ticker FINRA does not report (or the
    store has not fetched) is left out."""
    wanted = [t for t in tickers if t]
    if not wanted:
        return {}
    start = (today or calendar.today_ny()) - timedelta(days=SHORT_LOOKBACK_DAYS)
    out: dict[str, dict] = {}
    for r in read.finra_short_interest(wanted, start=start):     # ordered by ticker, settlement date
        if r.get("current_short") is None:
            continue
        out[r["ticker"]] = {"short_interest": float(r["current_short"]), "days_to_cover": r.get("days_to_cover"),
                            "short_as_of": r.get("settlement_date"), "avg_daily_volume": r.get("avg_daily_volume")}
    return out


def coverage_note(rows: list[dict]) -> str | None:
    """One line on how much of the newest complete quarter's 13F value is named a ticker (the CUSIPs left out of
    the totals, #77): read from the store, so a Refresh says what the Fetch said."""
    p = newest_complete(rows)
    if p is None or not p.get("value_total"):
        return None
    left = (p.get("value_total") or 0.0) - (p.get("value_mapped") or 0.0)
    cov = p.get("coverage")
    cov_text = f"{cov * 100:.1f}%" if cov is not None else "an unknown share"
    return (f"13F {_quarter_label(p['period'])}: {cov_text} of the ${(p.get('value_total') or 0) / 1e12:.2f}T "
            f"reported value is named a ticker; {int(p.get('unmapped_cusips') or 0):,} CUSIPs "
            f"(${left / 1e9:,.1f}B) are not, and are left out of the totals.")


def _quarter_label(period) -> str:
    d = period if isinstance(period, date) else date.fromisoformat(str(period)[:10])
    return f"{d.year} Q{(d.month - 1) // 3 + 1}"


def as_of_line(rows: list[dict], cells: dict[str, dict[str, dict]]) -> str | None:
    """Where the numbers come from and how old they are (#118): the store, the newest quarter and whether it is
    still filing, the newest filing it holds and when the derive ran."""
    if not rows:
        return None
    newest = rows[-1]
    fetched = [c.get("fetched_at") for per in cells.values() for c in per.values() if c.get("fetched_at")]
    built = ""
    if fetched:
        from datetime import datetime, timezone
        built = (" · derived " + datetime.fromtimestamp(max(fetched), tz=timezone.utc)
                 .astimezone(calendar.NY).strftime("%Y-%m-%d %H:%M") + " New York")
    state = ("complete" if newest.get("complete")
             else f"still filing until {_iso(newest.get('filing_deadline'))}")
    return (f"Source: SEC Form 13F through the ownership store ({SERVE}) · newest quarter "
            f"{_quarter_label(newest['period'])}, {state} · filings through {_iso(newest.get('filed_through'))}"
            f"{built}")


def builder_request() -> dict:
    """Queue one ``ownership/serve`` (the derive from the stored filings, no network) unless one is queued or
    running. Before its first run (the server thread's, after a backup: the dataset ``starts_by_hand``) nothing is
    queued, so a Fetch can never be the store's first write. A locked or broken ``ops.sqlite`` is a note, never a
    failed Fetch."""
    from RevenueChaser.core.calendar import now_ny
    try:
        if not ops.ever_built(SERVE):
            state = "not queued: it has not had its first run yet (the server thread starts it after a backup)"
        else:
            state = "queued" if ops.enqueue_once(SERVE, None, by=REQUESTED_BY) is not None else "already queued"
    except Exception as exc:  # noqa: BLE001 - the board renders what is stored either way
        logger.warning("sec 13f: builder request for %s not queued: %s", SERVE, exc)
        state = f"not queued ({type(exc).__name__}: {exc})"[:200]
    return {"requested_at": f"{now_ny():%Y-%m-%d %H:%M} New York", "datasets": {SERVE: state}}
