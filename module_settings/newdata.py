"""The data of the "New data system" section of Settings / Data health (PR 2; D6, D9, F6, F7 A).

Pure reads, one dict per call (``collect``): the builder's operational state from ``ops.sqlite`` (through the read
helpers of ``rcdata.ops``, which never create the file or a table), the Parquet folder listing for backfill progress,
the market backup's manifest, and row counts and newest dates from the live serving file. That file is opened with
``rcdata.serving.open_read`` once per call, read-only and with its path resolved then, and closed before the call
returns: never a connection kept at module level or across requests, because DuckDB's per-path instance cache would
then keep serving the build it opened after the builder swapped in a newer one.

Nothing here writes: Fetch, Backfill and Pause / Resume are requests the page sends to the ``/api/build/*`` routes.

Status of a dataset (the plan's rules): ``fresh`` (the last run changed rows) or ``unchanged`` within its cycle;
``late`` when one cycle was missed or coverage is under its target; ``stale`` when two were missed or the last check
failed; ``never`` before its first check. A daily dataset's cycle is one New York session (``core/calendar.py``),
counted from its newest ``source_as_of``; a weekly one is due within 8 days of its last check, a monthly one within
35. A ``skipped`` check (paused, day cap) does not reset the clock: the status comes from the newest other check.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from collections.abc import Mapping

from RevenueChaser.core import calendar, config
from RevenueChaser.rcdata import caps, ops, retention, serving
from RevenueChaser.rcdata.api import screen as api_screen


class _FromScreen(Mapping):
    """A map the screen reads from ``rcdata.api.screen`` and never writes.

    Every read goes back to the one source, so a card changed there is what the screen shows without a second edit,
    and setting a key raises: that is the point. It is a ``Mapping`` and **not** a ``dict`` subclass (the review's
    m2): a subclass with empty storage answers ``[]``, ``get`` and ``{**x}`` correctly while ``.copy()`` returns
    ``{}`` and ``json.dumps`` writes ``{}`` - a label map that silently serialises as empty.
    """

    def __init__(self, source, what: str):
        self._source, self._what = source, what

    def _now(self) -> dict:
        return self._source()

    def __getitem__(self, key):
        return self._now()[key]

    def get(self, key, default=None):
        return self._now().get(key, default)

    def __contains__(self, key):
        return key in self._now()

    def __iter__(self):
        return iter(self._now())

    def __len__(self):
        return len(self._now())

    def keys(self):
        return self._now().keys()

    def values(self):
        return self._now().values()

    def items(self):
        return self._now().items()

    def __eq__(self, other):
        return self._now() == other

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None

    def __repr__(self):
        return repr(self._now())

    def copy(self) -> dict:
        """A plain dict of what the source says now, so a copy is never the empty storage of a dict subclass."""
        return dict(self._now())

    def __setitem__(self, key, value):
        raise TypeError(f"{key!r}: {self._what} lives in rcdata/api/screen.py (avo, 29 Oct 2026); set it there")

    def update(self, *args, **kwargs):
        raise TypeError(f"{self._what} lives in rcdata/api/screen.py (avo, 29 Oct 2026); set it there")


def _card_label(dataset: str) -> str:
    """The dataset's label as the screen shows it, or the dataset's own name when it has no card."""
    return api_screen.labels().get(dataset, dataset)


# The screen carries no list of its own: labels, cadence, coverage targets, the newest-date column, the status
# order and the retirement list all come from ``rcdata/api/screen.py``, beside the need catalog (avo's plan, 29 Oct
# 2026). A need or a dataset added there needs no second edit here, and a write to one of these maps is refused so
# that a second copy cannot creep back in.
LABELS = _FromScreen(api_screen.labels, "a dataset's label")
CADENCE = _FromScreen(api_screen.cadences, "a dataset's cadence")
COVERAGE_TARGET = _FromScreen(api_screen.coverage_targets, "a dataset's coverage target")
DATE_COLUMN = _FromScreen(api_screen.written_columns, "a table's newest-date column")
RETIRING = _FromScreen(lambda: api_screen.RETIRING, "the retirement list")
CYCLE_DAYS = api_screen.CYCLE_DAYS
RETIREMENT_DAYS = api_screen.RETIREMENT_DAYS
STATUS_ORDER = api_screen.STATUS_ORDER
# The ownership store (PR 16).
# The fundamentals layer (PR 12).
# Default cadence per dataset (the dataset's own ``cadence`` when it has one); a ``dataset_schedule.cadence`` of
# daily / weekly / monthly overrides it.
# Coverage targets: alarm limits as proposed, to be re-checked after two weeks of real data (decision 1, 27 Oct).
# spine/security's coverage is the share of Tier A with a CUSIP (the plan's target, >= 98%).
# The newest-date column of each serving table.
# The identity funnel (PR 5, D9): the indexes it counts, and the dataset whose build issues it lists.
SPINE_INDEXES = ("sp500", "sp400", "sp600", "ndx")
SPINE_ISSUES = "spine/security"
# The WRDS difference slot (decision 3): these check datasets' cards show their newest monthly difference report,
# the note of their newest run that read WRDS (PR 12, decided 27 Oct); every other card says no WRDS run yet.
WRDS_DIFFERENCE = ("wrds/fundq", "wrds/funda", "wrds/firm_ratio", "wrds/segments")
# S8: a report that has not run shows a dash and says so, rather than a sentence that reads like a result.
NO_WRDS = {"text": "no WRDS run yet", "produced": False}
# Old .store tables a new dataset replaces: their reads in read_log are the retirement count (read for 14 days).
BACKUP_WARN_DAYS = 35
PORTABLE_DATASET = "backup/portable"
# The raw-option retention: on as a warning until one of these says how many days raw option files are kept
# (``rcdata/retention.py``, the rule the builder's retention hook reads too).
RETENTION_ENV = retention.ENV
RETENTION_ROW = retention.ROW             # a dataset_schedule row whose cadence holds the days

# The options data (G8), in a block of its own. Coverage: the share of the names a run set out to fetch that it has
# (the list names; the earnings set and the screener batches of the evening). The IBKR year has no target: a name
# just added to the list is uncovered until the next run, which is not a late dataset.


def _datasets() -> dict:
    from RevenueChaser.rcdata.build import DATASETS
    return DATASETS


def _now(now: datetime | None) -> datetime:
    return now if now is not None else calendar._now()


def _day(value) -> date | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso_utc(epoch) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="seconds")


# ── Status ────────────────────────────────────────────────────────────────────────────────────────────────────

def _cadence(dataset: str, schedule: dict | None) -> str:
    c = (schedule or {}).get("cadence")
    if c in ("daily", "weekly", "monthly"):
        return c
    own = getattr(_datasets().get(dataset), "cadence", None)
    return own if own in ("daily", "weekly", "monthly") else CADENCE.get(dataset, "daily")


def _missed(cadence: str, check: dict, now: datetime) -> int:
    """Cycles missed since ``check``: 0 within its cycle, 1 one cycle late, 2 for two or more."""
    if cadence == "daily":
        as_of = _day(check.get("source_as_of")) or _day(check.get("checked_on"))
        if as_of is None:
            return 2
        try:
            latest = calendar.latest_complete_session(now)
            n, d = 0, as_of
            while n < 2:
                d = calendar.next_trading_day(d)
                if d > latest:
                    break
                n += 1
            return n
        except ValueError:                       # past the calendar's table: no session to count (rule C)
            return 0
    limit = CYCLE_DAYS[cadence]
    on = _day(check.get("checked_on"))
    if on is None:
        return 2
    age = (calendar._as_ny(now).date() - on).days
    return 0 if age <= limit else 1 if age <= 2 * limit else 2


def status_of(dataset: str, last: dict | None, base: dict | None, cadence: str,
              now: datetime) -> tuple[str, str, dict | None]:
    """``(status, reason, the check it is read from)`` from ``dataset``'s newest check ``last`` and its newest check
    that is not ``skipped``, ``base`` (read on its own, so any number of skips after a run cannot hide it)."""
    if last is None:
        return "never", "no check yet: never built", None
    if last["result"] == "failed":
        return "stale", "the last check failed", last
    if base is None:
        return "never", "only skipped checks so far", last
    missed = _missed(cadence, base, now)
    unit = "session" if cadence == "daily" else f"{cadence} cycle"
    if missed >= 2:
        return "stale", f"two or more {unit}s missed", base
    target = COVERAGE_TARGET.get(dataset)
    cov = base.get("coverage")
    if missed == 1:
        return "late", f"one {unit} missed", base
    if target is not None and cov is not None and cov < target:
        return "late", f"coverage {cov:.1%} under the {target:.0%} target", base
    if base["result"] == "changed":
        return "fresh", "within its cycle; the last run changed rows", base
    return "unchanged", "within its cycle; the last run changed nothing", base


def attended_command(kind: str) -> str:
    """The CLI line that runs every attended (WRDS) dataset of ``kind``: one worker and one login for all of them."""
    names = [n for n, d in _datasets().items() if d.kind == kind and getattr(d, "attended", False)]
    return f"python -m RevenueChaser.rcdata.build backfill {' '.join(names)}"


def _wrds_difference(dataset: str) -> dict:
    """The WRDS difference slot: a check dataset's newest report (a run that read WRDS, not a skip or a failure)."""
    if dataset not in WRDS_DIFFERENCE:
        return dict(NO_WRDS)                     # a copy: a caller that edits its card must not edit the constant
    report = next((c for c in ops.recent_checks(dataset, limit=24)
                   if c["result"] in ("changed", "unchanged") and c.get("note")), None)
    return ({"text": report["note"], "as_of": report.get("source_as_of"), "produced": True} if report
            else dict(NO_WRDS))


