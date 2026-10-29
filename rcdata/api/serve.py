"""``get(need, **params)``: the envelope around one ``rcdata.read`` call.

What the envelope adds, and nothing else:

- ``state``: ``fresh`` | ``stale`` | ``missing`` — one word a card can show without knowing the store's shape.
- ``freshness``: the newest day the rows carry, how many trading sessions behind the market that is, and the need's
  own ``max_lag_sessions``. The lag is counted in sessions, not calendar days, so a Monday answer is not "stale by
  three" because of the weekend.
- ``coverage``: how many things were asked for, how many came back, and which ones the store does not hold.
- ``provenance``: per row, where it came from (``source``, ``source_as_of``, ``fetched_at``, ``run_id``) and which
  dataset owns writing it.
- ``status``: the newest ``dataset_check`` result of that dataset, as ``read`` already returns it per row.
- ``fill``: always ``None`` here. On-demand filling (``fill="if_stale"``), the per-need budget and the queue
  priority arrive in PR C; until then anything but ``fill="never"`` raises, so no caller can quietly depend on it.

The read log is bumped with the **need** name as well (``need:<name>``), beside the table name ``read`` already
counts, so Settings / Data can show which card asks for what and how often it gets a stale answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ...core import calendar
from .. import ops, read
from .catalog import NEEDS, TICKER, Need

FRESH, STALE, MISSING = "fresh", "stale", "missing"
NEVER = "never"
#: How far back the session walk looks before it gives up and reports the lag as this many sessions.
MAX_LAG_WALK = 400


class UnknownNeed(KeyError):
    """A need name the catalog does not hold."""


class FillNotSupported(NotImplementedError):
    """``fill`` other than ``"never"``: on-demand filling lands in PR C."""


@dataclass(frozen=True)
class Answer:
    """One need's answer. ``rows`` are exactly the rows the underlying ``rcdata.read`` call returned."""

    need: str
    rows: list[dict]
    state: str
    freshness: dict
    coverage: dict
    provenance: list[dict]
    status: str
    fill: dict | None = None

    @property
    def ok(self) -> bool:
        """True when the store answered inside the need's freshness window."""
        return self.state == FRESH


def _latest_session() -> date:
    """The market's newest complete session; the reference the lag is counted against."""
    return calendar.latest_complete_session()


def _day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def lag_sessions(as_of: date, latest: date) -> int:
    """Trading sessions between ``as_of`` and ``latest`` (0 when the rows are up to the newest session)."""
    if as_of >= latest:
        return 0
    day, lag = latest, 0
    while day > as_of and lag < MAX_LAG_WALK:
        # ``previous_trading_day`` returns the latest trading day **on or before** its argument, so step a day first.
        day = calendar.previous_trading_day(day - timedelta(days=1))
        lag += 1
    return lag


def _asked(need: Need, params: dict) -> list[str] | None:
    """The things the caller asked for, normalized as the reader normalizes them; None when the need takes no list."""
    if need.ask_param is None:
        return None
    value = params.get(need.ask_param)
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    items = [str(v).strip() for v in value if str(v or "").strip()]
    if need.normalize == TICKER:
        items = [v.upper() for v in items]
    return sorted(set(items))


def _provenance(need: Need, rows: list[dict]) -> list[dict]:
    return [{"key": r.get(need.key_column), "dataset": need.dataset, "source": r.get("source"),
             "source_as_of": r.get("source_as_of"), "fetched_at": r.get("fetched_at"), "run_id": r.get("run_id")}
            for r in rows]


def get(need: str, *, fill: str = NEVER, **params) -> Answer:
    """Answer ``need`` from the store. ``params`` go to the need's ``rcdata.read`` function unchanged.

    ``fill`` must be ``"never"`` in this PR: the answer serves what the store holds and says how old it is. No
    provider is called and nothing is written, so a page render can call this without holding anything up.
    """
    try:
        spec = NEEDS[need]
    except KeyError:
        raise UnknownNeed(f"unknown need {need!r}: one of {', '.join(sorted(NEEDS))}") from None
    if fill != NEVER:
        raise FillNotSupported(f"fill={fill!r} is not built yet (PR C); only fill={NEVER!r} exists")

    ops.read_log.bump(f"need:{need}")
    rows = spec.reader(**params)
    rows = list(rows.values()) if isinstance(rows, dict) else list(rows)

    days = [d for d in (_day(r.get(spec.day_column)) for r in rows) if d is not None]
    as_of = max(days) if days else None
    lag = lag_sessions(as_of, _latest_session()) if as_of is not None else None
    freshness = {"as_of_session": as_of, "lag_sessions": lag, "max_lag": spec.max_lag_sessions}

    asked = _asked(spec, params)
    served_keys = {str(r.get(spec.key_column)) for r in rows if r.get(spec.key_column) is not None}
    coverage = {"asked": len(asked) if asked is not None else len(served_keys),
                "served": len(served_keys),
                "missing": sorted(set(asked) - served_keys) if asked is not None else [],
                "from_fallback": 0}

    if not rows:
        state = MISSING
    elif lag is None or lag <= spec.max_lag_sessions:
        state = FRESH
    else:
        state = STALE

    status = rows[0].get("status") if rows else read.dataset_status(spec.dataset)
    return Answer(need=need, rows=rows, state=state, freshness=freshness, coverage=coverage,
                  provenance=_provenance(spec, rows), status=str(status), fill=None)
