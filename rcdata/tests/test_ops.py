"""``rcdata/ops.py``: the ops.sqlite tables, the queue, the check rows, the call counter, the watch lists."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from RevenueChaser.core import calendar, config
from RevenueChaser.rcdata import ops

_REPO = Path(__file__).resolve().parents[2]


def _status(request_id: int) -> str:
    return next(r["status"] for r in ops.rows("build_requests") if r["id"] == request_id)


def test_ensure_creates_every_table_in_wal_mode_with_the_busy_timeout(sqlite_ops):
    ops.ensure()
    with ops.connect() as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == ops.BUSY_TIMEOUT_MS == 5000
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert con.execute("SELECT max(version) FROM ops_schema").fetchone()[0] == ops.OPS_SCHEMA_VERSION == 2
    assert set(ops.TABLES) <= tables
    assert ops.ops_path() == config.MARKET_DIR / "ops.sqlite"


def test_a_file_is_marked_ensured_only_after_its_tables_are_committed(sqlite_ops, monkeypatch, tmp_path):
    """CI on PR 251 (28 Oct 2026): ``ensure`` marked the file done inside its own transaction, so a thread that saw
    the mark skipped ``ensure`` and read the file before the COMMIT ("no such table: watchlist_stock")."""
    seen: list[int] = []

    class Probe(set):
        def add(self, key):                                 # another connection looks, as a second thread would
            con = sqlite3.connect(key, timeout=0)
            try:
                seen.append(con.execute("SELECT count(*) FROM sqlite_master WHERE name = 'watchlist_stock'")
                            .fetchone()[0])
            finally:
                con.close()
            super().add(key)

    monkeypatch.setattr(ops, "_ENSURED", Probe())
    ops.ensure(tmp_path / "ops.sqlite")
    assert seen == [1]


def test_ensure_is_idempotent_and_refuses_a_newer_schema(sqlite_ops):
    ops.ensure()
    ops.ensure()
    with ops.connect() as con:
        assert con.execute("SELECT count(*) FROM ops_schema").fetchone()[0] == 1
        con.execute("INSERT INTO ops_schema VALUES (?, 0)", (ops.OPS_SCHEMA_VERSION + 1,))
    with pytest.raises(ops.OpsSchemaTooNew):
        ops.ensure()


def test_the_queue_is_first_in_first_out_and_marks_what_it_hands_out():
    a = ops.enqueue("market_series", {"x": 1}, by="settings")
    b = ops.enqueue("uw/spot_expiry_strike", None, by="cli")
    first = ops.next_request()
    assert first["id"] == a and first["status"] == "running" and first["params"] == {"x": 1}
    assert first["requested_by"] == "settings" and first["started_at"] is not None
    assert ops.next_request()["id"] == b
    assert ops.next_request() is None
    ops.finish_request(a, "done", note="ok")
    assert {r["id"]: r["status"] for r in ops.rows("build_requests")} == {a: "done", b: "running"}


def test_next_request_can_be_limited_to_one_dataset():
    ops.enqueue("a", None, by="cli")
    b = ops.enqueue("b", None, by="cli")
    assert ops.next_request(dataset="b")["id"] == b
    assert ops.next_request(dataset="b") is None


def test_record_check_writes_a_row_and_latest_check_reads_the_newest():
    ops.record_check("market_series", "changed", source_as_of="2026-10-26", rows=10, added=10, removed=0,
                     coverage=1.0, calls=3, run_id="r1", note="first")
    ops.record_check("market_series", "unchanged", rows=10, added=0, removed=0, calls=0, run_id="r2")
    latest = ops.latest_check("market_series")
    assert latest["result"] == "unchanged" and latest["run_id"] == "r2"
    assert latest["checked_on"] == calendar.today_ny().isoformat()
    assert ops.latest_check("nothing") is None
    with pytest.raises(ValueError):
        ops.record_check("market_series", "maybe")


def test_sync_issues_resolves_every_open_row_of_a_key_not_just_one():
    """``record_issue`` writes a row per run, so one unfixed problem becomes many rows with the same key: FRED's
    key was missing on homesrv for two days and left six ``missing_key``/``fred`` rows. Resolving has to close
    all of them -- a sync that keeps one row per key open leaves the health screen claiming a problem that is
    over, every time, for good.
    """
    for run in ("r1", "r2", "r3"):                      # three runs, one unfixed problem
        ops.record_issue("market_series", "missing_key", "fred", "no key", run_id=run)
    assert ops.open_issues("market_series", 0)[0] == 3
    added, resolved = ops.sync_issues("market_series", ["missing_key"], [])
    assert (added, resolved) == (0, 3)
    assert ops.open_issues("market_series", 0)[0] == 0


def test_prune_issues_deletes_rows_resolved_over_a_week_ago_and_never_an_open_one(monkeypatch):
    """#387: build_issues was 42,225 rows on 6 Nov, 16,155 of them resolved. avo, 8 Nov 2026 ("B"): a resolved row
    is kept seven days. An open row stays until it is resolved, however old."""
    monkeypatch.setattr(ops, "_today", lambda: "2026-10-01")
    ops.record_issue("market_series", "parse", "old-open", "still broken")
    for key in ("old", "edge"):
        ops.record_issue("market_series", "parse", key, "fixed")
    ops.sync_issues("market_series", ["parse"], [("parse", "old-open", "still broken")])   # resolves old and edge
    monkeypatch.setattr(ops, "_today", lambda: "2026-10-08")
    ops.record_issue("market_series", "parse", "recent", "fixed")
    ops.sync_issues("market_series", ["parse"], [("parse", "old-open", "still broken")])   # resolves recent
    assert ops.prune_issues() == 0                       # 2026-10-01 is exactly seven days old: kept
    monkeypatch.setattr(ops, "_today", lambda: "2026-10-09")
    assert ops.prune_issues() == 2
    left = sorted((r["key"], r["resolved_on"]) for r in ops.rows("build_issues"))
    assert left == [("old-open", None), ("recent", "2026-10-08")]
    assert ops.open_issues("market_series", 0)[0] == 1


def test_record_issue_and_job_runs():
    ops.record_issue("market_series", "parse", "fred:DGS2", "bad row", run_id="r1")
    ops.start_run("r1", "market_series", "cli", {"once": True})
    ops.finish_run("r1", "ok")
    [issue] = ops.rows("build_issues")
    [run] = ops.rows("job_runs2")
    assert tuple(issue[c] for c in ("dataset", "kind", "key", "detail", "run_id", "resolved_on")) == ("market_series", "parse", "fred:DGS2", "bad row", "r1", None)
    assert run["status"] == "ok" and run["pid"] == os.getpid() and json.loads(run["params"]) == {"once": True}
    assert run["finished_at"] is not None


def test_calls_today_sums_per_new_york_day(monkeypatch):
    # 03:00 UTC on 27 Oct is still 26 Oct in New York.
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc))
    ops.count_calls("uw", 2)
    ops.count_calls("uw", 1, failures=1, http_429=1)
    assert ops.calls_today("uw") == 3
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc))
    assert ops.calls_today("uw") == 0
    ops.count_calls("uw", 5)
    assert ops.calls_today("uw") == 5
    assert ops.calls_today("fred") == 0
    rows = [tuple(r.values()) for r in ops.rows("provider_calls_daily")]
    assert rows == [("uw", "2026-10-26", 3, 1, 1), ("uw", "2026-10-27", 5, 0, 0)]


# The headers of a live UW answer (27 Oct 2026, G2): the daily count is x-uw-daily-req-count; x-uw-token-req-limit is
# not the daily cap, and the per-minute pair is a rate window.
LIVE_UW_HEADERS = {"x-uw-daily-req-count": "355", "x-uw-req-per-minute-remaining": "1000000",
                   "x-uw-req-per-minute-reset": "60000", "x-uw-token-req-limit": "100000000"}


def test_sample_uw_headers_reads_the_daily_count_and_computes_what_is_left_under_our_cap(monkeypatch):
    from RevenueChaser.rcdata import caps
    monkeypatch.setitem(caps.DAY_CAPS, "uw", 100_000)              # UW has no cap since 2026-11-10: a test cap
    ops.sample_uw_headers({**LIVE_UW_HEADERS, "set-cookie": "secret"}, run_id="r1")
    [row] = ops.rows("uw_remaining")
    assert row["run_id"] == "r1" and row["daily_req_count"] == 355
    assert row["daily_cap"] == caps.DAY_CAPS["uw"] and row["daily_remaining_computed"] == caps.DAY_CAPS["uw"] - 355
    assert row["per_minute_remaining"] == 1_000_000 and row["per_minute_reset_ms"] == 60_000
    assert row["token_req_limit"] == 100_000_000           # kept as sent, never read as the daily cap


def test_sample_uw_headers_without_a_uw_cap_computes_nothing():
    ops.sample_uw_headers(LIVE_UW_HEADERS, run_id="r1")
    [row] = ops.rows("uw_remaining")
    assert row["daily_req_count"] == 355 and row["daily_cap"] is None and row["daily_remaining_computed"] is None
    assert json.loads(row["raw_headers"]) == LIVE_UW_HEADERS
    ops.sample_uw_headers({}, run_id="r2")      # nothing to sample: no row
    assert len(ops.rows("uw_remaining")) == 1


def test_watch_list_add_and_remove_write_history():
    ops.watch_add("option", "nvda", by="avo", note="earnings")
    ops.watch_add("option", "NVDA", by="avo")            # already there: no second history row
    ops.watch_add("stock", "AAPL", by="avo")
    assert ops.watch_list("option") == ["NVDA"]
    ops.watch_remove("option", "NVDA", by="avo")
    ops.watch_remove("option", "NVDA", by="avo")         # not there: no history row
    assert ops.watch_list("option") == [] and ops.watch_list("stock") == ["AAPL"]
    hist = [(r["ticker"], r["action"], r["by"]) for r in ops.rows("watchlist_option_changes")]
    assert hist == [("NVDA", "add", "avo"), ("NVDA", "remove", "avo")]
    with pytest.raises(ValueError):
        ops.watch_add("bond", "X", by="avo")


def test_schedule_set_pause_resume():
    ops.schedule_set("market_series", "daily", "2026-10-28")
    ops.schedule_pause("market_series", by="avo")
    s = ops.schedule_get("market_series")
    assert s["cadence"] == "daily" and s["paused_by"] == "avo" and s["paused_at"] is not None
    ops.schedule_resume("market_series")
    assert ops.schedule_get("market_series")["paused_at"] is None
    assert ops.schedule_get("nope") is None


def test_read_log_counts_in_memory_and_flushes_once_a_minute():
    ops.ensure()
    clock = [1000.0]
    log = ops.ReadLog(process="test", clock=lambda: clock[0])
    log.bump("market_series")
    log.bump("market_series")
    assert ops.rows("read_log") == []                                                # not yet a minute
    clock[0] += 61
    log.bump("spx_gamma_expiry_strike")
    rows = {r["table_name"]: r["reads"] for r in ops.rows("read_log") if r["process"] == "test"}
    assert rows == {"market_series": 2, "spx_gamma_expiry_strike": 1}
    log.bump("market_series")
    log.flush()
    assert [r["reads"] for r in ops.rows("read_log") if r["table_name"] == "market_series"] == [3]


_HOLDER = textwrap.dedent("""
    import sqlite3, sys, time
    con = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
    con.execute("BEGIN IMMEDIATE")
    con.execute("INSERT INTO build_requests (dataset, requested_at, requested_by, status) VALUES ('held', 0, 'x', 'queued')")
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
    con.execute("COMMIT")
