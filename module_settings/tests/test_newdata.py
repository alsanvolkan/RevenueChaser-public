"""PR 2 step 4: ``module_settings/newdata.py``, the data of the "New data system" section of Settings.

Everything comes from ``ops.sqlite`` (through ``rcdata.ops``), the Parquet folder listing, the backup manifest and
one read-only handle on the live serving file per call, closed before the call returns. A fixed clock throughout.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest

from RevenueChaser.core import calendar, config
from RevenueChaser.module_settings import newdata
from RevenueChaser.rcdata import caps, ops, serving
from RevenueChaser.rcdata.build import uw_spot_expiry_strike as spx

# 23:00 New York on Wednesday 28 Oct 2026: the latest complete session is that day.
NOW = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 28)


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "market")
    monkeypatch.setattr(config, "MARKET_RAW_DIR", tmp_path / "market_raw")
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", None)
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(ops, "read_log", ops.ReadLog(process="test"))
    monkeypatch.delenv(newdata.RETENTION_ENV, raising=False)
    monkeypatch.setattr(calendar, "_now", lambda: NOW)
    return tmp_path


def _card(data: dict, dataset: str) -> dict:
    return next(c for c in data["datasets"] if c["dataset"] == dataset)


def _check(dataset="uw/spot_expiry_strike", result="changed", as_of="2026-10-28", on=None, **kw):
    ops.record_check(dataset, result, source_as_of=as_of, checked_on=on or as_of, **kw)


# ── statuses ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_dataset_never_built_says_so():
    card = _card(newdata.collect(NOW), "market_series")
    assert card["status"] == "never" and card["checked_on"] is None


@pytest.mark.parametrize("result,as_of,want", [
    ("changed", "2026-10-28", "fresh"),
    ("unchanged", "2026-10-28", "unchanged"),
    ("changed", "2026-10-27", "late"),        # one session missed
    ("changed", "2026-10-26", "stale"),       # two missed
    ("failed", "2026-10-28", "stale"),        # the last check failed
])
def test_daily_status_from_the_sessions_missed(result, as_of, want):
    _check(result=result, as_of=as_of, coverage=1.0)
    assert _card(newdata.collect(NOW), "uw/spot_expiry_strike")["status"] == want


def test_coverage_under_target_is_late():
    target = newdata.COVERAGE_TARGET["uw/spot_expiry_strike"]
    _check(coverage=round(target - 0.01, 4))
    card = _card(newdata.collect(NOW), "uw/spot_expiry_strike")
    assert card["status"] == "late" and "coverage" in card["status_reason"]


def test_a_skip_after_a_good_run_keeps_the_good_runs_status_and_shows_the_skip():
    _check(coverage=1.0)
    _check(result="skipped", as_of=None, on="2026-10-28", note="skipped: paused by settings at x")
    card = _card(newdata.collect(NOW), "uw/spot_expiry_strike")
    assert card["status"] == "fresh" and card["last_note"].startswith("skipped: paused")


def test_a_built_dataset_keeps_its_status_after_many_skipped_checks():
    """Review should-fix 2: 25 ``skipped: paused`` rows after one good run (a Fetch every pass while paused) are
    more than the 20 newest the card lists; the status still comes from the good run, never "never built"."""
    _check(coverage=1.0, rows=7)
    for _ in range(25):
        _check(result="skipped", as_of=None, on="2026-10-28", note="skipped: paused by settings at x")
    card = _card(newdata.collect(NOW), "uw/spot_expiry_strike")
    assert card["status"] == "fresh", card["status_reason"]
    assert card["rows"] == 7 and card["last_note"].startswith("skipped: paused")


@pytest.mark.parametrize("cadence,age,want", [
    ("weekly", 8, "fresh"), ("weekly", 9, "late"), ("weekly", 16, "late"), ("weekly", 17, "stale"),
    ("monthly", 35, "fresh"), ("monthly", 36, "late"), ("monthly", 71, "stale"),
])
def test_weekly_and_monthly_boundaries(cadence, age, want):
    ops.schedule_set("market_series", cadence, None)
    on = (TODAY - timedelta(days=age)).isoformat()
    _check("market_series", as_of=on, on=on)
    assert _card(newdata.collect(NOW), "market_series")["status"] == want


def test_cards_come_worst_first():
    _check("uw/spot_expiry_strike", result="changed", coverage=1.0)
    _check("market_series", result="failed")
    order = [c["dataset"] for c in newdata.collect(NOW)["datasets"]]
    assert order.index("market_series") == 0                         # stale first
    assert order.index("uw/spot_expiry_strike") == len(order) - 1    # fresh last, after the never-built spine ones


# ── the checks inside the card ────────────────────────────────────────────────────────────────────────────────

def test_the_dataset_checks_are_listed_inside_its_card():
    _check(coverage=1.0, rows=10, added=2, removed=1, calls=1)
    ops.record_issue("uw/spot_expiry_strike", "fetch_failed", "2026-10-20", "HTTP 500")
    ops.record_issue("market_series", "no_rows", "x", "y")
    card = _card(newdata.collect(NOW), "uw/spot_expiry_strike")
    labels = {c["label"]: c for c in card["checks"]}
    assert labels["Last run"]["ok"] is True
    assert labels["Rows removed"]["ok"] is False and labels["Rows removed"]["detail"] == "1"
    assert labels["Open build issues"]["ok"] is False and labels["Open build issues"]["detail"] == "1"
    assert [i["kind"] for i in card["issues"]] == ["fetch_failed"]
    assert card["wrds"] == {"text": "no WRDS run yet"}


# ── backfill progress ─────────────────────────────────────────────────────────────────────────────────────────

def test_backfill_progress_from_the_parquet_folder(roots):
    for day in ("2025-01-16", "2025-01-17", "2026-10-28"):
        p = spx.partition_path(date.fromisoformat(day))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PAR1")
    (spx.partition_path(date(2025, 1, 21)).parent).mkdir(parents=True)        # a folder without its part file
    bf = _card(newdata.collect(NOW), "uw/spot_expiry_strike")["backfill"]
    expected = sum(1 for _ in calendar_sessions(spx.FIRST_SESSION, TODAY))
    assert bf["done"] == 3 and bf["expected"] == expected
    assert bf["last_partition"] == "2026-10-28" and bf["resumable"] is True
    assert bf["missing"][0] == "2025-01-21" and len(bf["missing"]) == expected - 3
    # A session that fills UW's page of 500 is paged: the days a first backfill leaves missing are mostly those, two
    # calls each (21 Mar 2025: 575 rows; 15 Oct 2026: 683), so the estimate is two calls a session, not one.
    assert bf["calls_per_partition"] == spx.CALLS_PER_SESSION_ESTIMATE == 2


def calendar_sessions(start, end):
    from RevenueChaser.rcdata.build import base
    return base.iter_sessions(start, end)


def test_the_market_series_backfill_counts_partitions_per_source(roots):
    from RevenueChaser.rcdata.build import market_series as ms
    for src, year in ((ms.SOURCES[0], 2025), (ms.SOURCES[0], 2026), (ms.SOURCES[1], 2026)):
        p = ms.partition_path(src, year)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PAR1")
    bf = _card(newdata.collect(NOW), "market_series")["backfill"]
    assert bf["done"] == 3 and bf["expected"] is None and bf["missing"] is None
    assert bf["last_partition"] == "2026"


# ── spend, backups, retention, retirement, watch lists, queue ─────────────────────────────────────────────────

def test_spend_is_today_and_this_week_per_provider_against_the_caps():
    ops.count_calls("uw", 1200)
    with ops.connect() as con:
        con.execute("INSERT INTO provider_calls_daily (provider, ny_day, calls) VALUES ('uw', '2026-10-26', 300), "
                    "('uw', '2026-10-25', 999), ('fred', '2026-10-27', 4)")
    ops.sample_uw_headers({"x-uw-daily-req-count": "1300", "x-uw-req-per-minute-remaining": "118",
                           "x-uw-token-req-limit": "100000000"})
    spend = newdata.collect(NOW)["spend"]
    uw = next(p for p in spend["providers"] if p["provider"] == "uw")
    assert (uw["today"], uw["week"], uw["cap"]) == (1200, 1500, caps.DAY_CAPS["uw"])     # the week starts Monday
    fred = next(p for p in spend["providers"] if p["provider"] == "fred")
    assert (fred["today"], fred["week"]) == (0, 4)
    assert {p["provider"] for p in spend["providers"]} >= set(caps.DAY_CAPS)
    uw_sample = spend["uw_remaining"]
    assert (uw_sample["daily_req_count"], uw_sample["daily_cap"]) == (1300, caps.DAY_CAPS["uw"])
    assert uw_sample["daily_remaining_computed"] == caps.DAY_CAPS["uw"] - 1300
    assert uw_sample["per_minute_remaining"] == 118


def _manifest(roots, files: dict):
    out = roots / "backups" / "market"
    out.mkdir(parents=True, exist_ok=True)
    (out / ".manifest.json").write_text(json.dumps({"version": 1, "files": files}), encoding="utf-8")


def test_backup_ages_and_the_35_day_warning(roots):
    old = (NOW - timedelta(days=40)).isoformat()
    new = (NOW - timedelta(days=1)).isoformat()
    _manifest(roots, {"market_20260918_000000_000000.duckdb": {"at": old, "sha256": "a"},
                      "ops_20261027_000000_000000.sqlite": {"at": new, "sha256": "b"},
                      "ops_20261001_000000_000000.sqlite": {"at": old, "sha256": "c"}})
    b = newdata.collect(NOW)["backups"]
    kinds = {k["kind"]: k for k in b["kinds"]}
    assert kinds["ops"]["age_days"] == 1 and kinds["ops"]["warn"] is False
    assert kinds["market"]["age_days"] == 40 and kinds["market"]["warn"] is True
    assert b["raw_copy"]["text"].startswith("not set")
    assert b["portable"] == {"text": "not recorded", "warn": True, "age_days": None}


def test_the_portable_ssd_date_comes_from_its_check_row():
    ops.record_check("backup/portable", "changed", checked_on=(TODAY - timedelta(days=36)).isoformat())
    p = newdata.collect(NOW)["backups"]["portable"]
    assert p["age_days"] == 36 and p["warn"] is True
    ops.record_check("backup/portable", "changed", checked_on=(TODAY - timedelta(days=2)).isoformat())
    assert newdata.collect(NOW)["backups"]["portable"]["warn"] is False


def test_the_backups_with_no_folder_say_so(monkeypatch):
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", "")
    b = newdata.collect(NOW)["backups"]
    assert b["configured"] is False and all(k["age_days"] is None and k["warn"] for k in b["kinds"])


def test_the_raw_copy_age_is_its_newest_file(roots, monkeypatch):
    copy = roots / "copy"
    (copy / "uw").mkdir(parents=True)
    f = copy / "uw" / "a.json.gz"
    f.write_bytes(b"x")
    t = (NOW - timedelta(days=3)).timestamp()
    os.utime(f, (t, t))
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", copy)
    rc = newdata.collect(NOW)["backups"]["raw_copy"]
    assert rc["age_days"] == 3 and rc["warn"] is False


def test_the_raw_retention_warning_is_on_until_a_retention_is_set(monkeypatch):
    r = newdata.collect(NOW)["retention"]
    assert r["warn"] is True and newdata.RETENTION_ENV in r["text"]
    monkeypatch.setenv(newdata.RETENTION_ENV, "400")
    r = newdata.collect(NOW)["retention"]
    assert r["warn"] is False and "400" in r["text"] and newdata.RETENTION_ENV in r["source"]
    monkeypatch.delenv(newdata.RETENTION_ENV)
    ops.schedule_set(newdata.RETENTION_ROW, "365", None)
    r = newdata.collect(NOW)["retention"]
    assert r["warn"] is False and "dataset_schedule" in r["source"]


def test_retirement_counts_reads_of_the_old_tables_in_the_last_14_days():
    ops.ensure()
    with ops.connect() as con:
        con.executemany("INSERT INTO read_log VALUES (?, ?, ?, ?)", [
            ("2026-10-27", "dashboard", "macro_series", 5), ("2026-10-20", "cli", "macro_series", 2),
            ("2026-10-01", "dashboard", "macro_series", 50), ("2026-10-27", "dashboard", "market_series", 9)])
    rows = {r["table"]: r for r in newdata.collect(NOW)["retirement"]}
    assert rows["macro_series"]["reads_14d"] == 7
    assert rows["vol_index_daily"]["reads_14d"] == 0
    assert "market_series" not in rows                                  # a new table is not being retired
    assert "RC-Earnings-Inputs" in rows["macro_series"]["writers"]


def test_watch_list_counts_schedule_state_and_the_queue():
    ops.watch_add("option", "spy", by="t")
    ops.watch_add("stock", "aapl", by="t")
    ops.watch_add("stock", "msft", by="t")
    ops.schedule_pause("market_series", by="settings")
    rid = ops.enqueue("market_series", None, by="settings")
    data = newdata.collect(NOW)
    assert {k: v["count"] for k, v in data["watchlists"].items()} == {"option": 1, "stock": 2}
    assert [r["ticker"] for r in data["watchlists"]["stock"]["rows"]] == ["AAPL", "MSFT"]
    assert data["watchlists"]["option"]["rows"][0]["added_by"] == "t"
    assert data["watchlists"]["option"]["rows"][0]["added_on"].endswith("+00:00")
    assert data["options_build"] == {"always": ["SPX"], "queued": ["SPY"]}
    assert _card(data, "market_series")["schedule"]["paused"] is True
    assert _card(data, "market_series")["schedule"]["paused_by"] == "settings"
    assert _card(data, "uw/spot_expiry_strike")["schedule"]["paused"] is False
    assert [r["id"] for r in data["queue"]["active"]] == [rid]


# ── the read-only, per-call handles ───────────────────────────────────────────────────────────────────────────

def _serving_file(rows: int, session: str = "2026-10-28", built_at: float = 1_700_000_000.0) -> None:
    path = serving.live_path("market")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("market.fixture.duckdb")
    con = duckdb.connect(str(tmp))
    try:
        con.execute("CREATE TABLE serving_schema_version (kind TEXT, version INTEGER, built_at DOUBLE, build_id TEXT)")
        con.execute("INSERT INTO serving_schema_version VALUES ('market', 1, ?, 'b')", [built_at])
        con.execute("CREATE TABLE spx_gamma_expiry_strike (session_date DATE)")
        con.execute(f"INSERT INTO spx_gamma_expiry_strike SELECT DATE '{session}' FROM range({rows})")
        con.execute("CREATE TABLE market_series (obs_date DATE)")
        con.execute("INSERT INTO market_series VALUES (DATE '2026-10-27')")
    finally:
        con.close()
    os.replace(tmp, path)


@pytest.fixture
def handles(monkeypatch):
    """Counts the handles ``newdata`` opens on a serving file and the ones it closed."""
    seen = {"opened": 0, "closed": 0}
    real = serving.open_read

    class Counted:
        def __init__(self, con):
            self._con = con

        def execute(self, *a, **k):
            return self._con.execute(*a, **k)

        def close(self):
            seen["closed"] += 1
            self._con.close()

    def counted(kind):
        con = real(kind)
        seen["opened"] += 1
        return Counted(con)
    monkeypatch.setattr(serving, "open_read", counted)
    return seen


def test_rows_and_the_newest_date_come_from_one_read_only_handle_closed_before_return(handles):
    _serving_file(3)
    data = newdata.collect(NOW)
    assert handles == {"opened": 1, "closed": 1}
    card = _card(data, "uw/spot_expiry_strike")
    assert card["table_rows"] == 3 and card["newest"] == "2026-10-28"
    assert _card(data, "market_series")["newest"] == "2026-10-27"
    assert data["verified"]["swaps"]["market"].startswith("2023-11-14")


def test_every_call_opens_its_own_handle_and_sees_a_swapped_file(handles):
    _serving_file(3)
    assert _card(newdata.collect(NOW), "uw/spot_expiry_strike")["table_rows"] == 3
    _serving_file(5)                                     # a swap between two requests
    assert _card(newdata.collect(NOW), "uw/spot_expiry_strike")["table_rows"] == 5
    assert handles == {"opened": 2, "closed": 2}


def test_no_serving_file_yet_is_no_rows_not_an_error(handles):
    card = _card(newdata.collect(NOW), "uw/spot_expiry_strike")
    assert card["table_rows"] is None and handles["opened"] == 0
    assert newdata.collect(NOW)["verified"]["swaps"] == {kind: None for kind in serving.KINDS}


def test_the_collect_never_creates_ops_sqlite_or_a_serving_file():
    newdata.collect(NOW)
    assert not ops.ops_path().exists() and not serving.live_path("market").exists()


def test_the_verified_running_line(roots):
    ops.start_run("r1", "market_series", "queue")
    ops.finish_run("r1", "ok")
    v = newdata.collect(NOW)["verified"]
    assert v["builder_last_run"]["run_id"] == "r1" and v["builder_last_run"]["status"] == "ok"
    assert v["ops_schema"] == ops.OPS_SCHEMA_VERSION


def test_an_ops_file_from_before_a_table_existed_reads_as_empty(roots):
    path = ops.ops_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(path)).close()                    # an empty file: no tables at all
    data = newdata.collect(NOW)
    assert data["queue"] == {"active": [], "finished": []} and data["verified"]["ops_schema"] is None
