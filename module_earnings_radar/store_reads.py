"""The Earnings board's one reader of the market store: the market-wide series it scores against.

The same shape as ``module_spx_gamma/store_reads.py`` (PR 3), and for the same reason: each reader is a thin mapping
over ``rcdata.read``, which opens the serving file read-only for the one call and closes it again. Nothing here keeps
a connection, runs SQL of its own, or calls a provider. A series the store does not hold comes back as a "not stored"
marker — never an exception, and never a live read.

Each function is named after the need the internal data API will serve it as, so the API later wraps this module
rather than the board:

- ``dgs10_map`` (``macro.dgs10``): the 10-year Treasury yield observations rule R6 pairs its reaction returns with.
  The series is ``market_series``'s own FRED row (``core.rotation_reference.MACRO_SERIES["US10Y"]["series"]``), the
  same row the Rotation board's macro map reads, so the two boards cannot disagree about a yield.
- ``estimates`` (``fundamentals.estimates``): the current-quarter EPS trend and revision counts rule R1 reads, from
  the fundamentals store's ``estimate_latest`` (``rcdata/build/fundamentals/estimates.py``, the same D14 value
  columns). The old nightly made one Yahoo request per ticker for these; the fundamentals build fetches them now.

Before this module the board read the nightly ``macro_series`` table of ``raw.duckdb`` and called FRED itself when
that table was empty or stale (``stored_inputs.dgs10_map`` and ``macro_watch.fred_daily_map``). The market build
(``rcdata/build/market_series.py``) already fetches the same FRED series, so both of those are gone.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from RevenueChaser.core import calendar
from RevenueChaser.core.rotation_reference import MACRO_SERIES
from RevenueChaser.rcdata import read

logger = logging.getLogger(__name__)

SERIES_SOURCE = "rcdata:market_series"
ESTIMATES_SOURCE = "rcdata:estimate_latest"
# Sessions an observation may lag the board's day by before it is not read. The limit the old stored reader had
# (``stored_inputs.MAX_LAG["macro_series"]``): FRED posts a day late, so a normal night reads 1 or 2, and 3 covers one
# missed night. Past that the series would silently pair fewer and fewer recent dates, so it is "not stored" instead.
MAX_LAG = {"dgs10": 3}
# The newest observations the board pairs (R6): the window the old reader asked for.
DGS10_LIMIT = 80
# Calendar days read back to find ``DGS10_LIMIT`` observations: 80 sessions is about 16 weeks, doubled for holidays
# and the days FRED has no value for.
_LOOKBACK_DAYS = 240
DGS10_SERIES = tuple(MACRO_SERIES["US10Y"]["series"])
# The dataset name the fundamentals estimates are checked under (``rcdata/build/fundamentals/estimates.NAME``, "yfinance/estimates").
ESTIMATES_DATASET = "yfinance/estimates"


def _day(value) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _sessions_after(obs: date, day: date) -> int | None:
    """NYSE sessions after ``obs`` up to and including ``day`` (0 when ``obs >= day``); None past the calendar."""
    n, d = 0, obs
    while n <= 40:
        try:
            d = calendar.next_trading_day(d)
        except ValueError:
            return None
        if d > day:
            return n
        n += 1
    return None


def _missing(what: str, day, reason: str | None = None, source: str = SERIES_SOURCE,
             status: str | None = None) -> dict:
    """The "not stored" marker every reader here returns instead of raising or reading live."""
    label = f"{what} not stored" + (f" for {_iso(day)}" if day is not None else "") + \
            (f" ({reason})" if reason else "")
    return {"stored": False, "label": label, "source": source, "source_as_of": None,
            "status": status if status is not None else _status()}


def _iso(value) -> str | None:
    return None if value is None else str(value)[:10]


def _status(dataset: str = "market_series") -> str:
    try:
        return read.dataset_status(dataset)
    except Exception as exc:                                  # noqa: BLE001 - a status read must not fail a render
        logger.warning("earnings market-store status read failed: %s", exc)
        return "unknown"


def _estimates_status() -> str:
    return _status(ESTIMATES_DATASET)


def dgs10_map(latest: date | None = None) -> tuple[dict[str, float], dict]:
    """``({"YYYY-MM-DD": yield}, note)``: the newest ``DGS10_LIMIT`` stored 10-year yield observations.

    ``latest`` is the day the freshness is measured against (default the latest complete session). An empty map means
    the board has no yield series: either nothing is stored, or the newest observation lags ``latest`` by more than
    ``MAX_LAG["dgs10"]`` sessions. The note says which, and carries the dataset's own check result.
    """
    try:
        day = _day(latest) or calendar.latest_complete_session()
    except ValueError:
        return {}, _missing("the 10-year yield", latest, "outside the calendar")
    series_id, source = DGS10_SERIES
    try:
        rows = read.market_series([series_id], start=day - timedelta(days=_LOOKBACK_DAYS), end=day)
    except Exception as exc:                                  # noqa: BLE001 - a store error is "not stored"
        logger.warning("earnings market-store read failed: %s", exc)
        return {}, _missing("the 10-year yield", day, f"read failed: {type(exc).__name__}")
    out: dict[str, float] = {}
    for r in rows:
        if r.get("source") != source or r.get("value") is None:
            continue
        obs = _day(r.get("obs_date"))
        if obs is not None:
            out[obs.isoformat()] = float(r["value"])
    if not out:
        return {}, _missing("the 10-year yield", day)
    newest = max(out)
    lag = _sessions_after(date.fromisoformat(newest), day)
    if lag is None or lag > MAX_LAG["dgs10"]:
        return {}, _missing("the 10-year yield", day, f"stale: newest observation {newest}")
    kept = dict(sorted(out.items())[-DGS10_LIMIT:])
    return kept, {"stored": True, "label": f"the 10-year yield through {newest}", "source": SERIES_SOURCE,
                  "source_as_of": newest, "status": _status()}


# The nine columns rule R1 reads: the EPS trend and the 7/30-day revision counts. The names the fundamentals store
# serves are D14's own (``rcdata/build/fundamentals/estimates.value_columns``), so no renaming happens here.
ESTIMATE_FIELDS = ("eps_trend_current", "eps_trend_7d", "eps_trend_30d", "eps_trend_60d", "eps_trend_90d",
                   "eps_rev_up_7d", "eps_rev_up_30d", "eps_rev_down_7d", "eps_rev_down_30d")
ESTIMATE_PERIOD = "0q"               # the current quarter: the print R1 scores


def estimates(tickers) -> tuple[dict[str, dict], dict]:
    """``({ticker: current-quarter row}, note)`` from the fundamentals store's ``estimate_latest``.

    Only ``ESTIMATE_PERIOD`` is kept: that is the print R1 scores. A ticker the store has no row for is absent from
    the map, and R1 stays unavailable on its card (it is logged only, weight 0) — never a reason to call a provider.
    """
    names = sorted({str(t).strip().upper() for t in (tickers or []) if str(t or "").strip()})
    if not names:
        return {}, _missing("the analyst estimates", None, "no ticker asked", source=ESTIMATES_SOURCE,
                            status=_estimates_status())
    try:
        rows = read.estimates(names)
    except Exception as exc:                                  # noqa: BLE001 - a store error is "not stored"
        logger.warning("earnings fundamentals-store read failed: %s", exc)
        return {}, _missing("the analyst estimates", None, f"read failed: {type(exc).__name__}",
                            source=ESTIMATES_SOURCE, status=_estimates_status())
    out: dict[str, dict] = {}
    as_of: list[str] = []
    for r in rows:
        if str(r.get("period") or "") != ESTIMATE_PERIOD:
            continue
        ticker = str(r.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        out[ticker] = {k: r.get(k) for k in ESTIMATE_FIELDS}
        if r.get("source_as_of") is not None:
            as_of.append(_iso(r["source_as_of"]))
    if not out:
        return {}, _missing("the analyst estimates", None, source=ESTIMATES_SOURCE,
                            status=_estimates_status())
    newest = max(as_of) if as_of else None
    return out, {"stored": True, "label": f"{len(out)} of {len(names)} names have estimates",
                 "source": ESTIMATES_SOURCE, "source_as_of": newest, "status": _estimates_status()}
