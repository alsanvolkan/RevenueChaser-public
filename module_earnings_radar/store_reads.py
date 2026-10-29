"""Earnings Radar's reads of the options store and ``market_series`` (G9, build plan PR 19).

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
* **R6** reads the 10-year (``DGS10``) and the FOMC decision and CPI release days from ``market_series``.

Every read here is a read of a serving file: no provider call, no write. A missing file reads as no rows.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from RevenueChaser.core import board_requests, calendar
from RevenueChaser.core.config import to_float

logger = logging.getLogger(__name__)

NOT_ON_LIST = board_requests.NOT_ON_LIST
LIST_SET, EARNINGS_SET = "list", "earnings"
# Trading sessions the newest screener session may lag the latest complete one by: the set is fetched every evening,
# so 0 on a normal day and 1 after one missed night.
SCREENER_MAX_LAG = 1
# Sessions the newest DGS10 observation may lag the latest complete session by (``stored_inputs.MAX_LAG``).
DGS10_MAX_LAG = 3
DGS10_LOOKBACK_DAYS = 150          # about the 80 observations R6's beta pairs with the bars (``stored_inputs``)
RATE_SERIES = ("FOMC_DECISION", "CPI_RELEASE")
GEX_LEVELS = ("call_wall", "gamma_flip", "gamma_magnet", "put_wall")


def _day(value) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _lag(day: date, latest: date) -> int | None:
    from RevenueChaser.module_earnings_radar import stored_inputs
    return stored_inputs._sessions_after(day, latest)


# ── the screener set ─────────────────────────────────────────────────────────────────────────────────────────────

def screener_set(tickers, latest: date | None = None) -> tuple[dict[str, dict], dict]:
    """``({ticker: screener row}, outcome)`` for ``tickers`` from the newest stored screener session.

    A row is the UW screener answer the gates read (``call_volume``, ``put_volume``, ``total_open_interest``,
    ``call_premium``, ``put_premium``, ``close``, ``iv30d``, ``iv_rank``, ``marketcap``, ...); a name UW had no
    screener row for (``screened`` false) has none. ``outcome`` is ``{"status", "error", "session"}``: ``ok``,
    ``stale`` (the newest session is older than ``SCREENER_MAX_LAG`` sessions: no rows) or ``missing`` (no session).
    """
    from RevenueChaser.rcdata import read
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
    from RevenueChaser.rcdata import read
    rows = [r for r in read.option_daily(ticker, None, report_date - timedelta(days=1)) if r.get("session_date")]
    if not rows:
        return None
    row = max(rows, key=lambda r: r["session_date"])
    return _day(row["session_date"]), row


def _earnings_session(ticker: str, report_date: date) -> tuple[date, dict] | None:
    """The ``er_options`` row ``uw/option_screener`` fetched for this report, the evening before it."""
    from RevenueChaser.rcdata import read
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
    from RevenueChaser.rcdata import read
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


# ── R6: the 10-year and the rate events ──────────────────────────────────────────────────────────────────────────

def dgs10_map(latest: date | None = None) -> dict[str, float]:
    """The stored FRED DGS10 observations of the last ``DGS10_LOOKBACK_DAYS`` days as ``{YYYY-MM-DD: value}``; empty
    when the newest is more than ``DGS10_MAX_LAG`` sessions older than ``latest`` (R6 then has no beta: a stale
    series would pair fewer and fewer dates without a word)."""
    from RevenueChaser.rcdata import read
    latest = calendar.latest_complete_session() if latest is None else latest
    out: dict[str, float] = {}
    for r in read.market_series(["DGS10"], latest - timedelta(days=DGS10_LOOKBACK_DAYS), latest):
        value = to_float(r.get("value"))
        if r.get("source") == "fred" and value is not None and r.get("obs_date") is not None:
            out[_day(r["obs_date"]).isoformat()] = value
    if not out:
        return {}
    lag = _lag(date.fromisoformat(max(out)), latest)
    return out if lag is not None and lag <= DGS10_MAX_LAG else {}


def rate_event_days() -> dict[str, list[date]]:
    """``{series: [days]}`` of the FOMC decisions and CPI releases ``market_series`` holds (one read per run)."""
    from RevenueChaser.rcdata import read
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