def _held(dataset: str, ds) -> str | None:
    """Why the schedule never queues ``dataset`` now (``rcdata/build/due.py``), or None: an attended dataset runs only
    from the CLI; a dataset that starts by hand waits for its first build (delta S6)."""
    from RevenueChaser.rcdata.build import due
    if getattr(ds, "attended", False):
        return due.ATTENDED_WHY
    if getattr(ds, "starts_by_hand", False) and not ops.ever_built(dataset):
        return due.BY_HAND_WHY
    return None


def _checks_list(dataset: str, check: dict | None, last: dict | None, missed: int | None, issues: int,
                 held: str | None = None) -> list[dict]:
    """The dataset's questions, one row each: ``{label, ok (True / False / None = not measured), detail}``.
    ``held`` (``_held``) replaces the next-check date when the schedule does not queue the dataset."""
    if last is None:
        return ([{"label": "Last run", "ok": None, "detail": "never run"}]
                + ([{"label": "Next check due", "ok": None, "detail": held}] if held else [])
                + [{"label": "Open build issues", "ok": issues == 0, "detail": str(issues)}])
    c = check or last
    target = COVERAGE_TARGET.get(dataset)
    cov = c.get("coverage")
    removed = c.get("removed")
    out = [
        {"label": "Last run", "ok": last["result"] != "failed",
         "detail": last["result"] + (f": {last['note']}" if last.get("note") else "")},
        {"label": "Source as of", "ok": None if missed is None else missed == 0, "detail": c.get("source_as_of") or "—"},
        {"label": "Coverage", "ok": None if cov is None else (target is None or cov >= target),
         "detail": "not measured" if cov is None else f"{cov:.1%}" + (f" (target {target:.0%})" if target else "")},
        {"label": "Rows removed", "ok": None if removed is None else removed == 0,
         "detail": "—" if removed is None else str(removed)},
        {"label": "Rows added", "ok": None, "detail": "—" if c.get("added") is None else str(c["added"])},
        {"label": "Calls", "ok": None, "detail": "—" if c.get("calls") is None else str(c["calls"])},
        {"label": "Next check due", "ok": None, "detail": held or c.get("next_check_due") or "—"},
        {"label": "Open build issues", "ok": issues == 0, "detail": str(issues)},
    ]
    return out


