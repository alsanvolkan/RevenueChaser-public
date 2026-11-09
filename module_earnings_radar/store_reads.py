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
- ``short_interest`` (``ownership.short_interest``): rule R5's squeeze inputs from FINRA's twice-monthly settlement short
  interest (``finra_short_interest``, the owner's B on #287): shares short over the float the prices store holds, FINRA's
  days to cover, and IBKR's borrow fee of the newest snapshot. The nightly D10 table of UW's daily figure and the live
  ``/short_screener`` call it fell back to are no longer read: the two series measure different things.

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
from RevenueChaser.rcdata.needs.catalog import FromCatalog

logger = logging.getLogger(__name__)

SERIES_SOURCE = "rcdata:market_series"
ESTIMATES_SOURCE = "rcdata:estimate_latest"
# Sessions an observation may lag the board's day by before it is not read. The limit the old stored reader had
# (``stored_inputs.MAX_LAG["macro_series"]``): FRED posts a day late, so a normal night reads 1 or 2, and 3 covers one
# missed night. Past that the series would silently pair fewer and fewer recent dates, so it is "not stored" instead.
# The catalog is the single owner of this number (avo, 29 Oct 2026). ``MAX_LAG`` stays readable as a mapping for the
# callers that have always used it; both it and the hash answer from ``macro.dgs10``.
_NEED_OF_LAG = {"dgs10": "macro.dgs10"}


def max_lag() -> dict[str, int]:
    """``{input: sessions it may lag by}``, read from the need catalog on every call."""
    from RevenueChaser.rcdata import needs
    return {key: needs.catalog.NEEDS[need].max_lag for key, need in _NEED_OF_LAG.items()}


# One class for every board's limits map, in the needs layer since #330 (SPX Gamma reads its lags the same way).
_FromCatalog = FromCatalog


MAX_LAG = _FromCatalog(max_lag)
# The newest observations the board pairs (R6): the window the old reader asked for.
DGS10_LIMIT = 80
# Calendar days read back to find ``DGS10_LIMIT`` observations: 80 sessions is about 16 weeks, doubled for holidays
# and the days FRED has no value for.
_LOOKBACK_DAYS = 240
DGS10_SERIES = tuple(MACRO_SERIES["US10Y"]["series"])
# The dataset name the fundamentals estimates are checked under (``rcdata/build/fundamentals/estimates.NAME``, "yfinance/estimates").
ESTIMATES_DATASET = "yfinance/estimates"
# Calendar days a stored estimate may be old before it is not read. The fundamentals build asks each company again
# once its answer is a week old (``rcdata/build/fundamentals/estimates.WEEK_DAYS``), so a normal read is 0 to 7 days old
# and 14 covers one missed weekly run. Past that the build has stopped renewing it: R1 would score an old EPS trend
# without a word, so the row is "not stored" instead. The nightly table the board read before allowed 5 sessions, but it
# was asked every night. Default chosen in the PR 285 review; change it if avo prefers.
_NEED_OF_AGE = {"estimates": "fundamentals.estimates", "short_interest": "ownership.short_interest"}


def max_age_days() -> dict[str, int]:
    """``{input: calendar days it may be old}``, read from the need catalog on every call."""
    from RevenueChaser.rcdata import needs
    out = {}
    for key, name in _NEED_OF_AGE.items():
        need = needs.catalog.NEEDS[name]
        if need.max_lag_unit != "days":
            raise ValueError(f"{name}: this reader counts calendar days, the catalog says {need.max_lag_unit}")
        out[key] = need.max_lag
    return out


MAX_AGE_DAYS = _FromCatalog(max_age_days)


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


def estimates(tickers, today: date | None = None) -> tuple[dict[str, dict], dict]:
    """``({ticker: current-quarter row}, note)`` from the fundamentals store's ``estimate_latest``.

    Only ``ESTIMATE_PERIOD`` is kept: that is the print R1 scores. A ticker the store has no row for is absent from
    the map, and R1 stays unavailable on its card (it is logged only, weight 0) — never a reason to call a provider.
    So is a ticker whose row has no as-of day or is more than ``MAX_AGE_DAYS["estimates"]`` days older than ``today``
    (default New York's today); the note counts those it left out.
    """
    try:
        today = _day(today) or calendar.today_ny()
    except ValueError:
        today = calendar.today_ny()
    oldest = today - timedelta(days=MAX_AGE_DAYS["estimates"])
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
    stale: dict[str, str | None] = {}
    for r in rows:
        if str(r.get("period") or "") != ESTIMATE_PERIOD:
            continue
        ticker = str(r.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        day = _day(r.get("source_as_of"))
        if day is None or day < oldest:
            stale[ticker] = _iso(day)
            continue
        out[ticker] = {k: r.get(k) for k in ESTIMATE_FIELDS}
        as_of.append(day.isoformat())
    left_out = f"{len(stale)} left out as older than {MAX_AGE_DAYS['estimates']} days" if stale else ""
    if not out:
        newest_stale = max((d for d in stale.values() if d), default=None)
        reason = (left_out + (f", newest {newest_stale}" if newest_stale else "")) if stale else None
        return {}, _missing("the analyst estimates", None, reason, source=ESTIMATES_SOURCE,
                            status=_estimates_status())
    return out, {"stored": True,
                 "label": f"{len(out)} of {len(names)} names have estimates" + (f"; {left_out}" if left_out else ""),
                 "source": ESTIMATES_SOURCE, "source_as_of": max(as_of), "status": _estimates_status()}


SHORT_SOURCE = "rcdata:finra_short_interest"
SHORT_DATASET = "finra/short_interest"
FEE_NEED = "ownership.shortable"


def _short_status() -> str:
    return _status(SHORT_DATASET)


def _floats(names: list[str]) -> dict[str, float]:
    """``{ticker: float shares}``: the newest float the prices store holds for each name, of any source.

    shortcut: no age limit on the float (it moves slowly and the prices build renews it); add one if R5 ever reads a
    float that is years old.
    """
    ids = read.security_ids(names)
    newest: dict[str, tuple] = {}
    for r in read.shares(ids.values(), measure="float"):
        sid, day, value = r.get("security_id"), _day(r.get("as_of")), r.get("value")
        if value and value > 0 and day is not None and (sid not in newest or day >= newest[sid][0]):
            newest[sid] = (day, float(value))
    return {t: newest[sid][1] for t, sid in ids.items() if sid in newest}


def _fees(names: list[str]) -> dict[str, float]:
    """``{ticker: borrow fee}`` from IBKR's newest snapshot, when it is inside ``ownership.shortable``'s session lag."""
    from RevenueChaser.rcdata import needs
    lag_max = needs.catalog.NEEDS[FEE_NEED].max_lag
    latest = calendar.latest_complete_session()
    out: dict[str, float] = {}
    for r in read.shortable(names):
        day, fee = _day(r.get("session_date")), r.get("fee_rate")
        lag = None if day is None else _sessions_after(day, latest)
        if fee is not None and lag is not None and lag <= lag_max:
            out[str(r.get("ticker") or "").upper()] = float(fee)
    return out


def short_interest(tickers, today: date | None = None) -> tuple[dict[str, dict], dict]:
    """``({ticker: R5 squeeze inputs}, note)`` from FINRA's newest settlement of each name in the ownership store.

    A row is ``{"si_pct", "days_to_cover", "borrow_fee", "settlement_date"}``: shares short as a percent of the float
    (None when the prices store has no float), FINRA's days to cover, IBKR's borrow fee (None when its snapshot is
    older than ``ownership.shortable`` allows) and the settlement day the figure is as of. A name whose newest
    settlement is more than ``MAX_AGE_DAYS["short_interest"]`` calendar days before ``today`` (default New York's
    today) is absent, and so is one FINRA does not list: R5 then reads flow only. Never a provider call.
    """
    try:
        today = _day(today) or calendar.today_ny()
    except ValueError:
        today = calendar.today_ny()
    oldest = today - timedelta(days=MAX_AGE_DAYS["short_interest"])
    names = sorted({str(t).strip().upper() for t in (tickers or []) if str(t or "").strip()})
    if not names:
        return {}, _missing("the short interest", None, "no ticker asked", source=SHORT_SOURCE, status=_short_status())
    try:
        rows = read.finra_short_interest(names, start=oldest, end=today)
        floats = _floats(names)
        fees = _fees(names)
    except Exception as exc:                                  # noqa: BLE001 - a store error is "not stored"
        logger.warning("earnings ownership-store read failed: %s", exc)
        return {}, _missing("the short interest", None, f"read failed: {type(exc).__name__}", source=SHORT_SOURCE,
                            status=_short_status())
    best: dict[str, dict] = {}
    for r in rows:
        ticker = str(r.get("ticker") or "").upper()
        day = _day(r.get("settlement_date"))
        if not ticker or day is None:
            continue
        rank = (day, r.get("symbol") == ticker)       # the newest settlement; on a tie, the ticker's own symbol
        if ticker not in best or rank > best[ticker]["_rank"]:
            best[ticker] = {**r, "_rank": rank}
    out: dict[str, dict] = {}
    for ticker, r in best.items():
        short, flt, dtc = r.get("current_short"), floats.get(ticker), r.get("days_to_cover")
        out[ticker] = {"si_pct": 100.0 * short / flt if short is not None and flt else None,
                       "days_to_cover": None if dtc is None else float(dtc), "borrow_fee": fees.get(ticker),
                       "settlement_date": r["_rank"][0].isoformat()}
    if not out:
        return {}, _missing("the short interest", None, f"no settlement inside {MAX_AGE_DAYS['short_interest']} days",
                            source=SHORT_SOURCE, status=_short_status())
    newest = max(r["settlement_date"] for r in out.values())
    return out, {"stored": True,
                 "label": f"{len(out)} of {len(names)} names have FINRA short interest, settlement {newest}",
                 "source": SHORT_SOURCE, "source_as_of": newest, "status": _short_status()}
