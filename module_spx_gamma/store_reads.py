"""SPX Gamma's one reader of the market store (D4, PR 3): the board's other modules import only this for market data.

Each reader is a thin mapping over the needs layer (#330): it asks ``needs.ask(need, as_of=day, ...)``, which calls the
need's ``rcdata.read`` function (the serving file opened read-only for the one call and closed again) and judges the
answer's lag against the board's own day, not against the market's newest session. The board names needs, never a
series id; nothing here keeps a connection, runs SQL of its own or counts sessions. The lag limits are the catalog's
(``MAX_LAG`` reads them from ``market.vix`` and ``market.cot`` on every call). A day the store does not hold comes
back as a "not stored" marker (``stored: False`` and a label naming the day), never as an exception and never as a
provider call: there is no live fallback any more.

- ``zero_one_dte_dollars`` (``market.spx_expiry_strike``): dollar OI gamma per strike,
  ``call_gamma_oi + put_gamma_oi`` summed over the expiries 0 and 1 DTE of ``spx_gamma_expiry_strike``, for the pin and
  squeeze tests (review S4). A day the builder has not stored yet because its session is not closed reads the previous
  session's snapshot, kept to the expiries on or after the day (decision A3): that is the day's 0 DTE expiry only, so
  it is labelled "0 DTE only (previous-session snapshot, ...)" and the tests run on that smaller, conservative sum
  (review SF2).
- ``vix_family`` (``market.vix``): the Cboe closes of the VIX family on the newest observation of VIX on or before the
  day, within ``MAX_LAG["vix"]`` sessions of the day, keyed with the caret the board's payload always used (``^VIX``).
- ``cot_tff`` (``market.cot``, ``market.cot_release``): the CFTC TFF row of the E-mini S&P 500 (``13874A``, futures
  only): the newest report released on or before the day, within ``MAX_LAG["cot"]`` sessions of the day. A report is
  released on CFTC's own release day from the stored schedule (#227); a report the schedule does not cover (a year
  before the first weekly fetch) counts as released on its Tuesday + ``RELEASE_DAYS`` (the Friday), an estimate the
  label and ``release_estimated`` say is one. The
  weekly change is this report's long minus the previous report's (decision A5): the builder keeps no ``change_*``.
  It is None (``change_gap``, "n/a (gap)") when the two newest reports are not 5 to 9 days apart (review SF4, #228).
- ``spx_candles`` (``market.spx_close``): the SPX closes of the 8-week window. Cboe's SPX file has one column, so a
  row has a close and no open, high, low or volume (decision A6).
"""
from __future__ import annotations

import functools
import logging
import sqlite3
from datetime import date, timedelta

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import needs, ops, read, serving
from RevenueChaser.rcdata.needs.catalog import FromCatalog

from .candles import window_lag, window_start
from .vix import vix_term_mode

logger = logging.getLogger(__name__)

SPX_SOURCE = "rcdata:spx_gamma_expiry_strike"
SERIES_SOURCE = "rcdata:market_series"
CANDLE_SOURCE = f"{SERIES_SOURCE} SPX (cboe)"
# Sessions an observation may lag the board's day by. The catalog owns the numbers (avo, 29 Oct 2026; #330): this map
# reads them from the needs on every call, the way Earnings Radar's does, so the board keeps no copy.
_NEED_OF_LAG = {"vix": "market.vix", "cot": "market.cot"}


def max_lag() -> dict[str, int]:
    """``{input: sessions it may lag the board's day by}``, read from the need catalog on every call."""
    return {key: needs.catalog.NEEDS[need].max_lag for key, need in _NEED_OF_LAG.items()}


MAX_LAG = FromCatalog(max_lag)
# A TFF report holds Tuesday's positions and is usually published on the Friday after it. An approximation (review
# SF5): a holiday week moves the release to the next business day and the 2025 shutdown delayed it by weeks, so a
# past day's render can count a report as released a little early. Used only where CFTC's schedule has no row (#227).
RELEASE_DAYS = 3
WEEK_DAYS = 7
CONSECUTIVE_DAYS = range(WEEK_DAYS - 2, WEEK_DAYS + 3)
VIX_SERIES = {"VIX": "^VIX", "VIX9D": "^VIX9D", "VIX3M": "^VIX3M", "VIX6M": "^VIX6M", "VIX1Y": "^VIX1Y",
              "VVIX": "^VVIX"}
