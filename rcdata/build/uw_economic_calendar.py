"""``uw/economic_calendar``: Unusual Whales' economic calendar (this week and next), one call an hour (#643 F3).

``GET /market/economic-calendar``: rows of ``event``, ``type`` (fed-speaker, fomc or report), ``time`` (UTC text),
``forecast``, ``prev`` and ``reported_period``. The calendar is a snapshot that UW revises during the day (a forecast
filled in, a time moved), so a day is kept as the **newest** fetch of that New York day: each fetch replaces the
day's partition whole, and a revision the old nightly table lost (it kept the first write of a key) is kept here.

Raw: ``raw/uw/economic_calendar/<YYYY-MM-DD>/<HHMMSS>.json.gz`` (New York day, UTC time of the fetch): one file per
fetch, so the hourly cadence (``due.py``) is not stopped by the day's first file. Parquet:
``parquet/uw/economic_calendar/date=<YYYY-MM-DD>/part-0.parquet``. Serving: ``economic_event``, keyed
``(snapshot_date, event_ts, event, type)``; ``event_ts`` is UW's ``time`` as epoch seconds. Not named
``economic_calendar``: that is the old raw table ``needs/screen.py`` retires.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

from ...core import calendar
from . import base, partitions
from . import dirty as dirty_set

NAME = "uw/economic_calendar"
TABLE = "economic_event"
ENDPOINT = "/market/economic-calendar"
SCHEMA = [("snapshot_date", "DATE"), ("event_ts", "DOUBLE"), ("event", "TEXT"), ("type", "TEXT"),
          ("event_time", "TEXT"), ("forecast", "TEXT"), ("prev", "TEXT"), ("reported_period", "TEXT"),
          ("source", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
KEY = ("snapshot_date", "event_ts", "event", "type")
COMPARE = [c for c, _ in SCHEMA if c not in ("fetched_at", "run_id", "source_as_of")]


def day_dir(day: date) -> Path:
    return base.raw_root() / "uw" / "economic_calendar" / day.isoformat()


def partition_path(day: date) -> Path:
    return base.parquet_root() / "uw" / "economic_calendar" / f"date={day.isoformat()}" / "part-0.parquet"


def _text(value) -> str | None:
    s = None if value is None else str(value).strip()
    return s or None


def event_ts(value) -> float | None:
    """UW's UTC time text (``2026-10-16 12:30:00+00:00`` or ``...Z``) as epoch seconds; None when unreadable."""
    raw = _text(value)
    if raw is None:
        return None
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


class UWEconomicCalendar:
    name = NAME
    kind = "market"
    providers = ("uw",)
    tables = (TABLE,)
    cadence = "hourly"

    def default_as_of(self) -> date:
        return calendar.today_ny()          # the snapshot's day, not a session: the calendar changes on any day

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        ctx.check_cap("uw")
        res = ctx.uw_client().get_result(ENDPOINT, {})
        if res.status != "ok":
            raise base.SourceError(f"UW {ENDPOINT}: {res.error}")
        body = res.data
        items = body.get("data", []) if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            raise base.SourceError(f"UW {ENDPOINT}: no rows")     # two weeks of calendar are never empty
        now = time.time()
        raw = day_dir(as_of) / f"{datetime.fromtimestamp(now, timezone.utc):%H%M%S}.json.gz"
        return [base.write_raw_json(raw, {"request": {"endpoint": ENDPOINT}, "snapshot_date": as_of.isoformat(),
                                          "fetched_at": now, "run_id": ctx.run_id, "body": body})]

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        doc = base.read_raw_json(raw)
        day = date.fromisoformat(doc["snapshot_date"])
        body = doc["body"]
        items = [i for i in (body.get("data", []) if isinstance(body, dict) else body) if isinstance(i, dict)]
        rows, seen, unkeyed = [], set(), 0
        for item in items:
            ts, event, kind = event_ts(item.get("time")), _text(item.get("event")), _text(item.get("type"))
            if ts is None or event is None or kind is None:
                unkeyed += 1
                continue
            if (ts, event, kind) in seen:
                continue                    # UW lists one event twice: the first row stands
            seen.add((ts, event, kind))
            rows.append({"snapshot_date": day, "event_ts": ts, "event": event, "type": kind,
                         "event_time": _text(item.get("time")), "forecast": _text(item.get("forecast")),
                         "prev": _text(item.get("prev")), "reported_period": _text(item.get("reported_period")),
                         "source": "uw", "source_as_of": day, "fetched_at": doc["fetched_at"],
                         "run_id": doc["run_id"]})
        if unkeyed:
            ctx.issue("unkeyed", day.isoformat(), f"{unkeyed} rows without a time, an event or a type")
        if not rows:
            raise base.SourceError(f"UW {ENDPOINT} {day}: no usable row in {raw.name}")
        con = duckdb.connect()
        try:
            base.rows_table(con, "snap", SCHEMA, rows)
            # The day's newest fetch replaces it whole: an event UW dropped is gone from that day too.
            return [base.write_parquet(con, "SELECT * FROM snap ORDER BY event_ts, event, type", partition_path(day))]
        finally:
            con.close()

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        # Oldest first by file name (the fetch time), so a day's newest fetch is the one that stays.
        return [p for raw in sorted(raws, key=lambda r: (r.parent.name, r.name)) for p in self.to_parquet(ctx, raw)]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        raise base.SourceError(f"{NAME} has no history: UW serves only this week and next")

    def dirty(self, ctx: base.RunContext):
        return dirty_set.keyed(ctx, self, "date")

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        parts = {p.parent.name.split("=", 1)[1]: p
                 for p in (base.parquet_root() / "uw" / "economic_calendar").glob("date=*/part-0.parquet")}
        return partitions.load(ctx, con, NAME, TABLE, SCHEMA, KEY, parts, _where)

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = partitions.compare(ctx, con, self.kind, NAME, TABLE, COMPARE, _where)
        rows = int(con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0])
        as_of = ctx.as_of or self.default_as_of()
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": as_of.isoformat(), "rows": rows, "added": added, "removed": removed,
                "coverage": None}


def _where(key: str) -> str:
    return f"snapshot_date = DATE '{date.fromisoformat(key).isoformat()}'"


DATASET = UWEconomicCalendar()