# ── Backfill progress (the Parquet folder listing) ────────────────────────────────────────────────────────────

def _backfill_sessions(now: datetime) -> dict:
    from RevenueChaser.rcdata.build import base
    from RevenueChaser.rcdata.build import uw_spot_expiry_strike as spx
    root = spx.partition_path(spx.FIRST_SESSION).parent.parent
    have = set()
    if root.is_dir():
        for d in root.iterdir():
            if d.name.startswith("date=") and (d / "part-0.parquet").is_file() and _day(d.name[5:]):
                have.add(_day(d.name[5:]))
    try:
        expected = list(base.iter_sessions(spx.FIRST_SESSION, calendar.latest_complete_session(now)))
    except ValueError:
        expected = []
    missing = [d.isoformat() for d in expected if d not in have]
    done = sum(1 for d in expected if d in have)
    return {"done": done, "expected": len(expected), "last_partition": max(have).isoformat() if have else None,
            "missing": missing, "resumable": bool(missing),
            "calls_per_partition": spx.CALLS_PER_SESSION_ESTIMATE,
            "from_earliest": spx.FIRST_SESSION.isoformat()}


def _backfill_years(now: datetime) -> dict:
    from RevenueChaser.rcdata.build import base
    from RevenueChaser.rcdata.build import market_series as ms
    years = []
    for src in ms.SOURCES:
        folder = base.parquet_root() / src.SOURCE / src.DATASET_DIR
        if folder.is_dir():
            years += [d.name[5:] for d in folder.iterdir()
                      if d.name.startswith("year=") and (d / "part-0.parquet").is_file()]
    return {"done": len(years), "expected": None, "last_partition": max(years) if years else None, "missing": None,
            "resumable": True, "calls_per_partition": None, "from_earliest": None}


BACKFILL = {"uw/spot_expiry_strike": _backfill_sessions, "market_series": _backfill_years}


# ── The prices store (PR 7, G2) ───────────────────────────────────────────────────────────────────────────────

# The plan's acceptance: Tier A sessions complete >= 99% (sized against the real Tier A, 5,655 names on 28 Oct).
# An attended dataset's own command, where it is not its kind's (``attended_command``).
ATTENDED_COMMANDS = {"wrds/prices": "python -m RevenueChaser.rcdata.build backfill wrds/prices"}
# The rows of the WRDS difference report (``wrds_price_diff``), as the card words them.
WRDS_REPORT_LABELS = {"crsp_vs_compustat": "CRSP vs Compustat closes", "compustat_vs_alpaca": "Compustat vs Alpaca closes",
                      "crsp_vs_alpaca": "CRSP vs Alpaca closes", "compustat_returns": "Compustat returns vs the store's",
                      "crsp_returns": "CRSP returns vs the store's"}


def _backfill_dates(*parts: str):
    """A dataset kept in ``date=`` partitions: how many there are and the newest (no expected count)."""
    def view(now: datetime) -> dict:
        from RevenueChaser.rcdata.build import base
        folder = base.parquet_root().joinpath(*parts)
        days = sorted(d.name[5:] for d in folder.iterdir()
                      if d.name.startswith("date=") and (d / "part-0.parquet").is_file()) if folder.is_dir() else []
        return {"done": len(days), "expected": None, "last_partition": days[-1] if days else None, "missing": None,
                "resumable": True, "calls_per_partition": None, "from_earliest": None}
    return view


def _backfill_wrds_prices(now: datetime) -> dict:
    """The WRDS history's year partitions (``wrds_history``): one query each, newest year first."""
    from RevenueChaser.rcdata.build.prices import wrds_history as wh
    today = calendar._as_ny(now).date()
    plan = [(t, y) for t in wh.YEARLY for y in wh.years(t, today)]
    have = [(t, y) for t, y in plan if wh.partition_path(t, y).is_file()]
    newest = max(have, key=lambda p: (p[1], -wh.YEARLY.index(p[0])), default=None)
    return {"done": len(have), "expected": len(plan), "last_partition": f"{newest[0]} {newest[1]}" if newest else None,
            "missing": [f"{t} {y}" for t, y in plan if (t, y) not in have], "resumable": len(have) < len(plan),
            "calls_per_partition": 1, "from_earliest": f"{wh.START_YEAR}-01-01"}


