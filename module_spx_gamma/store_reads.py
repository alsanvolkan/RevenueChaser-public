"""SPX Gamma's one reader of the market store (D4, PR 3): the board's other modules import only this for market data.

Each reader is a thin mapping over ``rcdata.read``, which opens the serving file read-only for the one call and closes
it again; nothing here keeps a connection or runs SQL of its own. A day the store does not hold comes back as a
"not stored" marker (``stored: False`` and a label naming the day), never as an exception and never as a provider
call: there is no live fallback any more.

- ``zero_one_dte_dollars``: dollar OI gamma per strike, ``call_gamma_oi + put_gamma_oi`` summed over the expiries 0
  and 1 DTE of ``spx_gamma_expiry_strike``, for the pin and squeeze tests (review S4). A day the builder has not
  stored yet because its session is not closed reads the previous session's snapshot, kept to the expiries on or
  after the day (decision A3): that is the day's 0 DTE expiry only, so it is labelled "0 DTE only (previous-session
  snapshot, ...)" and the tests run on that smaller, conservative sum (review SF2).
- ``vix_family``: the Cboe closes of the VIX family on the newest observation on or before the day, within
  ``MAX_LAG`` sessions, keyed with the caret the board's payload always used (``^VIX`` ...).
- ``cot_tff``: the CFTC TFF row of the E-mini S&P 500 (``13874A``, futures only): the newest report released on or
  before the day (a Tuesday report counts as released on the Friday, ``RELEASE_DAYS``: an approximation of CFTC's
  schedule, which holiday weeks move), within ``MAX_LAG`` sessions. The
  weekly change is this report's long minus the previous report's (decision A5): the builder keeps no ``change_*``.
  It is None (``change_gap``) when the two newest reports are not exactly a week apart (review SF4).
- ``spx_candles``: the SPX closes of the 8-week window. Cboe's SPX file has one column, so a row has a close and no
  open, high, low or volume (decision A6).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import read

from .candles import window_lag, window_start
from .vix import vix_term_mode

logger = logging.getLogger(__name__)

SPX_SOURCE = "rcdata:spx_gamma_expiry_strike"
SERIES_SOURCE = "rcdata:market_series"
CANDLE_SOURCE = f"{SERIES_SOURCE} SPX (cboe)"
# Sessions an observation may lag the board's day by (the limits the old stored readers had, stored_inputs.MAX_LAG).
MAX_LAG = {"vix": 3, "cot": 10}
# A TFF report holds Tuesday's positions and is usually published on the Friday after it. An approximation (review
# SF5): a holiday week moves the release to the next business day and the 2025 shutdown delayed it by weeks, so a
# past day's render can count a report as released a little early. CFTC's own schedule is a parked follow-up.
RELEASE_DAYS = 3
WEEK_DAYS = 7
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


def _within(obs: date, day: date, kind: str) -> bool:
    lag = _sessions_after(obs, day)
    return lag is not None and lag <= MAX_LAG[kind]


def _missing(what: str, day, reason: str | None = None, **extra) -> dict:
    label = f"{what} not stored for {_iso(day)}" + (f" ({reason})" if reason else "")
    return {"stored": False, "label": label, "status": extra.pop("status", None), "source": extra.pop("source"),
            "source_as_of": None, **extra}


def _failed(exc: Exception) -> str:
    logger.warning("spx gamma store read failed: %s", exc)
    return f"read failed: {type(exc).__name__}: {exc}"[:200]


def _value(row: dict) -> float | None:
    """The close of an OHLC row, else the one value column (VVIX, SPX)."""
    v = row.get("close")
    return float(v) if v is not None else (float(row["value"]) if row.get("value") is not None else None)


def _not_closed(day: date) -> bool:
    try:
        return day > calendar.latest_complete_session()
    except ValueError:
        return False


# ── 0-1 DTE dollar gamma ──

def zero_one_dte_dollars(day) -> dict:
    """``{strike: dollars}`` of 0-1 DTE OI gamma for the board's ``day``, with the snapshot it came from."""
    d = _day(day)
    try:
        session, fallback = d, False
        rows = read.spx_expiry_strike(d, 0, 1)
        if not rows and _not_closed(d):
            # A3: the builder stores a session after its close; before that the previous session's snapshot is the
            # newest there is, and of it only the expiries on or after the board's day are 0-1 DTE for that day.
            session, fallback = calendar.previous_trading_day(d - timedelta(days=1)), True
            rows = [r for r in read.spx_expiry_strike(session, 0, 1) if _day(r["expiry"]) >= d]
    except Exception as exc:  # noqa: BLE001 - a store problem is "not stored", never a failed board
        return _missing("0-1 DTE", d, _failed(exc), source=SPX_SOURCE, dollars={}, fallback=False)
    if not rows:
        try:
            status, note = read.dataset_status(read.DATASET_OF_TABLE["spx_gamma_expiry_strike"]), None
        except Exception as exc:  # noqa: BLE001 - review SF3: a broken ops.sqlite is a note, never a failed board
            status, note = None, f"status unknown, {_failed(exc)}"
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