COT_PREFIX = "cftc:tff:13874A:"
# group -> (long fields, short fields), in the order ``core.providers.cftc.map_row`` tried them.
COT_GROUPS = {
    "asset_manager": (("asset_mgr_positions_long", "asset_mgr_positions_long_all"),
                      ("asset_mgr_positions_short", "asset_mgr_positions_short_all")),
    "leveraged_funds": (("lev_money_positions_long", "lev_money_positions_long_all"),
                        ("lev_money_positions_short", "lev_money_positions_short_all")),
    "dealer": (("dealer_positions_long_all",), ("dealer_positions_short_all",)),
}
COT_OPEN_INTEREST = "open_interest_all"
COT_FIELDS = tuple(sorted({f for longs, shorts in COT_GROUPS.values() for f in longs + shorts}
                          | {COT_OPEN_INTEREST}))
_LOOKBACK_DAYS = {"vix": 14, "cot": 120}


def _day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _iso(value) -> str | None:
    return None if value is None else str(value)[:10]


def _stale(ans, obs: date, kind: str) -> bool:
    """True when the board's observation ``obs`` is more than ``MAX_LAG[kind]`` behind the day the answer was judged
    from (``ans.freshness["reference"]``, the session on or before the board's day).

    The envelope counts the lag from the newest day its rows carry. The board reads the newest day of one series of
    the need (VIX's own close, not a sibling's), which is that day unless a sibling series is newer; then the lag is
    counted from the board's day with the envelope's own count, so the rule stays "the observation the board shows".

    A lag the calendar cannot count (a day outside its holiday table: the envelope's ``lag_reason``) is stale, so the
    input reads "not stored" as the board's old ``_sessions_after`` had it.
    """
    f = ans.freshness
    if f.get("lag_reason"):
        return True
    try:
        lag = f["lag_sessions"] if f["as_of_session"] == obs else needs.lag_in(f["unit"], obs, f["reference"])
    except ValueError:
        return True
    return lag is None or lag > MAX_LAG[kind]


def _missing(what: str, day, reason: str | None = None, **extra) -> dict:
    label = f"{what} not stored for {_iso(day)}" + (f" ({reason})" if reason else "")
    return {"stored": False, "label": label, "status": extra.pop("status", None), "source": extra.pop("source"),
            "source_as_of": None, "failed": bool(reason and reason.startswith(FAILED_PREFIX)), **extra}


FAILED_PREFIX = "read failed: "


def _failed(exc: Exception) -> str:
    logger.warning("spx gamma store read failed: %s", exc)
    return f"{FAILED_PREFIX}{type(exc).__name__}: {exc}"[:200]


def _value(row: dict) -> float | None:
    """The close of an OHLC row, else the one value column (VVIX, SPX)."""
    v = row.get("close")
    return float(v) if v is not None else (float(row["value"]) if row.get("value") is not None else None)


def _not_closed(day: date) -> bool:
    try:
        return day > calendar.latest_complete_session()
    except ValueError:
        return False


# ── the status the board shows ──

def _board_status(dataset: str) -> tuple[str, str | None]:
    """``(status, skip note)``: the newest check that is not ``skipped`` (``ops.latest_unskipped_check``, as the
    Settings card reads it), so a Fetch while the dataset is paused or day-capped does not make a built dataset read
    "skipped" (review SF6b); a newer skip is named in the note."""
    last = ops.read_latest_check(dataset)       # a page read: never creates ops.sqlite (#336)
    if last is None:
        return read.NEVER_CHECKED, None
    if last.get("result") != "skipped":
        return last["result"], None
    base = ops.latest_unskipped_check(dataset)
    note = f"last run skipped ({last.get('note')})" if last.get("note") else "last run skipped"
    return (base["result"] if base else read.NEVER_CHECKED), note