BACKFILL.update({"alpaca/bars": _backfill_dates("alpaca", "bars"), "wrds/prices": _backfill_wrds_prices})


def _wrds_slot(dataset: str, facts: dict) -> dict:
    """The card's "WRDS difference" line: the monthly report of ``wrds/prices`` (read with the serving facts), or
    "no WRDS run yet"."""
    report = (facts.get("wrds_price_diff") or {}).get("report") if dataset == "wrds/prices" else None
    as_of = max((r["as_of"] for r in report or [] if r.get("as_of")), default=None)
    if not report or as_of is None:
        return dict(NO_WRDS)                     # a copy, as above: a card's slot is the card's own
    parts = []
    for r in sorted(report, key=lambda r: list(WRDS_REPORT_LABELS).index(r["comparison"])
                    if r["comparison"] in WRDS_REPORT_LABELS else 99):
        label = WRDS_REPORT_LABELS.get(r["comparison"], r["comparison"])
        returns = r["comparison"].endswith("_returns")
        differs, worst = (r.get("return_differs"), r.get("max_return_diff")) if returns else \
            (r.get("close_differs"), r.get("max_close_diff"))
        if not r.get("sessions"):
            parts.append(f"{label}: no common session")
            continue
        parts.append(f"{label}: {r['sessions']:,} sessions, {differs or 0:,} over 1 bp"
                     + (f" (max {worst:.3%})" if differs and worst is not None else ""))
    return {"text": f"as of {as_of}: " + "; ".join(parts), "rows": report, "produced": True}


# ── The serving file: one read-only handle per call ───────────────────────────────────────────────────────────

#: How many example rows a card shows (A16). Five: enough to see what a row looks like, few enough to read on a
#: phone and to cost nothing to fetch.
SAMPLE_ROWS = 5
#: The cases in the data that look like a bug and are not (K13). One line each, shown beside that table's rows.
SAMPLE_NOTES: dict[str, tuple[str, ...]] = {
    "security_symbol": ("HEI and HEI-A are two share classes of one issuer: the symbol is not the key, the "
                        "security is, and only one class carries the index membership.",),
    "security": ("An OTC name can have no CUSIP at all, so it has no row here and is not missing data.",),
    "price_bar_daily": ("A bar after a split is the adjusted price: the factor comes from adj_factor, not from "
                        "the bar.",
                        "A session with no bar for a name is a halt or a name not yet listed, not a gap in the "
                        "fetch."),
    "index_membership": ("A second share class does not inherit the first class's membership.",),
    "adj_factor": ("A factor's day is the day it stops applying (valid_to), which is why the card dates it on "
                   "that column.",),
    "fundamental_fact": ("A fact's own day is the period it covers; the card dates the table by when it was "
                         "filed, which is a different column on purpose.",),
    "wrds_price_diff": ("A check that answers \"failed\" ran and could not compare, which is not the same as a "
                        "check that found a difference.",),
}
_SAMPLE_CACHE: dict[str, tuple[str, dict | None]] = {}


def clear_sample_cache() -> None:
    _SAMPLE_CACHE.clear()


def _sample_hour(now: datetime) -> str:
    """The cache key: the hour the page was read. A sample is a picture of the store, not a live number."""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _sample_key(table: str, now: datetime) -> str:
    """The cache key: the table, the hour, and the serving file it was read from with its last write.

    The file belongs in the key for two reasons. A swap mid-hour replaces the rows, and a sample from before it is
    then wrong rather than merely old. And the store root moves between runs (every test gets its own), so a key of
    table and hour alone serves one test's rows to the next.
    """
    from RevenueChaser.rcdata import read
    kind = read.KIND_OF_TABLE.get(table)
    path = serving.live_path(kind) if kind is not None else None
    try:
        stamp = path.stat().st_mtime_ns if path is not None else None
    except OSError:
        stamp = None                           # not built yet: that is a key of its own
    return f"{table}|{_sample_hour(now)}|{path}|{stamp}"


def _cached_sample(table: str, now: datetime, read):
    """``read(table)`` at most once an hour per table and serving file (A16's hourly cache)."""
    key = _sample_key(table, now)
    hit = _SAMPLE_CACHE.get(table)
    if hit is not None and hit[0] == key:
        return hit[1]
    value = read(table)
    _SAMPLE_CACHE[table] = (key, value)
    return value


def _read_sample(con, table: str, day_column: str | None) -> dict:
    """The newest ``SAMPLE_ROWS`` rows of one table, through the read-only connection already open.

    One SELECT and nothing else: this runs on a page render, against the live store, and must not write.
    """
    order = f' ORDER BY "{day_column}" DESC' if day_column else ""
    cur = con.execute(f'SELECT * FROM "{table}"{order} LIMIT {SAMPLE_ROWS}')
    columns = [{"name": d[0], "type": str(d[1])} for d in cur.description]
    rows = [[v.isoformat() if isinstance(v, (date, datetime)) else v for v in r] for r in cur.fetchall()]
    return {"columns": columns, "rows": rows}


