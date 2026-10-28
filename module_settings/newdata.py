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

from RevenueChaser.core import calendar, config
from RevenueChaser.rcdata import caps, ops, serving

LABELS = {"uw/spot_expiry_strike": "SPX expiry-strike (Unusual Whales)", "market_series": "Market series",
          "sec/company_tickers_exchange": "SEC ticker and exchange file",
          "wrds/sec_idhist": "Compustat securities and identifier history (WRDS)",
          "wrds/co_hgic": "Compustat GICS history (WRDS)",
          "wrds/idxcst_his": "Compustat index constituents (WRDS, the membership check)",
          "openfigi/map": "OpenFIGI map of the CUSIPs", "funds/index_holdings": "Index membership (ETF holdings files)",
          "spine/security": "Identity spine (security, tiers, history)"}
# Default cadence per dataset (the dataset's own ``cadence`` when it has one); a ``dataset_schedule.cadence`` of
# daily / weekly / monthly overrides it.
CADENCE = {"uw/spot_expiry_strike": "daily", "market_series": "daily"}
CYCLE_DAYS = {"weekly": 8, "monthly": 35}
# Coverage targets: alarm limits as proposed, to be re-checked after two weeks of real data (decision 1, 27 Oct).
# spine/security's coverage is the share of Tier A with a CUSIP (the plan's target, >= 98%).
COVERAGE_TARGET: dict[str, float | None] = {"uw/spot_expiry_strike": 0.95, "market_series": None,
                                            "spine/security": 0.98}
# The newest-date column of each serving table.
DATE_COLUMN = {"spx_gamma_expiry_strike": "session_date", "market_series": "obs_date", "security": "source_as_of",
               "security_history": "valid_from", "gics_history": "valid_from", "index_membership": "valid_from"}
# The identity funnel (PR 5, D9): the indexes it counts, and the dataset whose build issues it lists.
SPINE_INDEXES = ("sp500", "sp400", "sp600", "ndx")
SPINE_ISSUES = "spine/security"
ATTENDED_COMMAND = "python -m RevenueChaser.rcdata.build backfill wrds/sec_idhist wrds/co_hgic wrds/idxcst_his"
# Old .store tables a new dataset replaces: their reads in read_log are the retirement count (read for 14 days).
RETIRING = {"macro_series": "market_series", "vol_index_daily": "market_series", "cftc_cot": "market_series",
            "economic_calendar": "market_series"}
RETIREMENT_DAYS = 14
BACKUP_WARN_DAYS = 35
PORTABLE_DATASET = "backup/portable"
# The raw-option retention: on as a warning until one of these says how many days raw option files are kept.
RETENTION_ENV = "RC_RAW_RETENTION_DAYS"
RETENTION_ROW = "retention_days"          # a dataset_schedule row whose cadence holds the days
STATUS_ORDER = ("stale", "never", "late", "unchanged", "fresh")


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
            "missing": missing, "resumable": bool(missing), "calls_per_partition": 1,
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


# ── The serving file: one read-only handle per call ───────────────────────────────────────────────────────────

def _serving_facts() -> tuple[dict[str, dict], dict[str, str | None], dict | None]:
    """``({table: {rows, newest}}, {kind: last swap}, the spine's funnel counts or None)``, from one ``open_read`` per
    kind, closed before return."""
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
            if kind == "spine" and {"security", "index_membership"} <= names:
                funnel = _spine_counts(con)
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
    env = (os.getenv(RETENTION_ENV) or "").strip()
    if env.isdigit():
        return {"warn": False, "text": f"raw option files are kept {env} days", "source": f"{RETENTION_ENV}={env}"}
    row = ops.read_schedule(RETENTION_ROW)
    days = str((row or {}).get("cadence") or "").strip()
    if days.isdigit():
        return {"warn": False, "text": f"raw option files are kept {days} days",
                "source": f"dataset_schedule row {RETENTION_ROW!r}"}
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

def _card(dataset: str, ds, facts: dict, now: datetime) -> dict:
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
        "wrds": {"text": "no WRDS run yet"},
        "attended": bool(getattr(ds, "attended", False)),
        "attended_command": ATTENDED_COMMAND if getattr(ds, "attended", False) else None,
        "schedule": {"paused": bool(schedule and schedule.get("paused_at") is not None),
                     "paused_by": (schedule or {}).get("paused_by"),
                     "paused_at": _iso_utc((schedule or {}).get("paused_at"))},
    }


def collect(now: datetime | None = None) -> dict:
    """Everything the section shows, read now. ``now`` is the clock (aware; default ``calendar``'s seam)."""
    now = _now(now)
    facts, swaps, spine_counts = _serving_facts()
    cards = [_card(name, ds, facts, now) for name, ds in _datasets().items()]
    cards.sort(key=lambda c: (STATUS_ORDER.index(c["status"]), c["dataset"]))
    last_run = ops.last_builder_run()
    if last_run:
        last_run = {**last_run, "started_at": _iso_utc(last_run["started_at"]),
                    "finished_at": _iso_utc(last_run["finished_at"])}
    return {
        "verified": {"builder_last_run": last_run, "swaps": swaps, "ops_schema": ops.schema_version()},
        "datasets": cards,
        "identity": {**_identity(spine_counts), "universe_source": _universe_source()},
        "queue": ops.read_queue(finished=20),
        "spend": _spend(now),
        "backups": _backups(now),
        "retention": _retention(),
        "retirement": _retirement(now),
        "watchlists": {k: ops.watch_count(k) for k in ops.WATCH_KINDS},
    }