def _with_board_status(dataset: str):
    """Re-stamp a reader's ``status`` (and the status in its label) with :func:`_board_status`; a failure to read
    it leaves the reader's own status (review SF3: never an exception out of a reader)."""
    def wrap(reader):
        @functools.wraps(reader)
        def inner(day) -> dict:
            out = reader(day)
            if out.get("failed"):
                return out          # #228 item 2: a read-failed marker keeps no status, not the board's
            try:
                status, skip = _board_status(dataset)
            except Exception as exc:  # noqa: BLE001
                _failed(exc)
                return out
            old = out.get("status")
            if old is not None and f"status {old})" in out["label"]:
                out["label"] = out["label"].replace(f"status {old})", f"status {status})")
            out["status"] = status
            if skip:
                out["label"] = f"{out['label']}; {skip}"
            return out
        return inner
    return wrap


# ── 0-1 DTE dollar gamma ──

def _expiry_strike(session: date, day: date) -> tuple[list[dict], str | None, str | None]:
    """``(rows, status, note)`` of one session's 0-1 DTE slice, judged from the board's ``day``.

    Review SF3: with no serving file yet the reader answers no rows without touching ``ops.sqlite``, and the
    envelope then reads the dataset's status from it; a broken file there (locked, stamped newer, corrupt) is a note
    on the "not stored" marker, never a failed board, and A3 may still look at the previous session. With the file
    there, a status that cannot be read comes back as ``status_unknown`` on rows that are kept (#228 item 5); any
    other sqlite error of the row read goes up as "read failed" and stops (#334 review, should-fix 3).
    """
    try:
        ans = needs.ask("market.spx_expiry_strike", as_of=day, session_date=session, min_dte=0, max_dte=1)
    except sqlite3.Error as exc:
        if serving.live_path(needs.NEEDS["market.spx_expiry_strike"].kind).exists():
            raise
        return [], None, f"status unknown, {_failed(exc)}"
    return ans.rows, ans.status, None


@_with_board_status("uw/spot_expiry_strike")
def zero_one_dte_dollars(day) -> dict:
    """``{strike: dollars}`` of 0-1 DTE OI gamma for the board's ``day``, with the snapshot it came from."""
    d = _day(day)
    try:
        session, fallback = d, False
        rows, status, note = _expiry_strike(d, d)
        if not rows and _not_closed(d):
            # A3: the builder stores a session after its close; before that the previous session's snapshot is the
            # newest there is, and of it only the expiries on or after the board's day are 0-1 DTE for that day.
            session, fallback = calendar.previous_trading_day(d - timedelta(days=1)), True
            rows, status, note = _expiry_strike(session, d)
            rows = [r for r in rows if _day(r["expiry"]) >= d]
    except Exception as exc:  # noqa: BLE001 - a store problem is "not stored", never a failed board
        return _missing("0-1 DTE", d, _failed(exc), source=SPX_SOURCE, dollars={}, fallback=False)
    if not rows:
        return _missing("0-1 DTE", d, note, source=SPX_SOURCE, dollars={}, fallback=False, status=status)
    dollars: dict[float, float] = {}
    for r in rows:
        c, p = r.get("call_gamma_oi"), r.get("put_gamma_oi")
        if c is None and p is None:
            continue
        strike = float(r["strike"])
        dollars[strike] = dollars.get(strike, 0.0) + (c or 0.0) + (p or 0.0)
    first = rows[0]
    snapshot_time = first.get("snapshot_time")
    stamp = f"{session.isoformat()} {snapshot_time or 'unknown-time'}"
    if fallback:
        # review SF2: the builder stores a session's own expiry and the next one, so the previous session's snapshot
        # holds the day's 0 DTE expiry only (never its 1 DTE one): a partial sum, said as such.
        label = f"0 DTE only (previous-session snapshot, {stamp}) ({SPX_SOURCE}, status {first.get('status')})"
    else:
        label = f"0-1 DTE from the {stamp} snapshot ({SPX_SOURCE}, status {first.get('status')})"
    return {"stored": True, "label": label, "dollars": dollars, "session": session.isoformat(),
            "snapshot_time": snapshot_time, "fallback": fallback, "status": first.get("status"),
            "source": SPX_SOURCE, "source_as_of": _iso(first.get("source_as_of")),
            "expiries": sorted({_iso(r["expiry"]) for r in rows})}


