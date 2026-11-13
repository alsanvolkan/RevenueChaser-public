"""``sec/13f_sets_listed``: which of the SEC's Form 13F data sets are published, read once a day while one is awaited.

A quarter of 13F holdings is closed when the SEC publishes the data set whose filing window holds the quarter's
filing deadline (owner, 12 Nov 2026, #732: "Çeyrek SEC o 3 ayı kapsayan zip dosyasini yazdiginda tam sayılsin ve
uzerinde daha degisiklik almayacak sekilde kapansin"). The data-set page (``sec13f_sets.index_url``) is read only
while such a set is awaited: from the day after the newest passed deadline until a listed set covers it (owner:
"o aralikta günde bir kere baksak yeterli"). Any other day the run asks nothing and keeps what it has. One paced SEC
request a run at most (``sec.Pacer``); nothing is downloaded, the zips stay ``sec/13f``'s backfill's.

Raw: ``raw/sec/13f_sets_listed/<as-of>.json``, the sets the page linked that day. Parquet:
``parquet/sec/13f_sets_listed/part-0.parquet``, every set ever listed with the day it was first seen. Served:
``sec13f_data_sets``. ``ownership/serve`` reads it to mark each period ``closed``.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb

from ....core import calendar
from .. import base, dirty
from . import KIND, common, sec, sec13f_sets

NAME = "sec/13f_sets_listed"
TABLE = "sec13f_data_sets"
SCHEMA = [("name", "TEXT"), ("first_day", "DATE"), ("last_day", "DATE"), ("url", "TEXT"), ("first_seen", "DATE")]
SERVED = SCHEMA


def raw_dir() -> Path:
    return base.raw_root() / "sec" / "13f_sets_listed"


def parquet_path() -> Path:
    return base.parquet_root() / "sec" / "13f_sets_listed" / "part-0.parquet"


def _quarter_ends_before(day: date, n: int = 4) -> list[date]:
    """The ``n`` calendar quarter ends before ``day``, newest first."""
    out, y, q = [], day.year, (day.month - 1) // 3          # q: the quarters of this year already ended
    while len(out) < n:
        if q == 0:
            y, q = y - 1, 4
        end = date(y + (q == 4), 1 if q == 4 else 3 * q + 1, 1) - timedelta(days=1)
        if end < day:
            out.append(end)
        q -= 1
    return out


def awaited(as_of: date, listed: list[dict]) -> date | None:
    """The filing deadline of the newest quarter whose deadline has passed by ``as_of`` and that no listed set
    covers yet (a set covers it when its window holds the deadline); None when there is none to wait for."""
    from .serve import filing_deadline
    passed = [filing_deadline(q) for q in _quarter_ends_before(as_of) if filing_deadline(q) < as_of]
    if not passed:
        return None
    deadline = max(passed)
    covered = any(s["first_day"] <= deadline <= s["last_day"] for s in listed)
    return None if covered else deadline


def _kept() -> list[dict]:
    """Every set the kept listings name, with the first day it was seen; newest name order."""
    seen: dict[str, dict] = {}
    for path in sorted(raw_dir().glob("*.json")) if raw_dir().exists() else []:
        day = date.fromisoformat(path.stem)
        for s in base.read_raw_json(path).get("sets", []):
            if s["name"] not in seen:
                seen[s["name"]] = {"name": s["name"], "first_day": date.fromisoformat(s["first_day"]),
                                   "last_day": date.fromisoformat(s["last_day"]), "url": s["url"], "first_seen": day}
    return sorted(seen.values(), key=lambda s: (s["first_day"], s["name"]))


class Sec13fSetsListed:
    name = NAME
    kind = KIND
    providers = (sec.PROVIDER,)
    tables = (TABLE,)
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True             # the first run is the server thread's, after a backup

    def default_as_of(self) -> date:
        return calendar.today_ny()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list[Path]:
        wait = awaited(as_of, _kept())
        if wait is None:
            ctx.notes.append("13F data sets: no set awaited, the page is not asked")
            return []
        sets = sec13f_sets.listed(ctx, sec.Pacer(ctx))
        rows = [{"name": n, "first_day": a.isoformat(), "last_day": b.isoformat(), "url": u}
                for n, (a, b, u) in sorted(sets.items())]
        if not any(date.fromisoformat(r["first_day"]) <= wait <= date.fromisoformat(r["last_day"]) for r in rows):
            ctx.notes.append(f"13F data sets: the set holding the {wait} deadline is not published yet")
        return [base.write_raw_json(raw_dir() / f"{as_of.isoformat()}.json",
                                    {"as_of": as_of.isoformat(), "run_id": ctx.run_id, "sets": rows})]

    def backfill(self, ctx: base.RunContext, start: date | None, end: date) -> None:
        # The page lists what is published now: a backfill is one run.
        self.to_parquet_all(ctx, self.fetch(ctx, end))

    def to_parquet(self, ctx: base.RunContext, raw: Path) -> list[Path]:
        return self.to_parquet_all(ctx, [raw])

    def to_parquet_all(self, ctx: base.RunContext, raws: list[Path]) -> list[Path]:
        """One file of every set the kept listings name (a handful of rows), written again whole."""
        con = duckdb.connect()
        try:
            base.rows_table(con, "rows", SCHEMA, _kept())
            return [base.write_parquet(con, "SELECT * FROM rows ORDER BY first_day, name", parquet_path())]
        finally:
            con.close()

    def dirty(self, ctx: base.RunContext):
        return dirty.ALL

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        con.execute(f"DROP TABLE IF EXISTS {TABLE}")
        if parquet_path().exists():
            n = common.create_table(con, TABLE, SERVED, f"SELECT * FROM read_parquet({base.sql_path(parquet_path())})")
        else:
            n = common.create_table(con, TABLE, SERVED)
        return {TABLE: n}

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        added, removed = base.compare_with_live(con, KIND, TABLE, ["name", "first_day", "last_day"])
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (ctx.as_of or self.default_as_of()).isoformat(),
                "rows": common.count(con, TABLE), "added": added, "removed": removed}


DATASET = Sec13fSetsListed()