""")


def _hold_write_lock(seconds: float) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(ops.ops_path()), str(seconds)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_a_second_process_can_read_while_another_writes(sqlite_ops):
    ops.ensure()
    ops.enqueue("before", None, by="cli")
    p = _hold_write_lock(1.5)
    try:
        t0 = time.monotonic()
        with ops.connect() as con:
            n = con.execute("SELECT count(*) FROM build_requests").fetchone()[0]
        assert n == 1 and time.monotonic() - t0 < 1.0      # WAL: the reader neither waits nor sees the open insert
    finally:
        p.wait(timeout=30)


def test_a_writer_waits_for_the_other_process_instead_of_failing(sqlite_ops):
    """busy_timeout: a write while another process holds the write lock waits for it, not 'database is locked'."""
    ops.ensure()
    p = _hold_write_lock(1.0)
    try:
        ops.count_calls("uw", 1)
    finally:
        p.wait(timeout=30)
    assert ops.calls_today("uw") == 1


def test_threads_of_one_process_queue_for_the_write_lock_instead_of_running_out_of_busy_timeout(sqlite_ops,
                                                                                                monkeypatch):
    """#522: n threads of one process (the dashboard's hourly pass and a panel click) writing at once.

    SQLite's busy handler polls with growing sleeps and no queue, so the last of n writers waits for the n - 1 before
    it plus its own idle sleeps, and on a loaded box (slow fsync) that runs past ``busy_timeout``: "database is
    locked". Here each transaction is held 80 ms and the timeout is 300 ms, so the eighth writer's turn comes ~560 ms
    in: it must wait its turn behind the others, not fail."""
    import threading
    ops.ensure()
    monkeypatch.setattr(ops, "BUSY_TIMEOUT_MS", 300)
    real = ops._watch_write

    def slow(*a, **k):
        time.sleep(0.08)                                  # a commit on a loaded disk
        return real(*a, **k)

    monkeypatch.setattr(ops, "_watch_write", slow)
    n = 8
    gate, results, errors = threading.Barrier(n), [], []

    def run():
        try:
            gate.wait(10)
            results.append(ops.watch_toggle("stock", "AAA", by="test"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [], errors[:2]
    assert sorted(results) == [False] * 4 + [True] * 4
    assert ops.watch_list("stock") == []


# ── Who may claim which request (#queueing the attended and CLI jobs) ──────────────────────────────────────────

def test_next_request_skips_the_datasets_it_was_told_to_leave_alone():
    """The hourly builder must leave an attended request queued instead of consuming it as a skip."""
    first = ops.enqueue("wrds/sec_idhist", None, by="settings")
    second = ops.enqueue("market_series", None, by="settings")
    assert ops.next_request(skip=("wrds/sec_idhist",))["id"] == second
    assert ops.next_request(skip=("wrds/sec_idhist",)) is None
    assert _status(first) == "queued"


def test_next_request_can_be_limited_to_the_datasets_it_was_given():
    """The attended runner claims only the attended datasets, whoever queued them."""
    ops.enqueue("market_series", None, by="settings")
    attended = ops.enqueue("wrds/sec_idhist", None, by="settings")
    assert ops.next_request(only=("wrds/sec_idhist",))["id"] == attended
    assert ops.next_request(only=("wrds/sec_idhist",)) is None


def test_next_request_refuses_a_skip_and_an_only_together():
    with pytest.raises(ValueError):
        ops.next_request(skip=("a",), only=("b",))


def test_an_only_with_no_name_claims_nothing():
    """A host with no attended dataset must leave every request alone, not take the whole queue."""
    rid = ops.enqueue("market_series", None, by="settings")
    assert ops.next_request(only=()) is None
    assert _status(rid) == "queued"


def test_next_request_and_queued_can_be_limited_to_one_requester():
    """A person's CLI run takes its own request, not the one a page left for the attended runner."""
    theirs = ops.enqueue("wrds/sec_idhist", None, by="settings")
    mine = ops.enqueue("wrds/sec_idhist", None, by="cli")
    assert ops.queued("wrds/sec_idhist", by="cli") is True
    assert ops.next_request("wrds/sec_idhist", by="cli")["id"] == mine
    assert ops.next_request("wrds/sec_idhist", by="cli") is None
    assert ops.queued("wrds/sec_idhist", by="cli") is False
    assert _status(theirs) == "queued"


def test_pause_cancels_the_queued_request_and_leaves_running_and_done_rows():
    """#492 PR (ii): Pause stops a dataset for real; its queued request is skipped with who and when."""
    done = ops.enqueue("market_series", None, by="settings")
    ops.next_request()
    ops.finish_request(done, "done")
    running = ops.enqueue("market_series", None, by="settings")
    ops.next_request()
    q1 = ops.enqueue("market_series", None, by="settings")
    q2 = ops.enqueue("market_series", {"backfill_from": "2026-01-02"}, by="cli")
    other = ops.enqueue("prices/serving", None, by="settings")
    assert ops.schedule_pause("market_series", by="avo") == 2
    rows = {r["id"]: r for r in ops.rows("build_requests")}
    assert [rows[i]["status"] for i in (done, running, q1, q2, other)] == ["done", "running", "skipped", "skipped",
                                                                           "queued"]
    assert rows[q1]["note"].startswith("cancelled: paused by avo at ") and rows[q1]["note"].endswith(" UTC")
    assert rows[q1]["ended_at"] is not None
    assert ops.schedule_get("market_series")["paused_by"] == "avo"
    assert ops.cancel_queued("market_series", by="avo") == 0, "nothing left to cancel"