# ── VIX family ──

@_with_board_status("market_series")
def vix_family(day) -> dict:
    """The VIX family closes as ``{"tickers": {"^VIX": ...}, "term_mode": ...}`` (the old payload) in ``payload``."""
    d = _day(day)
    try:
        ans = needs.ask("market.vix", as_of=d, start=d - timedelta(days=_LOOKBACK_DAYS["vix"]), end=d)
        rows = ans.rows
        vix_days = [_day(r["obs_date"]) for r in rows if r["series_id"] == "VIX" and _value(r) is not None]
        obs = max(vix_days) if vix_days else None
        stale = obs is None or _stale(ans, obs, "vix")
    except Exception as exc:  # noqa: BLE001
        return _missing("VIX family", d, _failed(exc), source=SERIES_SOURCE, payload=None)
    if stale:
        return _missing("VIX family", d, source=SERIES_SOURCE, payload=None,
                        status=rows[0]["status"] if rows else None)
    on_day = [r for r in rows if _day(r["obs_date"]) == obs]
    tickers = {caret: None for caret in VIX_SERIES.values()}
    for r in on_day:
        tickers[VIX_SERIES[r["series_id"]]] = _value(r)
    vix_row = next(r for r in on_day if r["series_id"] == "VIX")
    return {"stored": True, "label": f"VIX family of {_iso(obs)} ({SERIES_SOURCE}, {vix_row.get('source')}, "
                                     f"status {vix_row.get('status')})",
            "payload": {"tickers": tickers, "term_mode": vix_term_mode(tickers)}, "obs_date": _iso(obs),
            "status": vix_row.get("status"), "source": SERIES_SOURCE, "source_as_of": _iso(vix_row.get("source_as_of"))}


# ── CFTC COT ──

def _cot_report(by_field: dict[str, float], fields: tuple[str, ...]) -> float | None:
    return next((by_field[f] for f in fields if by_field.get(f) is not None), None)


def _release_of(report: date, lags: dict[date, float]) -> tuple[date, bool]:
    """``(release day, estimated)``: from the schedule row of the report's week (keyed on its Tuesday; a holiday can
    date a report on the Monday), else the estimate: that Tuesday + ``RELEASE_DAYS``, the Friday."""
    tuesday = report + timedelta(days=1 - report.weekday())
    lag = lags.get(tuesday)
    return tuesday + timedelta(days=int(lag) if lag is not None else RELEASE_DAYS), lag is None


@_with_board_status("market_series")
def cot_tff(day) -> dict:
    """The E-mini TFF row in the shape ``normalize.normalize_cftc`` reads, in ``payload``."""
    d = _day(day)
    try:
        start = d - timedelta(days=_LOOKBACK_DAYS["cot"])
        ans = needs.ask("market.cot", as_of=d, start=start, end=d)
        rows = ans.rows
        reports: dict[date, dict[str, float]] = {}
        meta: dict[date, dict] = {}
        for r in rows:
            field_name = r["series_id"][len(COT_PREFIX):]
            if field_name not in COT_FIELDS:
                continue
            obs = _day(r["obs_date"])
            reports.setdefault(obs, {})[field_name] = r.get("value")
            meta.setdefault(obs, r)
        lags = {_day(r["obs_date"]): r["value"]
                for r in needs.ask("market.cot_release", as_of=d, start=start, end=d).rows}
        release = {obs: _release_of(obs, lags) for obs in reports}
        # Only reports released by the day, so the envelope judges the newest *released* report against it.
        reports = {obs: v for obs, v in reports.items() if release[obs][0] <= d}
        released = sorted(reports)
        stale = not released or _stale(ans, released[-1], "cot")
    except Exception as exc:  # noqa: BLE001
        return _missing("CFTC COT", d, _failed(exc), source=SERIES_SOURCE, payload=None)
    if stale:
        return _missing("CFTC COT", d, source=SERIES_SOURCE, payload=None, status=rows[0]["status"] if rows else None)
    now = reports[released[-1]]
    # review SF4: a weekly change only between consecutive reports; across a missing report it is None and the board
    # says "n/a (gap)". Consecutive is 5 to 9 days apart (#228 item 3): a holiday week dates a report on the Monday.
    gap = len(released) > 1 and (released[-1] - released[-2]).days not in CONSECUTIVE_DAYS
    prev = reports[released[-2]] if len(released) > 1 and not gap else {}
    released_on, estimated = release[released[-1]]
    payload: dict = {"report_date": released[-1].isoformat(), "release_date": released_on.isoformat(),
                     "release_estimated": estimated, "open_interest": now.get(COT_OPEN_INTEREST), "change_gap": gap}
    for group, (longs, shorts) in COT_GROUPS.items():
        long, short = _cot_report(now, longs), _cot_report(now, shorts)
        prev_long = _cot_report(prev, longs)
        payload[group] = {"long": long, "short": short,
                          "net": (long - short) if long is not None and short is not None else None,
                          "change": (long - prev_long) if long is not None and prev_long is not None else None}
    row = meta[released[-1]]
    when = (f"~{payload['release_date']} (report + {RELEASE_DAYS} days; holiday weeks can differ)" if estimated
            else f"{payload['release_date']} (CFTC schedule)")
    return {"stored": True, "label": f"CFTC TFF report of {payload['report_date']}, released {when} "
                                     f"({SERIES_SOURCE}, {row.get('source')}, status {row.get('status')})",
            "payload": payload, "obs_date": payload["report_date"], "status": row.get("status"),
            "source": SERIES_SOURCE, "source_as_of": _iso(row.get("source_as_of"))}


