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
# The ownership store (PR 16).
LABELS.update({"finra/short_interest": "Short interest (FINRA)", "regsho/threshold": "Reg SHO threshold lists",
               "ibkr/shortable": "Borrow fee and shares available (IBKR file)", "sec/ftd": "Fails to deliver (SEC)",
               "sec/13f": "13F holdings (SEC data sets, then forms)", "sec/form4": "Insider transactions (Form 4)",
               "sec/13dg": "5% owners (Schedules 13D and 13G)", "ownership/serve": "13F totals per ticker and quarter"})
# The fundamentals layer (PR 12).
LABELS.update({
    "sec/companyfacts": "SEC company facts (re-read on the filing day)",
    "sec/filings": "SEC filing lists, 8-K items and report texts",
    "alphavantage/earnings_calendar": "Earnings calendar (Alpha Vantage)",
    "uw/financials": "Preliminary quarters (Unusual Whales, off by default)",
    "yfinance/estimates": "Analyst estimates (yfinance)",
    "sec/fsds": "SEC Financial Statement Data Sets (the check)",
    "wrds/fundq": "Compustat quarterly fundamentals (WRDS, the revision check)",
    "wrds/funda": "Compustat annual fundamentals (WRDS, the revision check)",
    "wrds/firm_ratio": "WRDS financial ratios (the debt check)",
    "wrds/segments": "Compustat segments and customers (WRDS)",
    "fundamentals/tables": "Fundamentals (served tables, peers, build issues)"})
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
DATE_COLUMN.update({"finra_short_interest": "settlement_date", "regsho_threshold": "list_date",
                    "ibkr_shortable": "session_date", "sec_ftd": "settlement_date", "sec13f_filings": "filed",
                    "insider_transactions": "filed", "beneficial_ownership": "filed", "sec13f_cusip_map": "period",
                    "sec13f_filer_period": "period", "sec13f_ticker_period": "period", "sec13f_periods": "period"})
DATE_COLUMN.update({"fundamental_company": "facts_as_of", "fundamental_fact": "filed", "filing_item": "filing_date",
                    "earnings_event": "source_as_of", "fundamental_revision": "new_as_of",
                    "segment_revenue": "document_date", "customer_share": "document_date",
                    "estimate_latest": "source_as_of"})
# The identity funnel (PR 5, D9): the indexes it counts, and the dataset whose build issues it lists.
SPINE_INDEXES = ("sp500", "sp400", "sp600", "ndx")
SPINE_ISSUES = "spine/security"
# The WRDS difference slot (decision 3): these check datasets' cards show their newest monthly difference report,
# the note of their newest run that read WRDS (PR 12, decided 27 Oct); every other card says no WRDS run yet.
WRDS_DIFFERENCE = ("wrds/fundq", "wrds/funda", "wrds/firm_ratio", "wrds/segments")
NO_WRDS = {"text": "no WRDS run yet"}
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
    return {"text": report["note"], "as_of": report.get("source_as_of")} if report else dict(NO_WRDS)


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

LABELS.update({"alpaca/bars": "Daily bars (Alpaca)", "alpaca/corporate_actions": "Corporate actions (Alpaca)",
               "sec/cover_shares": "Shares per class (SEC filing covers)", "yfinance/float": "Float (Yahoo)",
               "uw/total_float": "Float check (Unusual Whales)",
               "wrds/prices": "Price history and actions (WRDS: Compustat, CRSP)",
               "prices/serving": "Prices store (bars, actions, factors, shares, market cap)"})
# The plan's acceptance: Tier A sessions complete >= 99% (sized against the real Tier A, 5,655 names on 28 Oct).
COVERAGE_TARGET.update({"alpaca/bars": 0.99})
DATE_COLUMN.update({"price_bar_daily": "session_date", "corporate_action": "ex_date", "adj_factor": "valid_to",
                    "shares_class": "as_of", "security_symbol": "valid_from", "market_cap_daily": "session_date",
                    "price_revision": "session_date", "wrds_price_diff": "as_of"})
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
        return {"text": "no WRDS run yet"}
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
    return {"text": f"as of {as_of}: " + "; ".join(parts), "rows": report}


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
        **_watch_section(),
    }


def _watch_section() -> dict:
    wl = _watchlists()
    return {"watchlists": wl,
            "options_build": {"always": list(OPTIONS_ALWAYS),
                              "queued": [r["ticker"] for r in wl.get("option", {}).get("rows", [])]}}
