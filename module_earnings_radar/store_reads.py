"""Earnings Radar's reads of the serving stores: the options store, ``market_series`` and the fundamentals store.

Each reader is a thin mapping over ``rcdata.read``, which opens the serving file read-only for the one call and closes
it again (the same shape as ``module_spx_gamma/store_reads.py``). Nothing here keeps a connection, writes, or calls a
provider; a missing file reads as no rows or a "not stored" marker, never a live read.

The options store and the rate events (G9, build plan PR 19):

* **The screener set** (``option_screener_daily``, ``uw/option_screener``): every name reporting in the 28 days after
  the session, with its UW screener row. The gates and the pre-score read it (``screener_set``), in the shape of the
  UW screener answer the pipeline read before, so the gate rule itself is unchanged. The newest session is used
  while it lags the latest complete session by at most ``SCREENER_MAX_LAG`` sessions; an older one, or none, is
  "no screener row" for every name, and the run says so on the board.
* **A card's options inputs** (``card_inputs``), three ways per name (the plan, avo 27 Oct):
  1. a name on the option watch list (or SPX) reads the list set: its ``option_daily`` row (the GEX levels), its
     ``option_strike`` rows (spot exposures by strike, the GEX profile), its ``option_expiry`` rows (the term
     structure) and its ``option_print`` rows, of its newest session before the report;
  2. a name off the list that passed the gates on the evening before its report reads the earnings set that
     ``uw/option_screener`` fetched that evening for that report (``er_options`` and the same three tables, set
     ``earnings``);
  3. any other name (it failed the gates, or its earnings-set fetch is missing, or it reports later than the next
     session) has no options inputs: R4, R5 and R8 show ``NOT_ON_LIST`` and give no score.
* **R6's rate events**: the FOMC decision and CPI release days from ``market_series`` (``rate_event_days``).

Named after the need the internal data API will serve them as:

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

from RevenueChaser.core import board_requests, calendar
from RevenueChaser.core.config import to_float
from RevenueChaser.core.rotation_reference import MACRO_SERIES
from RevenueChaser.rcdata import read

logger = logging.getLogger(__name__)

NOT_ON_LIST = board_requests.NOT_ON_LIST
LIST_SET, EARNINGS_SET = "list", "earnings"
# Trading sessions the newest screener session may lag the latest complete one by: the set is fetched every evening,
# so 0 on a normal day and 1 after one missed night.
SCREENER_MAX_LAG = 1
RATE_SERIES = ("FOMC_DECISION", "CPI_RELEASE")
GEX_LEVELS = ("call_wall", "gamma_flip", "gamma_magnet", "put_wall")
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
# Calendar days a stored estimate may be old before it is not read. The fundamentals build asks each company again
# once its answer is a week old (``rcdata/build/fundamentals/estimates.WEEK_DAYS``), so a normal read is 0 to 7 days old
# and 14 covers one missed weekly run. Past that the build has stopped renewing it: R1 would score an old EPS trend
# without a word, so the row is "not stored" instead. The nightly table the board read before allowed 5 sessions, but it
# was asked every night. Default chosen in the PR 285 review; change it if avo prefers.
MAX_AGE_DAYS = {"estimates": 14}


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


def _lag(day: date, latest: date) -> int | None:
    return _sessions_after(day, latest)


# ── the screener set ─────────────────────────────────────────────────────────────────────────────────────────────

def screener_set(tickers, latest: date | None = None) -> tuple[dict[str, dict], dict]:
    """``({ticker: screener row}, outcome)`` for ``tickers`` from the newest stored screener session.

    A row is the UW screener answer the gates read (``call_volume``, ``put_volume``, ``total_open_interest``,
    ``call_premium``, ``put_premium``, ``close``, ``iv30d``, ``iv_rank``, ``marketcap``, ...); a name UW had no
    screener row for (``screened`` false) has none. ``outcome`` is ``{"status", "error", "session"}``: ``ok``,
    ``stale`` (the newest session is older than ``SCREENER_MAX_LAG`` sessions: no rows) or ``missing`` (no session).
    """
    wanted = {str(t).upper() for t in tickers if t}
    rows = read.option_screener()
    if not rows:
        return {}, {"status": "missing", "error": {"reason": "no stored screener session"}, "session": None}
    session = _day(rows[0]["session_date"])
    latest = calendar.latest_complete_session() if latest is None else latest
    lag = _lag(session, latest)
    if lag is None or lag > SCREENER_MAX_LAG:
        return {}, {"status": "stale", "error": {"reason": f"newest stored screener session {session}"},
                    "session": session.isoformat()}
    out = {}
    for r in rows:
        t = str(r.get("ticker") or "").upper()
        if t in wanted and r.get("screened"):
            out[t] = {k: v for k, v in r.items() if v is not None and k not in ("status",)}
    return out, {"status": "ok", "error": None, "session": session.isoformat()}


# ── a card's options inputs ──────────────────────────────────────────────────────────────────────────────────────

def _list_session(ticker: str, report_date: date) -> tuple[date, dict] | None:
    """The newest ``option_daily`` session of a list name before ``report_date``, and its row."""
    rows = [r for r in read.option_daily(ticker, None, report_date - timedelta(days=1)) if r.get("session_date")]
    if not rows:
        return None
    row = max(rows, key=lambda r: r["session_date"])
    return _day(row["session_date"]), row


def _earnings_session(ticker: str, report_date: date) -> tuple[date, dict] | None:
    """The ``er_options`` row ``uw/option_screener`` fetched for this report, the evening before it."""
    rows = [r for r in read.earnings_options(tickers=[ticker]) if _day(r.get("report_date")) == report_date]
    if not rows:
        return None
    row = max(rows, key=lambda r: r["session_date"])
    return _day(row["session_date"]), row


def card_inputs(ticker: str, report_date: date, listed) -> dict:
    """The options inputs of one card: ``{"set", "session", "gex", "strikes", "term", "prints", "volume"}``.

    ``set`` is ``list``, ``earnings`` or None (the three-way rule in the module doc); with None every other field is
    empty and the card's R4, R5 and R8 read ``NOT_ON_LIST``. ``gex`` holds the four GEX levels, ``strikes`` the
    spot exposures by strike (the GEX profile's rows), ``term`` the term structure as ``{"dte", "volatility"}`` rows,
    ``prints`` the stored prints (the newest 50 of the session, as UW's ``flow-recent`` served them) and ``volume``
    the day's option volume and open interest.
    """
    t = str(ticker).upper()
    found, set_name = None, None
    if t in set(listed):
        found, set_name = _list_session(t, report_date), LIST_SET
    else:
        found, set_name = _earnings_session(t, report_date), EARNINGS_SET
    if found is None:
        return {"set": None, "session": None, "gex": {}, "strikes": [], "term": [], "prints": [], "volume": {}}
    session, row = found
    term = [{"dte": r.get("days_to_expiry"), "volatility": r.get("volatility"), "expiry": r.get("expiry")}
            for r in read.option_expiries(t, session, set_name)]
    volume = {"option_volume": (row.get("option_volume") if set_name == EARNINGS_SET
                                else (to_float(row.get("call_volume")) or 0) + (to_float(row.get("put_volume")) or 0)),
              "open_interest": (row.get("total_open_interest") if set_name == EARNINGS_SET
                                else (to_float(row.get("call_open_interest")) or 0)
                                + (to_float(row.get("put_open_interest")) or 0))}
    return {"set": set_name, "session": session, "gex": {k: row.get(k) for k in GEX_LEVELS},
            "strikes": read.option_strikes(t, session, set_name), "term": term,
            "prints": [p for p in read.option_prints(t, session, set_name) if not p.get("canceled")],
            "volume": volume}


# ── R6: the rate events ────────────────────────────────────────────────────────────────────────────────────────

def rate_event_days() -> dict[str, list[date]]:
    """``{series: [days]}`` of the FOMC decisions and CPI releases ``market_series`` holds (one read per run)."""
    out: dict[str, list[date]] = {s: [] for s in RATE_SERIES}
    for r in read.market_series(RATE_SERIES):
        if r.get("obs_date") is not None and (to_float(r.get("value")) or 0) > 0:
            out.setdefault(r["series_id"], []).append(_day(r["obs_date"]))
    return out


def rate_event_overlap(report_date, events: dict[str, list[date]]) -> bool | None:
    """Is an FOMC decision or a CPI release inside ``report_date`` ± 1 trading day?

    ``True``/``False`` when both series span the window (the Fed's page and the BLS calendar list the year ahead),
    None when either does not: R6 must not turn a calendar that does not reach the report into "no event"."""
    from RevenueChaser.module_earnings_radar import stored_inputs
    window = stored_inputs.rate_event_window(report_date)
    if window is None:
        return None
    start, end = window
    days = [events.get(s) or [] for s in RATE_SERIES]
    if any(not d or min(d) > start or max(d) < end for d in days):
        return None
    return any(start <= d <= end for series in days for d in series)