# ── SPX candles ──

@_with_board_status("market_series")
def spx_candles(day) -> dict:
    """The SPX closes from the 8-week window's start through ``day`` in ``rows`` (oldest first)."""
    d = _day(day)
    try:
        rows = needs.ask("market.spx_close", as_of=d, start=window_start(d), end=d).rows
    except Exception as exc:  # noqa: BLE001
        return _missing("SPX closes", d, _failed(exc), source=SERIES_SOURCE, rows=[])
    out = [{"date": _iso(r["obs_date"]), "open": r.get("open"), "high": r.get("high"), "low": r.get("low"),
            "close": _value(r), "volume": None} for r in rows if _value(r) is not None]
    out.sort(key=lambda r: r["date"])           # spot is rows[-1]: the order is load-bearing (#293 review S-1)
    if not out:
        return _missing("SPX closes", d, source=SERIES_SOURCE, rows=[], status=rows[0]["status"] if rows else None)
    last = rows[-1]
    return {"stored": True, "label": f"SPX closes to {out[-1]['date']} ({SERIES_SOURCE}, {last.get('source')}, "
                                     f"status {last.get('status')})",
            "rows": out, "obs_date": out[-1]["date"], "status": last.get("status"), "source": SERIES_SOURCE,
            "source_as_of": _iso(last.get("source_as_of"))}


# ── everything build_day reads ──

def _meta(reader_out: dict) -> dict:
    return {k: v for k, v in reader_out.items() if k not in ("payload", "rows", "dollars")}


def read_day(day) -> dict:
    """The raw-dict keys the store now fills for ``day``: ``vix``, ``cftc``, ``ohlc``, ``zero_one_dte`` and
    ``store_inputs`` (per input: stored or not, its label, source, ``source_as_of`` and status)."""
    vix, cot, candles, zod = vix_family(day), cot_tff(day), spx_candles(day), zero_one_dte_dollars(day)
    candle_source = candles["label"]
    if candles["stored"]:
        candle_source = CANDLE_SOURCE
        try:
            expected, last = window_lag(candles["rows"], _day(day))
        except ValueError:
            expected = last = None
        if expected:
            candle_source = f"{CANDLE_SOURCE}, stale to {last}"
    return {
        "vix": vix.get("payload"),
        "cftc": cot.get("payload"),
        "ohlc": {"source": candle_source, "data": candles["rows"]},
        "zero_one_dte": zod,
        "store_inputs": {"vix": _meta(vix), "cftc": _meta(cot), "candles": _meta(candles),
                         "zero_one_dte": _meta(zod)},
    }
