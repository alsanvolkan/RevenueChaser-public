"""Read the inputs the nightly RC-Earnings-Inputs sweeps stored (E5, #372).

The Earnings Fetch used to ask UW (and FRED) for everything it needed, every run. Since E0 the nightly job stores
the same answers in ``raw.duckdb``: the earnings calendar (D26), short interest (D10), the economic calendar (D37),
the FRED daily series (D27), the analyst estimates (D14), and the two market series SPX Gamma reads instead of
fetching (the vol indices, D28, and the CFTC COT, D29). This module is the ONE reader of those tables — it reads
only, over ``rawstore.query(..., read_only=True)``, and never writes, migrates or alters anything.

Two rules hold for every reader here:

- **A missing table or a store error is "nothing stored"** — the caller falls back to the live provider, exactly as
  it did before this module existed. A broken store must never fail a Fetch, so the error is logged once per table
  and swallowed.
- **A stored session is only used while it is fresh.** The newest stored ``session_date`` at or before
  ``calendar.latest_complete_session()`` is taken only when it is at most ``MAX_LAG[table]`` trading sessions older.
  A stale calendar is worse than a live read: it would silently drop the names UW has added since.

Each reader returns the data and a coverage note the pipeline records in the run's result history
(``input_sources``), so a run says which parts it read from the store and which it asked the provider for.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from RevenueChaser.core import calendar, rawstore

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

# Trading sessions a stored session may lag the latest complete one by, before the reader goes live instead.
# ``macro_series`` counts the sessions after the newest DGS10 observation. FRED posts a day late, so a normal night
# reads 1 or 2; 3 covers one missed night. A missed night next to a bond-only holiday (Columbus or Veterans Day)
# reaches 4 and the run asks FRED, which is one free call.
# ``vol_index_daily`` is a daily series too, so it gets the same tolerance as ``short_interest``.
# ``cftc_cot`` is weekly: CFTC publishes on Friday the positions held on the Tuesday before, so on a current night
# the newest stored row is already ~6 sessions old (measured: 2026-10-13 stored against the 2026-10-21 session). A
# tighter number would read "nothing stored" every night and the table would never be used.
MAX_LAG = {"earnings_calendar": 1, "short_interest": 3, "estimate_snapshots": 5, "macro_series": 3,
           "vol_index_daily": 3, "cftc_cot": 10}

_DGS10_LIMIT = 80                 # the same window ``macro_watch.fred_daily_map`` asks FRED for
_OHLC_MAX_BARS = 270              # ``pipeline.REACTION_OHLC_SESSIONS``
_OHLC_LOOKBACK_DAYS = 400         # calendar days back from the latest complete session

_LOGGED: set[str] = set()         # one note per table per process


def _note_once(table: str, exc: BaseException | None = None) -> None:
    if table in _LOGGED:
        return
    _LOGGED.add(table)
    logger.info("stored inputs: %s is not read (%s); the run asks the provider",
                table, f"{type(exc).__name__}: {exc}" if exc is not None else "no table")


def _query(sql: str, params: list | None = None, *, table: str, db: str = "raw") -> list[dict]:
    """``rawstore.query`` in read-only mode; a missing table or a store error is an empty result (logged once)."""
    try:
        return rawstore.query(sql, params or [], read_only=True, db=db) or []
    except Exception as exc:  # noqa: BLE001 - a Fetch must never fail on the store
        _note_once(table, exc)
        return []


def _as_date(value) -> date | None:
    """A stored DATE cell as a ``date`` (DuckDB serves ``date``, a legacy cell may be ISO text)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _as_json(value):
    """A stored ``JSON`` cell as a Python object (the text form when the driver hands back a string)."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _num(value) -> float | None:
    """A stored ``DOUBLE`` cell as a float (None for a NULL or an unreadable cell)."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _max_session(table: str, latest: date, *, db: str = "raw") -> date | None:
    """The newest stored ``session_date`` of ``table`` at or before ``latest``, or None."""
    rows = _query(f"SELECT max(session_date) AS s FROM {table} WHERE session_date <= ?", [latest],
                  table=table, db=db)
    return _as_date(rows[0].get("s")) if rows else None