def vix_family(day) -> dict:
    """The VIX family closes as ``{"tickers": {"^VIX": ...}, "term_mode": ...}`` (the old payload) in ``payload``."""
    d = _day(day)
    try:
        rows = read.market_series(list(VIX_SERIES), start=d - timedelta(days=_LOOKBACK_DAYS["vix"]), end=d)
    except Exception as exc:  # noqa: BLE001
        return _missing("VIX family", d, _failed(exc), source=SERIES_SOURCE, payload=None)
    vix_days = [r["obs_date"] for r in rows if r["series_id"] == "VIX" and _value(r) is not None]
    obs = max(vix_days) if vix_days else None
    if obs is None or not _within(_day(obs), d, "vix"):
        return _missing("VIX family", d, source=SERIES_SOURCE, payload=None,
                        status=rows[0]["status"] if rows else None)
    on_day = [r for r in rows if r["obs_date"] == obs]
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


def cot_tff(day) -> dict:
    """The E-mini TFF row in the shape ``normalize.normalize_cftc`` reads, in ``payload``."""
    d = _day(day)
    ids = [COT_PREFIX + f for f in COT_FIELDS]
    try:
        rows = read.market_series(ids, start=d - timedelta(days=_LOOKBACK_DAYS["cot"]), end=d)
    except Exception as exc:  # noqa: BLE001
        return _missing("CFTC COT", d, _failed(exc), source=SERIES_SOURCE, payload=None)
    reports: dict[date, dict[str, float]] = {}
    meta: dict[date, dict] = {}
    for r in rows:
        obs = _day(r["obs_date"])
        reports.setdefault(obs, {})[r["series_id"][len(COT_PREFIX):]] = r.get("value")
        meta.setdefault(obs, r)
    released = sorted(obs for obs in reports if obs + timedelta(days=RELEASE_DAYS) <= d)
    if not released or not _within(released[-1], d, "cot"):
        return _missing("CFTC COT", d, source=SERIES_SOURCE, payload=None, status=rows[0]["status"] if rows else None)
    now = reports[released[-1]]
    # review SF4: a weekly change only between reports exactly a week apart; across a missing report it is None
    # and the board says "n/a (gap)".
    gap = len(released) > 1 and (released[-1] - released[-2]).days != WEEK_DAYS
    prev = reports[released[-2]] if len(released) > 1 and not gap else {}
    payload: dict = {"report_date": released[-1].isoformat(),
                     "release_date": (released[-1] + timedelta(days=RELEASE_DAYS)).isoformat(),
                     "open_interest": now.get(COT_OPEN_INTEREST), "change_gap": gap}
    for group, (longs, shorts) in COT_GROUPS.items():
        long, short = _cot_report(now, longs), _cot_report(now, shorts)
        prev_long = _cot_report(prev, longs)
        payload[group] = {"long": long, "short": short,
                          "net": (long - short) if long is not None and short is not None else None,
                          "change": (long - prev_long) if long is not None and prev_long is not None else None}
    row = meta[released[-1]]
    return {"stored": True, "label": f"CFTC TFF report of {payload['report_date']}, released "
                                     f"~{payload['release_date']} (report + {RELEASE_DAYS} days; holiday weeks can "
                                     f"differ) ({SERIES_SOURCE}, {row.get('source')}, "
                                     f"status {row.get('status')})",
            "payload": payload, "obs_date": payload["report_date"], "status": row.get("status"),
            "source": SERIES_SOURCE, "source_as_of": _iso(row.get("source_as_of"))}


# ── SPX candles ──

def spx_candles(day) -> dict:
    """The SPX closes from the 8-week window's start through ``day`` in ``rows`` (oldest first)."""
    d = _day(day)
    try:
        rows = read.market_series(["SPX"], start=window_start(d), end=d)
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
