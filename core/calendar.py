"""US market calendar: NYSE full-day closures + trade-by date.

``trade_by_date`` is used by the Earnings Radar GUI to show the last session in
which a position can still be opened before an earnings print: PM reports trade
by that day's close, AM reports trade by the previous session's close (rolling
back over weekends and holidays).

The closure table is transcribed from https://www.nyse.com/markets/hours-calendars
(the same table the uw-earnings-vol-scan skill ships). It stops after 2028
because that is as far as the NYSE publishes — past its horizon these helpers
RAISE rather than guess, because a guessed holiday would move a deadline onto a
closed day.

The second half of the module is the New York clock the forecast ledger (#128) and the
post-close Fetch (#129) share: when a session closed, which session is the latest
*complete* one, and whether a wall-clock moment is inside the US trading window. Every
conversion goes through ``America/New_York``, never a fixed UTC offset, so the US/EU
daylight-saving gap weeks (late Oct, mid Mar) and the Nov/Mar US changes stay correct.

The last part (#283, ADR-3) is the one session date every dated table is keyed on:
``session_for`` maps a run time to its New York session (rule B) and ``to_session_date`` is
the read helper that does the same for the old run-time columns (epoch, ISO text, plain
dates). Rule C: the holiday table is NOT extended back; a date outside 2025-2028 raises, and
sessions before 2025 come from the stored price bars, not from this module.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo

# MM-DD in date order. A Saturday New Year's (2028) drops the January entry
# because the NYSE does not close the preceding Friday.
_MARKET_HOLIDAYS = {
    2025: "01-01 01-09 01-20 02-17 04-18 05-26 06-19 07-04 09-01 11-27 12-25",
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25",
    2027: "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24",
    2028: "01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25",
}

MARKET_HOLIDAYS: dict[int, frozenset[date]] = {
    y: frozenset(date(y, int(md[:2]), int(md[3:])) for md in mds.split())
    for y, mds in _MARKET_HOLIDAYS.items()
}


def market_holidays(year: int) -> frozenset[date]:
    """NYSE full-day closures for ``year``. Raises rather than guessing."""
    try:
        return MARKET_HOLIDAYS[year]
    except KeyError:
        lo, hi = min(MARKET_HOLIDAYS), max(MARKET_HOLIDAYS)
        raise ValueError(
            f"the NYSE holiday table does not cover {year}; it runs {lo}-{hi}. "
            f"Add {year} to _MARKET_HOLIDAYS from "
            "https://www.nyse.com/markets/hours-calendars."
        )


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in market_holidays(d.year)


def previous_trading_day(d: date) -> date:
    """The latest trading day <= ``d``."""
    for _ in range(15):  # longest real closure stretch is a few days
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    raise ValueError(f"no trading day found on or before {d}")


def next_trading_day(d: date) -> date:
    """The earliest trading day > ``d`` (so an AM report still needs tonight)."""
    for _ in range(15):
        d += timedelta(days=1)
        if is_trading_day(d):
            return d
    raise ValueError(f"no trading day found after {d}")


def trade_by_date(report_date: date | None, is_am: bool | None) -> date | None:
    """Last session the position can still be opened before the print.

    - PM (is_am False) on X -> enter before X's close (trade_by = X).
    - AM (is_am True)  on X -> enter before the PREVIOUS session's close.
    - ``None`` input (unknown label/date) -> ``None`` (never fabricate).
    """
    if report_date is None or is_am is None:
        return None
    if is_am:
        return previous_trading_day(report_date - timedelta(days=1))
    return previous_trading_day(report_date)


# ── New York session clock ──────────────────────────────────────────────────

NY = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
# Daily bars are only trusted once the data vendors have settled the session. 30 minutes
# after the close covers the last prints and the official closing auction.
COMPLETE_MARGIN = timedelta(minutes=30)
WINDOW_END = time(16, 30)    # = SESSION_CLOSE + COMPLETE_MARGIN


def _now() -> datetime:
    """The current time, timezone-aware UTC. The one clock seam: tests (and the e2e dashboard
    subprocess, via ``RC_E2E_NOW``) replace this function; production code never calls
    ``datetime.now`` for a session decision."""
    return datetime.now(timezone.utc)


def _as_ny(now: datetime | None) -> datetime:
    now = _now() if now is None else now
    if now.tzinfo is None:
        raise ValueError("a session decision needs a timezone-aware time")
    return now.astimezone(NY)


def now_ny() -> datetime:
    """The current New York wall-clock time (aware), through the clock seam."""
    return _as_ny(None)


def today_ny() -> date:
    """Today's New York calendar date, through the clock seam (not the machine's local date)."""
    return _as_ny(None).date()


def session_close(d: date) -> datetime:
    """16:00 New York on trading day ``d``, as an aware datetime.

    Early-close days (13:00 ET: the day after Thanksgiving, Christmas Eve, and in some years
    the day before Independence Day) are deliberately treated as full days. That is conservative: the
    session counts as complete later than it needs to, never earlier, so a freeze can be
    delayed by a few hours on three days a year but can never mix in unfinished bars. It
    also needs no second table that could drift from the holiday one.
    """
    if not is_trading_day(d):
        raise ValueError(f"{d} is not an NYSE trading day")
    return datetime.combine(d, SESSION_CLOSE, tzinfo=NY)


def latest_complete_session(now: datetime | None = None) -> date:
    """The latest trading day whose session is complete at ``now`` (default: the clock seam).

    Complete = ``now`` is at least ``COMPLETE_MARGIN`` after that day's close. The calendar
    day used is the New York one, not the UTC one. Raises ``ValueError`` past the end of
    the holiday table (never guesses).
    """
    ny = _as_ny(now)
    d = ny.date()
    if is_trading_day(d) and ny >= session_close(d) + COMPLETE_MARGIN:
        return d
    return previous_trading_day(d - timedelta(days=1))


def in_us_session_window(now: datetime | None = None) -> bool:
    """True from 09:30 to 16:30 New York time on a trading day.

    A run that overlaps this window mixes unfinished intraday bars with finished ones, so
    the ledger never freezes inside it. Raises ``ValueError`` for a weekday past the end
    of the holiday table.
    """
    ny = _as_ny(now)
    return is_trading_day(ny.date()) and SESSION_OPEN <= ny.time() < WINDOW_END


def bars_complete(session: date, fetched_epoch: float) -> bool:
    """True when data written at ``fetched_epoch`` (epoch seconds) covers the whole ``session``."""
    written = datetime.fromtimestamp(float(fetched_epoch), tz=timezone.utc)
    return written >= session_close(session) + COMPLETE_MARGIN


# ── One session date (#283, ADR-3) ──────────────────────────────────────────

QUALITY_COMPLETE = "complete"
QUALITY_INTRADAY = "intraday"


class SessionStamp(NamedTuple):
    session_date: date
    quality: str    # QUALITY_COMPLETE or QUALITY_INTRADAY


def session_for(run_at: datetime | float | int) -> SessionStamp:
    """The New York session a run at ``run_at`` belongs to (rule B), and its quality.

    - On a trading day from 09:30 ET on: that day. Before 16:30 ET (``WINDOW_END``) the bars
      are unfinished, so quality is ``intraday``; from 16:30 on it is ``complete``.
    - Before 09:30 ET, on a weekend or on a holiday: the previous trading day, ``complete``.

    ``run_at`` is a timezone-aware datetime or epoch seconds (UTC). The date is the New York
    one, never the UTC or Amsterdam one. Early closes count as full days (see
    ``session_close``). Raises ``ValueError`` for a naive datetime or a day outside the
    holiday table (2025-2028); it never guesses.
    """
    if isinstance(run_at, bool):
        raise ValueError("run_at must be a datetime or epoch seconds")
    if isinstance(run_at, (int, float)):
        run_at = datetime.fromtimestamp(float(run_at), tz=timezone.utc)
    ny = _as_ny(run_at)
    d = ny.date()
    if is_trading_day(d) and ny.time() >= SESSION_OPEN:
        quality = QUALITY_INTRADAY if ny.time() < WINDOW_END else QUALITY_COMPLETE
        return SessionStamp(d, quality)
    return SessionStamp(previous_trading_day(d - timedelta(days=1)), QUALITY_COMPLETE)


def to_session_date(value) -> date | None:
    """Read helper: map an old run-time column value to its session date.

    Accepts epoch seconds, an aware or naive ``datetime`` (naive = New York time), an ISO
    text with or without a time, or a ``date``. A value with a time goes through rule B
    (``session_for``). A date-only value has no time of day, so it becomes the latest session
    on or before it (a Saturday gives Friday). ``None`` and ``""`` give ``None``; anything
    else unparseable raises ``ValueError``, and so does a date outside 2025-2028.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"cannot read a session date from {value!r}")
    if isinstance(value, (int, float)):
        return session_for(value).session_date
    if isinstance(value, datetime):
        return session_for(value if value.tzinfo else value.replace(tzinfo=NY)).session_date
    if isinstance(value, date):
        return previous_trading_day(value)
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 10:
            return previous_trading_day(date.fromisoformat(text))
        parsed = datetime.fromisoformat(text)
        return session_for(parsed if parsed.tzinfo else parsed.replace(tzinfo=NY)).session_date
    raise ValueError(f"cannot read a session date from {value!r}")