def _sample_block(table: str, sample: dict | None) -> dict:
    """One card's example block. Without a sample the kind's file has never been built, so the declared schema
    stands in for the rows: "never built" and "built and empty" must not read the same (``rcdata/scaffold.py``)."""
    from RevenueChaser.rcdata import scaffold
    from RevenueChaser.rcdata import read
    out = {"table": table, "dataset": read.DATASET_OF_TABLE.get(table), "notes": SAMPLE_NOTES.get(table, ())}
    if sample is not None:
        return {**out, "built": True, "columns": sample["columns"], "rows": sample["rows"]}
    declared = next((cols for kind in scaffold.SCHEMA_OF_KIND
                     for name, cols in scaffold.SCHEMA_OF_KIND[kind].items() if name == table), [])
    return {**out, "built": False, "rows": [],
            "columns": [{"name": name, "type": str(kind)} for name, kind in declared]}


def _serving_facts(now: datetime | None = None) -> tuple[dict[str, dict], dict[str, str | None], dict | None]:
    """``({table: {rows, newest, sample}}, {kind: last swap}, the spine's funnel counts or None)``, from one
    ``open_read`` per kind, closed before return.

    ``sample`` is the table's newest five rows (A16), read through this same connection and cached by the hour, so
    a page render costs one read per table per hour rather than one per render. Without ``now`` no sample is read
    at all, which is what a caller that only wants the counts asks for.
    """
    tables: dict[str, dict] = {}
    swaps: dict[str, str | None] = {}
    funnel: dict | None = None
    for kind in serving.KINDS:
        swaps[kind] = None
        try:
            con = serving.open_read(kind)
        except serving.ServingFileMissing:
            continue
        try:
            names = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
            if serving.VERSION_TABLE in names:
                row = con.execute(f"SELECT max(built_at) FROM {serving.VERSION_TABLE}").fetchone()
                swaps[kind] = _iso_utc(row[0]) if row and row[0] is not None else None
            for table, col in DATE_COLUMN.items():
                if table not in names:
                    continue
                rows, newest = con.execute(f'SELECT count(*), max("{col}") FROM "{table}"').fetchone()
                tables[table] = {"rows": int(rows), "newest": newest.isoformat() if newest else None}
                if now is not None:
                    tables[table]["sample"] = _cached_sample(
                        table, now, lambda name, c=con, column=col: _read_sample(c, name, column))
            if kind == "spine" and {"security", "index_membership"} <= names:
                funnel = _spine_counts(con)
            if kind == "prices" and "wrds_price_diff" in names:
                cur = con.execute("SELECT * FROM wrds_price_diff")
                cols = [d[0] for d in cur.description]
                tables["wrds_price_diff"]["report"] = [
                    {k: (v.isoformat() if isinstance(v, date) else v) for k, v in zip(cols, r)}
                    for r in cur.fetchall()]
        finally:
            con.close()
    return tables, swaps, funnel


def _spine_counts(con) -> dict:
    """The identity funnel's counts from the spine's ``security`` and ``index_membership`` (one handle, the caller's)."""
    members = ("SELECT ticker FROM index_membership WHERE source = 'funds/index_holdings' AND valid_to IS NULL "
               f"AND index_slug IN ({', '.join(repr(s) for s in SPINE_INDEXES)})")
    row = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE tier IN ('A', 'B')), count(*) FILTER (WHERE tier = 'A'), "
        "count(*) FILTER (WHERE tier = 'A' AND cusip IS NOT NULL), "
        "count(*) FILTER (WHERE tier = 'A' AND figi IS NOT NULL), "
        "count(*) FILTER (WHERE tier = 'A' AND gsubind IS NOT NULL), "
        f"count(*) FILTER (WHERE tier = 'A' AND ticker IN ({members})), count(*) FILTER (WHERE tier = 'C'), "
        "count(*) FILTER (WHERE tier = 'D'), count(*) FILTER (WHERE tier = 'B'), max(source_as_of) FROM security"
    ).fetchone()
    per_index = dict(con.execute(
        "SELECT index_slug, count(DISTINCT ticker) FROM index_membership WHERE source = 'funds/index_holdings' "
        "AND valid_to IS NULL AND ticker IS NOT NULL GROUP BY index_slug").fetchall())
    keys = ("sec", "listed", "tier_a", "cusip", "figi", "gics", "index", "otc", "no_exchange", "excluded_type")
    out = dict(zip(keys, (int(v or 0) for v in row[:10])))
    out["as_of"] = row[10].isoformat() if row[10] else None
    out["indexes"] = {s: int(per_index.get(s, 0)) for s in SPINE_INDEXES}
    return out


def _universe_source() -> dict:
    """Which universe the boards read (``core.universe.source_state``): the flag and its fallback, if any."""
    from RevenueChaser.core import universe
    return universe.source_state()