def _lag_sessions(session: date | None, latest: date) -> int | None:
    """Trading sessions between ``session`` and ``latest`` (0 when the same), or None when it is not a session.

    Counted with ``calendar.previous_trading_day``, one step per session, so a weekend or a holiday costs nothing.
    """
    if session is None or session > latest:
        return None
    n, d = 0, latest
    while d > session and n <= 30:
        try:
            d = calendar.previous_trading_day(d - timedelta(days=1))
        except ValueError:
            return None
        n += 1
    return n if d == session else None


def _sessions_after(day: date, latest: date) -> int | None:
    """Trading sessions after ``day`` up to and including ``latest`` (0 when ``day >= latest``).

    Unlike :func:`_lag_sessions`, ``day`` need not be a session itself: a FRED observation can fall on a day the
    stock market is closed (Good Friday). None past the end of the holiday table, or beyond 30 sessions.
    """
    n, d = 0, day
    while n <= 30:
        try:
            d = calendar.next_trading_day(d)
        except ValueError:
            return None
        if d > latest:
            return n
        n += 1
    return None


def _fresh_session(table: str, latest: date | None = None, *, db: str = "raw") -> tuple[date | None, bool]:
    """``(session, usable)``: the newest stored session of ``table`` and whether it is inside ``MAX_LAG``."""
    latest = calendar.latest_complete_session() if latest is None else latest
    session = _max_session(table, latest, db=db)
    lag = _lag_sessions(session, latest)
    if lag is None or lag > MAX_LAG.get(table, 0):
        return session, False
    return session, True


# ── D26: the earnings calendar ───────────────────────────────────────────────────────────────────────────────────

def calendar_items(days) -> tuple[dict, date | None]:
    """The stored calendar rows for ``days``: ``({(earnings_date, report_session): [UW item, ...]}, session|None)``.

    ``item`` is the UW row as served, so the caller rebuilds the event from it rather than from the typed columns
    (``marketcap`` is a DOUBLE column, the live row carries text). A ``(day, session)`` pair is *covered* only when
    it has at least one row: a pair the sweep did not reach must still be asked for live.
    """
    wanted = {_as_date(d) for d in (days or [])}
    wanted.discard(None)
    if not wanted:
        return {}, None
    session, usable = _fresh_session("earnings_calendar")
    if not usable:
        return {}, None
    rows = _query("SELECT earnings_date, report_session, item FROM earnings_calendar WHERE session_date = ?",
                  [session], table="earnings_calendar")
    out: dict[tuple[date, str], list[dict]] = {}
    for r in rows:
        d = _as_date(r.get("earnings_date"))
        report_session = str(r.get("report_session") or "")
        if d is None or d not in wanted or not report_session:
            continue
        item = _as_json(r.get("item"))
        if not isinstance(item, dict):
            continue
        if not (item.get("symbol") or item.get("ticker")):
            continue
        out.setdefault((d, report_session), []).append(item)
    return out, session


# ── D10: short interest ──────────────────────────────────────────────────────────────────────────────────────────

# The ``short_interest`` columns that make up the UW row ``UWClient._parse_short_row`` reads.
_SHORT_FIELDS = ("short_interest", "si_float", "total_float", "days_to_cover", "fee_rate", "rebate_rate",
                 "short_shares_available")


def short_rows(tickers) -> tuple[dict[str, dict], date | None]:
    """``({ticker: UW-shaped short row}, session|None)`` from the newest fresh session of ``short_interest``.

    The row is the shape ``/short_screener`` serves, rebuilt from the typed columns (``payload`` is the source row
    but not every source fills it, and a NULL cell is dropped rather than served as ``None``). Any source counts:
    the sweep's yfinance fallback carries ``si_float`` and ``total_float`` only, and a ticker with no stored row
    here is one the caller must still ask UW for.
    """
    names = sorted({str(t).upper() for t in (tickers or []) if t})
    if not names:
        return {}, None
    session, usable = _fresh_session("short_interest")
    if not usable:
        return {}, None
    marks = ", ".join("?" * len(names))
    rows = _query(
        f"SELECT ticker, {', '.join(_SHORT_FIELDS)} FROM short_interest "
        f"WHERE session_date = ? AND ticker IN ({marks})",
        [session, *names], table="short_interest")
    out: dict[str, dict] = {}
    for r in rows:
        t = str(r.get("ticker") or "").upper()
        if not t:
            continue
        out[t] = {k: r.get(k) for k in _SHORT_FIELDS if r.get(k) is not None}
    return out, session


