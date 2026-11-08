"""A5: hourly-close trigger / confirm / reset of the directional scenarios (30 Oct 2026 report, "Üç senaryo").

The report reads SPX hourly closes from IBKR at 09:30 + n hours New York (10:30 ... 15:30) and the 16:00 close. UW
does not sell SPX index prices to this account (G2-06; its answer names a CBOE approval), so phase 1 reads SPY
30-minute bars from UW, keeps the bars that end on those report times, and scales them to SPX by the ratio of the
last session both series closed (stored SPX close / SPY close). The scaled close is a proxy and is labelled one on
the board; IBKR's own SPX hourly close is phase 2.

Rule, pinned to the 30 Oct report (Downside: trigger 7600, confirm 7575, reset 7625):

* **Trigger**: an hourly close beyond the band's near edge (below the downside band's top; above the upside
  band's floor).
* **Confirm**: a LATER hourly close a further ``CONFIRM_STEP`` beyond it (30 Oct: "tetikten sonraki saatlik
  kapanışlardan biri"; the trigger bar itself never confirms).
* **Reset**: before a confirm, an hourly close ``RESET_STEP`` back on the other side drops the trigger; a new
  trigger and confirm are then needed. A confirm is never undone by a reset (after entry the stop rules apply).

shortcut: the upside mirrors the downside rule on the flip; the report states its upside condition differently
(a close above the turning point AND the week's gamma turning positive). The validity window (last entry day) and
the Friday-close exception beyond "the week starts Monday" are not modelled.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
AMS = ZoneInfo("Europe/Amsterdam")

CONFIRM_STEP = 25.0
RESET_STEP = 25.0
#: The UW candle size read; a 30-minute bar ends on every report time (:30 and the close).
CANDLE_SIZE = "30m"
#: Rows asked for: a week of regular 30-minute bars (13 a day) plus the prior session and extended-hours rows.
CANDLE_LIMIT = 200
PROXY_TICKER = "SPY"


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _bar(row: dict) -> tuple[datetime, float] | None:
    """``(end time, close)`` of a regular-session bar in either UW shape (REST or MCP); None otherwise."""
    if not isinstance(row, dict):
        return None
    market = row.get("market_time", row.get("market"))
    if market not in (None, "r"):
        return None
    end = row.get("end_time") or row.get("end")
    close = _num(row.get("close", row.get("c")))
    if not end or close is None:
        return None
    try:
        when = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when, close


def _bars(payload) -> list[tuple[datetime, float]]:
    rows = payload.get("data") if isinstance(payload, dict) else payload
    out = [b for b in (_bar(r) for r in rows or []) if b is not None]
    return sorted(out)


def spx_ratio(bars, candles) -> float | None:
    """Stored SPX close / SPY session close, for the latest session both have; None without one."""
    spy_close: dict[str, float] = {}
    for when, close in (bars if bars and isinstance(bars[0], tuple) else _bars(bars)):
        ny = when.astimezone(NY)
        spy_close[ny.date().isoformat()] = close     # bars are oldest first: the last one of a date is its close
    for c in sorted(candles or [], key=lambda r: str(r.get("date")), reverse=True):
        spx = _num(c.get("close"))
        spy = spy_close.get(str(c.get("date"))[:10])
        if spx and spy:
            return spx / spy
    return None


def _report_close(ny: datetime) -> bool:
    """A report hourly close: 10:30 ... 15:30 New York, or the 16:00 session close. (A 13:00 early close is
    accepted by ``hourly_closes`` only when it is the day's last bar.)"""
    if ny.minute == 30 and (ny.hour, ny.minute) >= (10, 30) and ny.hour <= 15:
        return True
    return ny.minute == 0 and ny.hour == 16


def _session_over(ny: datetime, asof: date, now: datetime | None) -> bool:
    """Whether a day whose last bar so far ends 13:00 NY really closed then, rather than still trading.

    A full day fetched between 13:00 and the next bar's arrival also ends on its 13:00 bar. So the day must be
    before the fetch's own day, or the fetch must come at or after 13:30 NY, when a full day's next bar exists.
    Without a fetch time, only a day before ``asof`` is known to be over.
    """
    if now is None:
        return ny.date() < asof
    fetched = now.astimezone(NY)
    return ny.date() < fetched.date() or fetched >= ny.replace(hour=13, minute=30)


def hourly_closes(payload, asof: str, candles, now: datetime | None = None) -> dict | None:
    """The target week's report-time closes as SPX-equivalent levels, oldest first; None without a ratio.

    The week is the one the record resolves on (``brier.target_friday``): from its Monday, so a Friday's own closes
    never count, and nothing after ``asof``. A bar ending after ``now`` (still forming) is left out.
    """
    from .brier import target_friday

    bars = _bars(payload)
    ratio = spx_ratio(bars, candles)
    if not bars or ratio is None:
        return None
    monday = date.fromisoformat(target_friday(asof)) - timedelta(days=4)
    last = date.fromisoformat(asof)   # a re-built past day never sees a later session's closes
    last_of_day: dict = {}
    for when, _c in bars:
        last_of_day[when.astimezone(NY).date()] = when
    rows = []
    for when, close in bars:
        ny = when.astimezone(NY)
        short_day_close = ((ny.hour, ny.minute) == (13, 0) and last_of_day.get(ny.date()) == when
                           and _session_over(ny, last, now))
        if ny.date() < monday or ny.date() > last or not (_report_close(ny) or short_day_close):
            continue
        if now is not None and when > now:
            continue
        rows.append({"time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "amsterdam": when.astimezone(AMS).strftime("%a %H:%M"),
                     "close": close * ratio, "proxy": close})
    return {"rows": rows, "ratio": round(ratio, 6), "week_start": monday.isoformat(), "source": PROXY_TICKER}


def _walk(closes, beyond, back, trigger: float, confirm: float, reset: float) -> dict:
    state, trig_at, conf_at, reset_at = "waiting", None, None, None
    for row in closes:
        c = _num(row.get("close"))
        if c is None:
            continue
        if state == "waiting":
            if beyond(c, trigger):
                state, trig_at = "triggered", row.get("time")
        elif state == "triggered":
            if beyond(c, confirm):
                state, conf_at = "confirmed", row.get("time")
            elif back(c, reset):
                state, reset_at, trig_at = "waiting", row.get("time"), None
                if beyond(c, trigger):   # a close can never be both back past the reset and beyond the trigger
                    state, trig_at = "triggered", row.get("time")
    return {"trigger": trigger, "confirm": confirm, "reset": reset, "state": state,
            "triggered_at": trig_at, "confirmed_at": conf_at, "reset_at": reset_at}


def scenario_triggers(bands: dict | None, closes) -> dict | None:
    """Trigger / confirm / reset state of the Upside and Downside classes over ``closes``; None without bands."""
    if not bands:
        return None
    down_top = float(bands["downside"][1])
    up_floor = float(bands["upside"][0])
    closes = list(closes or [])
    return {
        "Downside": _walk(closes, lambda c, x: c < x, lambda c, x: c >= x,
                          down_top, down_top - CONFIRM_STEP, down_top + RESET_STEP),
        "Upside": _walk(closes, lambda c, x: c > x, lambda c, x: c <= x,
                        up_floor, up_floor + CONFIRM_STEP, up_floor - RESET_STEP),
    }


def trigger_text(name: str, state: dict | None, rows=None) -> str:
    """One board line for a class's trigger state, e.g. "triggered Tue 16:30 · confirm < 7,575 · reset ≥ 7,625"."""
    if not state:
        return "n/a"
    when = {r.get("time"): r.get("amsterdam") for r in rows or []}
    below = name == "Downside"
    lt, ge = ("<", "≥") if below else (">", "≤")
    levels = (f"trigger {lt} {state['trigger']:,.0f} · confirm {lt} {state['confirm']:,.0f} · "
              f"reset {ge} {state['reset']:,.0f}")
    if state["state"] == "confirmed":
        head = f"CONFIRMED {when.get(state['confirmed_at'], '')} (triggered {when.get(state['triggered_at'], '')})"
    elif state["state"] == "triggered":
        head = f"TRIGGERED {when.get(state['triggered_at'], '')}, waiting for confirm"
    elif state.get("reset_at"):
        head = f"reset {when.get(state['reset_at'], '')}, waiting for a new trigger"
    else:
        head = "waiting"
    return f"{head} — {levels}"


__all__ = ["trigger_text", "CONFIRM_STEP", "RESET_STEP", "CANDLE_SIZE", "CANDLE_LIMIT", "PROXY_TICKER",
           "hourly_closes", "scenario_triggers", "spx_ratio"]