def _identity(counts: dict | None) -> dict:
    """The identity funnel card (PR 5, D9): the nested steps SEC file -> listed -> Tier A, then four rows each out of
    Tier A, the drops between, the members per index, and ``spine/security``'s open build issues by kind."""
    n_issues, newest = ops.open_issues(SPINE_ISSUES, limit=10)
    kinds = ops.open_issue_kinds(SPINE_ISSUES)
    issues = {"issues_open": n_issues, "issue_kinds": kinds,
              "issues": [{"kind": i["kind"], "key": i["key"], "detail": i["detail"], "found_on": i["found_on"]}
                         for i in newest]}
    if counts is None:
        return {"steps": [], "drops": [], "indexes": {}, "as_of": None, "note": "the spine has not been built yet",
                **issues}
    c = counts
    steps = [{"key": "sec", "label": "In the SEC ticker and exchange file", "count": c["sec"], "out_of": None},
             {"key": "listed", "label": "Listed on Nasdaq, NYSE or Cboe", "count": c["listed"], "out_of": c["sec"]},
             {"key": "tier_a", "label": "Tier A (not a fund, SPAC, preferred, warrant or unit)", "count": c["tier_a"],
              "out_of": c["listed"]}]
    for key, label in (("cusip", "With a CUSIP"), ("figi", "With a FIGI"), ("gics", "With a GICS classification"),
                       ("index", "In the S&P 500, 400, 600 or Nasdaq-100")):
        steps.append({"key": key, "label": label, "count": c[key], "out_of": c["tier_a"]})
    drops = [{"key": "otc", "label": "Over the counter (Tier C)", "count": c["otc"]},
             {"key": "no_exchange", "label": "No exchange in the file (Tier D)", "count": c["no_exchange"]},
             {"key": "excluded_type", "label": "Listed, but an excluded security type (Tier B)",
              "count": c["excluded_type"]}]
    return {"steps": steps, "drops": drops, "indexes": c["indexes"], "as_of": c["as_of"], "note": None, **issues}


# ── Page-level blocks ─────────────────────────────────────────────────────────────────────────────────────────

def _spend(now: datetime) -> dict:
    today = calendar._as_ny(now).date()
    monday = today - timedelta(days=today.weekday())
    rows = ops.calls_by_day(monday.isoformat(), today.isoformat())
    providers = sorted(set(caps.DAY_CAPS) | {r["provider"] for r in rows})
    out = []
    for p in providers:
        mine = [r for r in rows if r["provider"] == p]
        out.append({"provider": p, "cap": caps.cap_for(p),
                    "today": sum(r["calls"] for r in mine if r["ny_day"] == today.isoformat()),
                    "week": sum(r["calls"] for r in mine),
                    "failures_today": sum(r["failures"] for r in mine if r["ny_day"] == today.isoformat()),
                    "http_429_today": sum(r["http_429"] for r in mine if r["ny_day"] == today.isoformat())})
    sample = ops.latest_uw_sample()
    if sample:
        sample = {**sample, "sampled_at": _iso_utc(sample["sampled_at"])}
    return {"day": today.isoformat(), "week_from": monday.isoformat(), "providers": out, "uw_remaining": sample}


def _age_days(when: datetime | None, now: datetime) -> int | None:
    return None if when is None else max(0, (now - when).days)