# ── D27: the FRED daily series (10Y yield) ───────────────────────────────────────────────────────────────────────

def dgs10_map(latest: date | None = None) -> dict[str, float]:
    """The stored FRED DGS10 observations as ``{YYYY-MM-DD: value}`` — the same window
    ``macro_watch.fred_daily_map("DGS10")`` asks FRED for (the newest 80, ascending order not needed: R6 pairs
    them by date). An empty map means the run should ask FRED.

    Fresh only while the newest observation is at most ``MAX_LAG["macro_series"]`` sessions before ``latest``
    (default ``calendar.latest_complete_session()``): if the nightly stops, beta would pair fewer and fewer
    recent dates until R6 went unavailable without a word, so a stale series is "nothing stored" and FRED is
    asked instead."""
    rows = _query("SELECT obs_date, value FROM macro_series WHERE series_id = 'DGS10' AND source = 'fred' "
                  "AND value IS NOT NULL ORDER BY obs_date DESC LIMIT ?", [_DGS10_LIMIT], table="macro_series")
    out: dict[str, float] = {}
    for r in rows:
        d = _as_date(r.get("obs_date"))
        try:
            value = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if d is not None:
            out[d.isoformat()] = value
    if not out:
        return {}
    latest = calendar.latest_complete_session() if latest is None else latest
    lag = _sessions_after(date.fromisoformat(max(out)), latest)
    if lag is None or lag > MAX_LAG["macro_series"]:
        return {}
    return out


# ── D37: the economic calendar (rate events) ─────────────────────────────────────────────────────────────────────

def _event_day(ts) -> date | None:
    """The New York date of a UW economic event (``event_ts`` is UTC epoch seconds)."""
    try:
        return datetime.fromtimestamp(float(ts), NY).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _is_rate_event(row: dict) -> bool:
    """FOMC, or a report whose text names CPI — the two releases that move the whole yield curve.

    Payrolls, GDP and the fed-speaker slots are not rate events (a bad print there is a growth story). The match is
    on the served text, so 'CPI', 'CPI, Y/Y%' and 'Core CPI, M/M%' all count while 'PPI' and
    'Ex-Food & Energy PPI' do not.
    """
    kind = str(row.get("type") or "").strip().lower()
    if kind == "fomc":
        return True
    if kind != "report":
        return False
    text = str(row.get("event") or "").lower()
    return "cpi" in text or "consumer price" in text


def rate_events() -> list[dict]:
    """Every stored ``economic_calendar`` row (``session_date``, ``event_ts``, ``event``, ``type``): one read per
    run, handed to :func:`rate_event_overlap` for each card. A missing table or a store error is an empty list."""
    return _query("SELECT session_date, event_ts, event, type FROM economic_calendar", table="economic_calendar")


def rate_event_window(report_date) -> tuple[date, date] | None:
    """``(start, end)``: the trading day before ``report_date`` and the trading day after it (±1 trading day).

    ``next_trading_day(d)`` is already the first trading day after ``d``, so a Monday report ends on Tuesday and a
    Friday one on Monday. None for a date outside the holiday table.
    """
    d = _as_date(report_date)
    if d is None:
        return None
    try:
        return calendar.previous_trading_day(d - timedelta(days=1)), calendar.next_trading_day(d)
    except ValueError:
        return None


def rate_event_overlap(report_date, events: list[dict] | None = None) -> bool | None:
    """Is an FOMC/CPI event inside ``report_date`` ± 1 trading day (:func:`rate_event_window`)?

    ``True``/``False`` when the stored ``economic_calendar`` spans the window, ``None`` when it does not — the
    store holds the current and next week, so a report three months out has no answer and R6 must not turn a
    missing calendar into a veto. The event's day is its New York date, never its UTC one (a 23:30 ET release is
    the same session in New York, the next day in UTC).

    The writer keeps every session's sweep, so a release that was moved keeps its old date in the older sweeps
    (review D-1). The answer comes from one sweep only: the newest ``session_date`` whose own rows span the window.

    ``events`` is the run's one :func:`rate_events` read; without it the table is read here.
    """
    window = rate_event_window(report_date)
    if window is None:
        return None
    start, end = window
    rows = rate_events() if events is None else events
    sweeps: dict[date | None, list[tuple[date, dict]]] = {}
    for r in rows:
        day = _event_day(r.get("event_ts"))
        if day is not None:
            sweeps.setdefault(_as_date(r.get("session_date")), []).append((day, r))
    for session in sorted(sweeps, key=lambda s: s or date.min, reverse=True):
        dated = sweeps[session]
        if min(d for d, _ in dated) > start or max(d for d, _ in dated) < end:
            continue
        return any(start <= d <= end and _is_rate_event(r) for d, r in dated)
    return None


