"""``get(need, **params)``: the envelope around one ``rcdata.read`` call.

What the envelope adds, and nothing else:

- ``state``: ``fresh`` | ``stale`` | ``missing`` — one word a card can show without knowing the store's shape.
- ``freshness``: the newest day the rows carry, how far behind the market that is, the need's own ``max_lag`` and
  the unit both are counted in. Sessions by default, so a Monday answer is not "stale by three" because of the
  weekend; calendar days for a need whose build has no session of its own (avo, 29 Oct 2026).
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
from .catalog import DAYS, NEEDS, SESSIONS, TICKER, Need

FRESH, STALE, MISSING = "fresh", "stale", "missing"
NEVER = "never"
#: How far back the session walk looks before it gives up and reports the lag as this many sessions.
MAX_LAG_WALK = 400


class UnknownNeed(KeyError):
    """A need name the catalog does not hold."""


class AskRequired(ValueError):
    """A need whose reader answers nothing unless something is named, called with nothing named.

    Without this the answer was ``state: missing`` on a table holding 236 series (the live verification's finding
    2, 30 Oct 2026): "no rows" is the truth about the call, not about the store, and a card cannot tell them apart.
    """


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


def reference_session() -> date:
    """The day every freshness answer is counted against: the market's newest complete session.

    One function, because the number and the day it is measured from are two halves of the same rule. The envelope
    counted the lag from here while ``core.earnings_dates`` counted it from *today*; on a trading day whose session
    is not over yet those differ by one, so a card could say ``fresh`` while the board had already dropped the same
    data (the review's I1).
    """
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


def lag_in(unit: str, as_of: date, latest: date) -> int:
    """How far ``as_of`` is behind ``latest`` in a need's own unit: trading sessions, or calendar days."""
    if unit == DAYS:
        return max(0, (latest - as_of).days)
    return lag_sessions(as_of, latest)


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


def _asked_nothing(need: Need, params: dict) -> bool:
    """True when the caller named the need's ask parameter and named nothing in it.

    avo, 30 Oct 2026: "Boş liste 'hiçbir şey' demek, 400 dönsün". The readers whose ask parameter defaults to
    ``None`` read the whole table when it is absent, but an empty list is not that default - they answer no rows
    for it (``read.identity``: ``if not wanted: return []``), and the envelope then called a full table
    ``missing``. That is finding 2's envelope again, one need at a time, reachable from any card that renders its
    URL from an emptied selection box. The ask is refused in words instead, before the reader is called.

    Not refused: a need that names its own rows (``macro.dgs10`` and the other filtered ones), because
    ``_with_needs_own_rows`` replaces the caller's value before the reader sees it; and an explicit ``None``,
    which is the reader's own default and means every row.
    """
    if need.ask_param is None or need.ask_param not in params:
        return False
    if need.filter_values or need.filter_prefix:
        return False
    if params[need.ask_param] is None:
        return False
    return not _asked(need, params)


def _provenance(need: Need, rows: list[dict]) -> list[dict]:
    return [{"key": r.get(need.key_column), "dataset": need.dataset, "source": r.get("source"),
             "source_as_of": r.get("source_as_of"), "fetched_at": r.get("fetched_at"), "run_id": r.get("run_id")}
            for r in rows]


def _with_needs_own_rows(need: Need, params: dict) -> dict:
    """``params`` with the need's own keys filled in, for a need that names its rows inside a shared table.

    ``macro.dgs10`` is the 10-year yield and nothing else, so a caller does not have to know the series id, and a
    caller cannot turn that need into another series either: the need's own keys win. A need with no filter is
    untouched, and its caller names what it wants as before.
    """
    if not need.ask_param or (not need.filter_values and not need.filter_prefix):
        return params
    out = dict(params)
    if need.filter_values:
        out[need.ask_param] = list(need.filter_values)
    else:
        out.pop(need.ask_param, None)
        out["prefix"] = need.filter_prefix
    return out


def summary(need: str, *, sample: int = 0) -> dict:
    """One need's state without naming anything: how much the store holds for it and how new that is.

    The Settings / Data screen asks this of every need; it wants row and key counts, the newest day, the last
    check's result and a few example rows, not a ticker's rows. The by-key readers cannot answer it, so the single
    query is ``rcdata.read.table_summary`` — in the module that owns the store's SQL, never here.

    The envelope is the same shape ``get`` returns, so a card reads one shape either way; ``coverage["asked"]`` is
    None because nothing was asked for. Read-only: no provider, no write, no fill.
    """
    try:
        spec = NEEDS[need]
    except KeyError:
        raise UnknownNeed(f"unknown need {need!r}: one of {', '.join(sorted(NEEDS))}") from None

    ops.read_log.bump(f"need:{need}")
    # A need that names its own rows inside a shared table counts only those (the review's C2).
    s = read.table_summary(spec.table, day_column=spec.day_column, key_column=spec.summary_key, sample=sample,
                           key_values=spec.filter_values, key_prefix=spec.filter_prefix)

    as_of = s["newest_day"]
    lag = lag_in(spec.max_lag_unit, as_of, reference_session()) if as_of is not None else None
    if s["row_count"] == 0:
        state = MISSING
    elif lag is None or lag <= spec.max_lag:
        state = FRESH
    else:
        state = STALE
    return {"need": need, "state": state,
            "freshness": {"as_of_session": as_of, "lag_sessions": lag, "max_lag": spec.max_lag,
                          "unit": spec.max_lag_unit},
            "coverage": {"asked": None, "served": s["key_count"], "missing": [], "from_fallback": 0},
            "status": str(s["status"]), "row_count": s["row_count"], "key_count": s["key_count"],
            "dataset": s["dataset"], "legacy": spec.legacy, "fill": None,
            "sample_rows": s["sample_rows"], "provenance": _provenance(spec, s["sample_rows"])}


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

    # "Nothing was asked", not "the parameter was absent": ``_asked`` answers None only for an absent parameter,
    # and an empty value normalises to ``[]``, which used to slip past and answer ``state: missing`` on a full
    # table (the delta review of #295). ``prefix`` is the reader's other way of naming rows, so it counts as an ask.
    if spec.ask_required and not _asked(spec, params) and not params.get("prefix"):
        raise AskRequired(f"{need} answers nothing unless it is asked for something: name {spec.ask_param!r} "
                          f"(or ask for the whole table's state with summary=1)")
    if _asked_nothing(spec, params):
        raise AskRequired(f"{need} was asked for nothing: {spec.ask_param!r} is there and names nothing. An empty "
                          f"ask is nothing, never every row (avo, 30 Oct 2026): leave {spec.ask_param!r} out to "
                          f"read every row, or ask for the whole table's state with summary=1")

    ops.read_log.bump(f"need:{need}")
    rows = spec.reader(**_with_needs_own_rows(spec, params))
    rows = list(rows.values()) if isinstance(rows, dict) else list(rows)

    days = [d for d in (_day(r.get(spec.day_column)) for r in rows) if d is not None]
    as_of = max(days) if days else None
    lag = lag_in(spec.max_lag_unit, as_of, reference_session()) if as_of is not None else None
    freshness = {"as_of_session": as_of, "lag_sessions": lag, "max_lag": spec.max_lag,
                 "unit": spec.max_lag_unit}

    asked = _asked(spec, params)
    served_keys = {str(r.get(spec.key_column)) for r in rows if r.get(spec.key_column) is not None}
    coverage = {"asked": len(asked) if asked is not None else len(served_keys),
                "served": len(served_keys),
                "missing": sorted(set(asked) - served_keys) if asked is not None else [],
                "from_fallback": 0}

    if not rows:
        state = MISSING
    elif lag is None or lag <= spec.max_lag:
        state = FRESH
    else:
        state = STALE

    if rows and rows[0].get("status") is not None:
        status = rows[0]["status"]
    elif rows:
        # A reader whose rows carry no status column: the dataset's own newest check, never the string "None"
        # on a card (the review's m4).
        status = read.dataset_status(spec.dataset) if spec.dataset else read.NEVER_CHECKED
    else:
        # No rows and no single owning dataset (a table several datasets write): there is no status to give.
        status = read.dataset_status(spec.dataset) if spec.dataset else read.NEVER_CHECKED
    return Answer(need=need, rows=rows, state=state, freshness=freshness, coverage=coverage,
                  provenance=_provenance(spec, rows), status=str(status), fill=None)