def _parse_utc(text) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _backups(now: datetime) -> dict:
    from RevenueChaser.rcdata import backup
    folder = (config.RAW_STORE_BACKUP_DIR or "").strip()
    files: dict = {}
    if folder:
        try:
            files = json.loads((Path(folder) / backup.SUBDIR / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
            files = files.get("files", {}) if isinstance(files, dict) else {}
        except (OSError, ValueError):
            files = {}
    kinds = []
    for kind in ("ops", *serving.KINDS):
        stamps = [_parse_utc(v.get("at")) for name, v in files.items()
                  if name.startswith(f"{kind}_") and isinstance(v, dict)]
        newest = max((s for s in stamps if s), default=None)
        age = _age_days(newest, now)
        kinds.append({"kind": kind, "newest": newest.isoformat(timespec="seconds") if newest else None,
                      "age_days": age, "warn": age is None or age > BACKUP_WARN_DAYS})
    return {"configured": bool(folder), "kinds": kinds, "raw_copy": _raw_copy(now), "portable": _portable(now),
            "warn_days": BACKUP_WARN_DAYS}


def _raw_copy(now: datetime) -> dict:
    target = config.MARKET_RAW_COPY_DIR
    if target is None:
        return {"text": "not set: RC_MARKET_RAW_COPY_DIR is empty, so no raw copy is made", "warn": True,
                "age_days": None}
    newest = None
    for dirpath, _dirs, names in os.walk(target):
        for n in names:
            if n.endswith(".tmp"):
                continue
            try:
                m = os.stat(os.path.join(dirpath, n)).st_mtime
            except OSError:
                continue
            newest = m if newest is None or m > newest else newest
    if newest is None:
        return {"text": "no file copied yet", "warn": True, "age_days": None}
    age = _age_days(datetime.fromtimestamp(newest, timezone.utc), now)
    return {"text": f"newest copied raw file {age} d old", "warn": age > BACKUP_WARN_DAYS, "age_days": age}


def _portable(now: datetime) -> dict:
    rows = ops.recent_checks(PORTABLE_DATASET, limit=1)
    on = _day(rows[0]["checked_on"]) if rows else None
    if on is None:
        return {"text": "not recorded", "warn": True, "age_days": None}
    age = (calendar._as_ny(now).date() - on).days
    return {"text": f"recorded {on.isoformat()}", "warn": age > BACKUP_WARN_DAYS, "age_days": age}


def _retention() -> dict:
    days, where = retention.days()
    if days is not None:
        return {"warn": False, "text": f"raw option files are kept {days} days", "source": where}
    return {"warn": True, "source": None,
            "text": f"raw option files are kept without a limit: no {RETENTION_ENV} and no dataset_schedule row "
                    f"{RETENTION_ROW!r} sets their retention in days"}


def _retirement(now: datetime) -> list[dict]:
    from RevenueChaser.core import automations
    since = (calendar._as_ny(now).date() - timedelta(days=RETIREMENT_DAYS - 1)).isoformat()
    reads = ops.reads_since(since, RETIRING)
    return [{"table": t, "replaced_by": RETIRING[t], "reads_14d": reads.get(t, 0),
             "writers": [e.name for e in automations.REGISTRY if t in e.tables_written]} for t in RETIRING]


# ── The section ───────────────────────────────────────────────────────────────────────────────────────────────

def _cost(dataset: str, backfill_view: dict | None, used_today: dict[str, int] | None = None) -> dict:
    """What one run of this dataset costs, for the line beside its buttons (M3, A22).

    The call estimate is the backfill view's own ``calls_per_partition`` where it has one, and the provider is the
    first part of the dataset's name (``uw/option_list`` is UW's; ``market_series`` has no provider of its own,
    because several answer it). ``remaining`` is the day cap less what the provider has already sent today, and is
    None for a provider with no cap.

    The day's calls are handed in, from the spend section this collect has already read, rather than read again
    here: ``caps.at_cap`` would open ops.sqlite, and a page render must never create or migrate it
    (``test_the_collect_never_creates_ops_sqlite_or_a_serving_file``).
    """
    provider = dataset.split("/")[0] if "/" in dataset else None
    cap = caps.cap_for(provider) if provider else None
    used = (used_today or {}).get(provider, 0) if provider else 0
    return {"provider": provider, "calls_per_partition": (backfill_view or {}).get("calls_per_partition"),
            "remaining": None if cap is None else max(0, cap - used), "cap": cap}


# ── The cross-check table (A17-B, Karar 32-B) ────────────────────────────────────────────────────────────────

#: The one comparison of the WRDS report the prices row shows: our closes against CRSP's, which is the field's
#: second source. The other rows of that report are on the WRDS card itself.
CROSS_COMPARISON = "crsp_vs_alpaca"
CROSS_SECOND_SOURCE = "WRDS (CRSP and Compustat)"
#: Karar 32-B (avo, 29 Oct 2026): a real second source is looked for in two fields and no more.
CROSS_FIELDS = ("prices", "interest rates")
NO_SECOND_SOURCE = ("no second source is stored for this field yet: FRED is the only one that answers for it, so "
                    "a cross-check would compare the series with itself")
NO_REPORT_YET = "the monthly WRDS comparison has not run yet, so there is nothing to compare against"
#: Why the prices check is not a button press away: its second source is WRDS, which needs a person to log in and
#: confirm Duo, so the run is attended and the queue cannot start it (``rcdata/build``'s ``attended`` flag). avo's
#: A17-B allows a press to ask the live source; this is the field where that cannot apply, and saying so is better
#: than a button that queues a run nothing will pick up.
CROSS_ATTENDED = ("this check is attended: WRDS asks for a login and Duo, so it runs from a terminal on the "
                  "server and the queue cannot start it")


def _diff_rows() -> list[dict]:
    """The stored WRDS comparison, read once. Its own function so a test can stand in for the store."""
    from RevenueChaser.rcdata import read
    return read.price_source_diff(comparison=CROSS_COMPARISON)


def cross_check(now: datetime, facts: dict, used_today: dict[str, int]) -> dict:
    """The cross-check table: per field, what the second source said and what a fresh check would cost.

    This reads the **stored** report and nothing else. The check itself runs only when the reader presses the
    button, and then through the queue (``/api/build/request/<dataset>``), never by this page calling a provider:
    that is what makes avo's choice of A17-B safe.
    """
    newest = next(iter(_diff_rows()), None)
    prices = {"field": "prices", "need": "prices.wrds_diff", "second_source": CROSS_SECOND_SOURCE,
              # Attended, so there is nothing for the button to queue; the command is shown instead.
              "dataset": None, "attended": True, "command": ATTENDED_COMMANDS.get("wrds/prices"),
              "calls": None, "remaining": _remaining("wrds", used_today), "as_of": None, "checked": None,
              "differs": None, "worst": None, "why": NO_REPORT_YET, "cannot_queue": CROSS_ATTENDED}
    if newest is not None:
        worst = newest.get("max_close_diff")
        prices.update({
            "as_of": _day_text(newest.get("as_of")),
            "checked": f'{_count(newest.get("sessions"))} sessions of {_count(newest.get("securities"))} names',
            "differs": newest.get("close_differs"),
            "worst": f"{worst:.3%}" if isinstance(worst, (int, float)) else None,
            "why": None})
    rates = {"field": "interest rates", "need": "macro.dgs10", "second_source": None, "dataset": None,
             "attended": False, "command": None, "calls": None, "remaining": None, "as_of": None,
             "checked": None, "differs": None, "worst": None, "why": NO_SECOND_SOURCE,
             "cannot_queue": NO_SECOND_SOURCE}
    return {"fields": [prices, rates]}


def _remaining(provider: str, used_today: dict[str, int]) -> int | None:
    cap = caps.cap_for(provider)
    return None if cap is None else max(0, cap - (used_today or {}).get(provider, 0))


def _count(value) -> str:
    return "?" if value is None else f"{int(value):,}"


def _day_text(value) -> str | None:
    return value.isoformat() if isinstance(value, (date, datetime)) else (str(value) if value else None)


def _measured_at(now: datetime) -> str:
    """The clock the store was read at, for each card's "Measured" row (S6)."""
    return now.strftime("%H:%M")


def _silent_minutes(last_run: dict | None, now: datetime) -> int | None:
    """How many whole minutes since the builder last *started* a run, or None when that cannot be read (M7)."""
    started = (last_run or {}).get("started_at")
    if not started:
        return None
    try:
        when = datetime.fromisoformat(str(started))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((now - when).total_seconds() // 60))


def _card(dataset: str, ds, facts: dict, now: datetime, used_today: dict[str, int] | None = None) -> dict:
    newest = ops.recent_checks(dataset, limit=1)
    last = newest[0] if newest else None
    schedule = ops.read_schedule(dataset)
    cadence = _cadence(dataset, schedule)
    status, reason, base = status_of(dataset, last, ops.latest_unskipped_check(dataset), cadence, now)
    n_issues, issues = ops.open_issues(dataset, limit=10)
    missed = _missed(cadence, base, now) if base else None
    table_facts = [facts[t] for t in ds.tables if t in facts]
    c = base or last or {}
    backfill = BACKFILL.get(dataset)
    held = _held(dataset, ds)
    return {
        "dataset": dataset, "label": LABELS.get(dataset, dataset), "status": status, "status_reason": reason,
        "cadence": cadence, "checked_on": c.get("checked_on"), "source_as_of": c.get("source_as_of"),
        "next_check_due": held or c.get("next_check_due"), "rows": c.get("rows"), "added": c.get("added"),
        "removed": c.get("removed"), "calls": c.get("calls"), "coverage": c.get("coverage"),
        "coverage_target": COVERAGE_TARGET.get(dataset), "last_note": last.get("note") if last else None,
        "last_result": last["result"] if last else None,
        "table_rows": sum(f["rows"] for f in table_facts) if table_facts else None,
        "newest": max((f["newest"] for f in table_facts if f["newest"]), default=None),
        "checks": _checks_list(dataset, base, last, missed, n_issues, held),
        "issues": [{"kind": i["kind"], "key": i["key"], "detail": i["detail"], "found_on": i["found_on"]}
                   for i in issues],
        "issues_open": n_issues,
        "backfill": backfill(now) if backfill else None,
        "cost": _cost(dataset, backfill(now) if backfill else None, used_today),
        "measured_at": _measured_at(now),
        # A16: one example block per serving table this dataset writes, in the dataset's own order.
        "samples": [_sample_block(name, (facts.get(name) or {}).get("sample")) for name in ds.tables],
        "wrds": _wrds_difference(dataset) if dataset in WRDS_DIFFERENCE else _wrds_slot(dataset, facts),
        "attended": bool(getattr(ds, "attended", False)),
        "attended_command": (ATTENDED_COMMANDS.get(dataset) or attended_command(ds.kind))
                            if getattr(ds, "attended", False) else None,
        "schedule": {"paused": bool(schedule and schedule.get("paused_at") is not None),
                     "paused_by": (schedule or {}).get("paused_by"),
                     "paused_at": _iso_utc((schedule or {}).get("paused_at"))},
    }


# The options build covers SPX always, and the option watch list besides it (D2). It is built later (build-plan
# step 11); until then every name on the option list is waiting for its first build.
OPTIONS_ALWAYS = ("SPX",)


def _watchlists() -> dict:
    """Both watch lists for their cards: ``{kind: {count, rows: [{ticker, added_on, added_by, note}]}}`` (one read
    through ``rcdata.read``: the read itself never creates ops.sqlite; the read log's minute flush may)."""
    from RevenueChaser.rcdata import read
    lists = read.watchlists()
    return {k: {"count": len(rows), "rows": [{"ticker": r["ticker"], "added_on": _iso_utc(r["added_at"]),
                                              "added_by": r["added_by"], "note": r["note"]} for r in rows]}
            for k, rows in lists.items()}


def collect(now: datetime | None = None) -> dict:
    """Everything the section shows, read now. ``now`` is the clock (aware; default ``calendar``'s seam)."""
    now = _now(now)
    facts, swaps, spine_counts = _serving_facts(now)
    # Read once, used twice: the Spend card, and each dataset's cost line (M3).
    spend = _spend(now)
    used_today = {str(p["provider"]): int(p["today"] or 0) for p in spend["providers"]}
    cards = [_card(name, ds, facts, now, used_today) for name, ds in _datasets().items()]
    cards.sort(key=lambda c: (STATUS_ORDER.index(c["status"]), c["dataset"]))
    last_run = ops.last_builder_run()
    if last_run:
        last_run = {**last_run, "started_at": _iso_utc(last_run["started_at"]),
                    "finished_at": _iso_utc(last_run["finished_at"])}
    return {
        "verified": {"builder_last_run": last_run, "swaps": swaps, "ops_schema": ops.schema_version(),
                     "silent_minutes": _silent_minutes(last_run, now)},
        "datasets": cards,
        "identity": {**_identity(spine_counts), "universe_source": _universe_source()},
        "queue": ops.read_queue(finished=20),
        "spend": spend,
        "backups": _backups(now),
        "retention": _retention(),
        "retirement": _retirement(now),
        "cross_check": cross_check(now, facts, used_today),
        **_watch_section(),
    }


def _watch_section() -> dict:
    wl = _watchlists()
    return {"watchlists": wl,
            "options_build": {"always": list(OPTIONS_ALWAYS),
                              "queued": [r["ticker"] for r in wl.get("option", {}).get("rows", [])]}}