# ── D14: analyst estimates ───────────────────────────────────────────────────────────────────────────────────────

# The nine columns rule 1 reads: the EPS trend and the 7/30-day revision counts.
_ESTIMATE_FIELDS = ("eps_trend_current", "eps_trend_7d", "eps_trend_30d", "eps_trend_60d", "eps_trend_90d",
                    "eps_rev_up_7d", "eps_rev_up_30d", "eps_rev_down_7d", "eps_rev_down_30d")


def estimates(tickers) -> tuple[dict[str, dict], date | None]:
    """``({ticker: 0q row}, session|None)`` from the newest fresh session of ``estimate_snapshots``.

    Only the current quarter (``period = '0q'``) is read: that is the print the board's rule 1 scores. A ticker
    with no row is one the caller has no estimate snapshot for.
    """
    names = sorted({str(t).upper() for t in (tickers or []) if t})
    if not names:
        return {}, None
    session, usable = _fresh_session("estimate_snapshots")
    if not usable:
        return {}, None
    marks = ", ".join("?" * len(names))
    rows = _query(f"SELECT ticker, {', '.join(_ESTIMATE_FIELDS)} FROM estimate_snapshots "
                  f"WHERE session_date = ? AND period = '0q' AND ticker IN ({marks})",
                  [session, *names], table="estimate_snapshots")
    out: dict[str, dict] = {}
    for r in rows:
        t = str(r.get("ticker") or "").upper()
        if t:
            out[t] = {k: r.get(k) for k in _ESTIMATE_FIELDS}
    return out, session


# ── D01: the daily price bars (the reaction window) ──────────────────────────────────────────────────────────────

# The keys the pipeline and ``history.packet_ohlc`` read from an ``ohlc_year`` bar — nothing else is mapped.
_OHLC_KEYS = ("open", "high", "low", "close", "volume")


def _bar(values: dict) -> dict:
    """One stored bar in the shape the provider serves (``date`` first, then the five numbers)."""
    out = {"date": values["date"]}
    for k in _OHLC_KEYS:
        v = values.get(k)
        try:
            out[k] = None if v is None else float(v)
        except (TypeError, ValueError):
            out[k] = None
    return out


def ohlc_bars(tickers) -> dict[str, list[dict]]:
    """``{ticker: live-shaped bars}`` from the stored D01 daily bars, oldest first, the last 270 per ticker.

    One ``prices.daily_bars`` read for the lot (K8a window: from ``latest_complete_session() - 400d`` on), nominal
    basis — the bars as served, with the splits and dividends in ``price_actions`` applied on read. A ticker with no
    bar is absent from the map: the caller must ask UW for it (and the pipeline only uses a series whose last bar is
    the latest complete session, which is what the reaction columns anchor on).
    """
    names = sorted({str(t).upper() for t in (tickers or []) if t})
    if not names:
        return {}
    from RevenueChaser.core import prices
    start = calendar.latest_complete_session() - timedelta(days=_OHLC_LOOKBACK_DAYS)
    try:
        frame = prices.daily_bars(names, basis="nominal", start=start)
    except Exception as exc:  # noqa: BLE001 - a Fetch must never fail on the store
        _note_once("price_bars_daily", exc)
        return {}
    out: dict[str, list[dict]] = {}
    for rec in frame.to_dict("records"):
        ticker = str(rec.get("ticker") or "").upper()
        d = _as_date(rec.get("session_date"))
        if not ticker or d is None:
            continue
        out.setdefault(ticker, []).append(_bar({**rec, "date": d.isoformat()}))
    for ticker, bars in out.items():
        bars.sort(key=lambda b: b["date"])
        out[ticker] = bars[-_OHLC_MAX_BARS:]
    return out


