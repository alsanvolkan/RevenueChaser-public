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


def test_ensure_creates_every_table_in_wal_mode_with_the_busy_timeout():
    ops.ensure()
    with ops.connect() as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == ops.BUSY_TIMEOUT_MS == 5000
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert con.execute("SELECT max(version) FROM ops_schema").fetchone()[0] == ops.OPS_SCHEMA_VERSION == 2
    assert set(ops.TABLES) <= tables
    assert ops.ops_path() == config.MARKET_DIR / "ops.sqlite"


def test_a_file_is_marked_ensured_only_after_its_tables_are_committed(monkeypatch, tmp_path):
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


def test_ensure_is_idempotent_and_refuses_a_newer_schema():
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
    with ops.connect() as con:
        rows = dict(con.execute("SELECT id, status FROM build_requests").fetchall())
    assert rows == {a: "done", b: "running"}


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


def test_record_issue_and_job_runs():
    ops.record_issue("market_series", "parse", "fred:DGS2", "bad row", run_id="r1")
    ops.start_run("r1", "market_series", "cli", {"once": True})
    ops.finish_run("r1", "ok")
    with ops.connect() as con:
        issue = con.execute("SELECT dataset, kind, key, detail, run_id, resolved_on FROM build_issues").fetchone()
        run = con.execute("SELECT dataset, trigger, pid, status, params, finished_at FROM job_runs2").fetchone()
    assert tuple(issue) == ("market_series", "parse", "fred:DGS2", "bad row", "r1", None)
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
    with ops.connect() as con:
        rows = [tuple(r) for r in con.execute("SELECT provider, ny_day, calls, failures, http_429 "
                                              "FROM provider_calls_daily ORDER BY ny_day")]
    assert rows == [("uw", "2026-10-26", 3, 1, 1), ("uw", "2026-10-27", 5, 0, 0)]


# The headers of a live UW answer (27 Oct 2026, G2): the daily count is x-uw-daily-req-count; x-uw-token-req-limit is
# not the daily cap, and the per-minute pair is a rate window.
LIVE_UW_HEADERS = {"x-uw-daily-req-count": "355", "x-uw-req-per-minute-remaining": "1000000",
                   "x-uw-req-per-minute-reset": "60000", "x-uw-token-req-limit": "100000000"}


def test_sample_uw_headers_reads_the_daily_count_and_computes_what_is_left_under_our_cap():
    from RevenueChaser.rcdata import caps
    ops.sample_uw_headers({**LIVE_UW_HEADERS, "set-cookie": "secret"}, run_id="r1")
    with ops.connect() as con:
        row = con.execute("SELECT * FROM uw_remaining").fetchone()
    assert row["run_id"] == "r1" and row["daily_req_count"] == 355
    assert row["daily_cap"] == caps.DAY_CAPS["uw"] and row["daily_remaining_computed"] == caps.DAY_CAPS["uw"] - 355
    assert row["per_minute_remaining"] == 1_000_000 and row["per_minute_reset_ms"] == 60_000
    assert row["token_req_limit"] == 100_000_000           # kept as sent, never read as the daily cap
    assert json.loads(row["raw_headers"]) == LIVE_UW_HEADERS
    ops.sample_uw_headers({}, run_id="r2")      # nothing to sample: no row
    with ops.connect() as con:
        assert con.execute("SELECT count(*) FROM uw_remaining").fetchone()[0] == 1


def test_watch_list_add_and_remove_write_history():
    ops.watch_add("option", "nvda", by="avo", note="earnings")
    ops.watch_add("option", "NVDA", by="avo")            # already there: no second history row
    ops.watch_add("stock", "AAPL", by="avo")
    assert ops.watch_list("option") == ["NVDA"]
    ops.watch_remove("option", "NVDA", by="avo")
    ops.watch_remove("option", "NVDA", by="avo")         # not there: no history row
    assert ops.watch_list("option") == [] and ops.watch_list("stock") == ["AAPL"]
    with ops.connect() as con:
        hist = [tuple(r) for r in con.execute("SELECT ticker, action, by FROM watchlist_option_changes ORDER BY at")]
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
    with ops.connect() as con:
        assert con.execute("SELECT count(*) FROM read_log").fetchone()[0] == 0     # not yet a minute
    clock[0] += 61
    log.bump("spx_gamma_expiry_strike")
    with ops.connect() as con:
        rows = dict(con.execute("SELECT table_name, reads FROM read_log WHERE process='test'").fetchall())
    assert rows == {"market_series": 2, "spx_gamma_expiry_strike": 1}
    log.bump("market_series")
    log.flush()
    with ops.connect() as con:
        assert con.execute("SELECT reads FROM read_log WHERE table_name='market_series'").fetchone()[0] == 3


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


def test_a_second_process_can_read_while_another_writes():
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


def test_a_writer_waits_for_the_other_process_instead_of_failing():
    """busy_timeout: a write while another process holds the write lock waits for it, not 'database is locked'."""
    ops.ensure()
    p = _hold_write_lock(1.0)
    try:
        ops.count_calls("uw", 1)
    finally:
        p.wait(timeout=30)
    assert ops.calls_today("uw") == 1


# ── Who may claim which request (#queueing the attended and CLI jobs) ──────────────────────────────────────────

def test_next_request_skips_the_datasets_it_was_told_to_leave_alone():
    """The hourly builder must leave an attended request queued instead of consuming it as a skip."""
    first = ops.enqueue("wrds/sec_idhist", None, by="settings")
    second = ops.enqueue("market_series", None, by="settings")
    assert ops.next_request(skip=("wrds/sec_idhist",))["id"] == second
    assert ops.next_request(skip=("wrds/sec_idhist",)) is None
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests WHERE id = ?", (first,)).fetchone()[0] == "queued"


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
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests WHERE id = ?", (rid,)).fetchone()[0] == "queued"


def test_next_request_and_queued_can_be_limited_to_one_requester():
    """A person's CLI run takes its own request, not the one a page left for the attended runner."""
    theirs = ops.enqueue("wrds/sec_idhist", None, by="settings")
    mine = ops.enqueue("wrds/sec_idhist", None, by="cli")
    assert ops.queued("wrds/sec_idhist", by="cli") is True
    assert ops.next_request("wrds/sec_idhist", by="cli")["id"] == mine
    assert ops.next_request("wrds/sec_idhist", by="cli") is None
    assert ops.queued("wrds/sec_idhist", by="cli") is False
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests WHERE id = ?", (theirs,)).fetchone()[0] == "queued"