# ── D28: the volatility indices (the VIX family) ─────────────────────────────────────────────────────────────────

def vol_index_rows(latest: date | None = None) -> tuple[dict, date | None]:
    """``({"tickers": {"^VIX": close, ...}, "term_mode": ...}, session|None)`` from the newest fresh session of
    ``vol_index_daily`` — the payload ``module_spx_gamma.vix.fetch_vix_family`` serves, so ``normalize_vix`` cannot
    tell the two apart. (The table is this step's; SPX Gamma is its only reader.)

    Only the ``VIX_TICKERS`` closes are mapped: the sweep also stores ``OVX`` and ``MOVE``, which the board's VIX
    layer does not read. A stored symbol carries no caret (``inputs.vol_index.SYMBOLS`` is ``("VIX", ...)``) and the
    live payload is keyed with one, so the caret is added here. ``term_mode`` is SPX Gamma's own derivation, called
    on the dict rather than reimplemented; it is imported inside the function because that module reads this one.
    """
    session, usable = _fresh_session("vol_index_daily", latest)
    if not usable:
        return {}, None
    from RevenueChaser.module_spx_gamma.vix import VIX_TICKERS, vix_term_mode

    rows = _query("SELECT symbol, close FROM vol_index_daily WHERE session_date = ? AND close IS NOT NULL",
                  [session], table="vol_index_daily")
    tickers: dict[str, float] = {}
    for r in rows:
        symbol = str(r.get("symbol") or "").strip().upper()
        close = _num(r.get("close"))
        if close is not None and f"^{symbol}" in VIX_TICKERS:
            tickers[f"^{symbol}"] = close
    if not tickers:
        return {}, None
    return {"tickers": tickers, "term_mode": vix_term_mode(tickers)}, session


# ── D29: the CFTC COT (the E-mini S&P 500 TFF row) ──────────────────────────────────────────────────────────────

# The ``cftc_cot`` columns the live payload is rebuilt from: the writer stores the long/short sides and the long
# change, never a net (``row_from``).
_COT_COLUMNS = ("report_date", "open_interest",
                "asset_mgr_long", "asset_mgr_short", "asset_mgr_change_long",
                "lev_money_long", "lev_money_short", "lev_money_change_long",
                "dealer_long", "dealer_short", "dealer_change_long")


def _cot_group(long, short, change) -> dict:
    """One position group as ``core.providers.cftc.map_row`` builds it: ``net`` is None unless both sides exist."""
    long, short = _num(long), _num(short)
    return {"long": long, "short": short,
            "net": (long - short) if (long is not None and short is not None) else None,
            "change": _num(change)}


def cot_row(latest: date | None = None) -> tuple[dict, date | None]:
    """``(E-mini TFF payload, session|None)`` from the newest fresh session of ``cftc_cot`` — the shape
    ``core.providers.cftc.fetch_cftc_tff`` returns, so ``normalize_cftc`` cannot tell the two apart.

    ``report_date`` is the ISO text of the stored Tuesday (the day the live payload names, without Socrata's
    ``T00:00:00.000`` tail). The table's only writer keeps one market (the E-mini), so the newest row of the newest
    stored session is the row the live read would serve.
    """
    session, usable = _fresh_session("cftc_cot", latest)
    if not usable:
        return {}, None
    rows = _query(f"SELECT {', '.join(_COT_COLUMNS)} FROM cftc_cot WHERE session_date = ? "
                  f"ORDER BY report_date DESC LIMIT 1", [session], table="cftc_cot")
    if not rows:
        return {}, None
    r = rows[0]
    report = _as_date(r.get("report_date"))
    if report is None:
        return {}, None
    return {"report_date": report.isoformat(),
            "open_interest": _num(r.get("open_interest")),
            "asset_manager": _cot_group(r.get("asset_mgr_long"), r.get("asset_mgr_short"),
                                        r.get("asset_mgr_change_long")),
            "leveraged_funds": _cot_group(r.get("lev_money_long"), r.get("lev_money_short"),
                                          r.get("lev_money_change_long")),
            "dealer": _cot_group(r.get("dealer_long"), r.get("dealer_short"), r.get("dealer_change_long"))}, session
