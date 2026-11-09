"""``rcdata/ops.py`` on Redis (master plan Order 3): the same functions, the same rows, kept in Redis.

These need a Redis at ``RC_REDIS_URL`` (CI starts one as a service). Every other ops test runs on whichever backend
is configured; the parity test here runs one script on both and holds Redis to what SQLite answers.
"""
from __future__ import annotations

import os
import threading

import pytest

from RevenueChaser.core import config
from RevenueChaser.rcdata import ops

pytestmark = pytest.mark.skipif(not os.getenv("RC_REDIS_URL"), reason="needs a Redis at RC_REDIS_URL (CI runs one)")

# Wall-clock columns: two runs never write the same instant, so the parity test compares everything else.
_TIME_COLUMNS = {"requested_at", "started_at", "ended_at", "finished_at", "added_at", "at", "paused_at", "sampled_at",
                 "host"}


def _drop_times(value):
    if isinstance(value, dict):
        return {k: _drop_times(v) for k, v in value.items() if k not in _TIME_COLUMNS}
    if isinstance(value, (list, tuple)):
        return type(value)(_drop_times(v) for v in value)
    return value


def _script() -> dict:
    """Every write ops has, then every read; what each read answered and every table's rows."""
    out: dict = {}
    a = ops.enqueue("market_series", {"x": 1}, by="settings")
    out["once"] = [ops.enqueue_once("prices/serving", None, by="cli"), ops.enqueue_once("prices/serving", None, by="cli")]
    ops.enqueue("wrds/sec_idhist", None, by="settings")
    out["claimed"] = ops.next_request(skip=("wrds/sec_idhist",))
    ops.finish_request(a, "done", note="ok")
    out["claimed2"] = ops.next_request(only=("prices/serving",), by="cli")
    out["queued"] = [ops.queued("wrds/sec_idhist"), ops.queued("wrds/sec_idhist", by="cli"), ops.queued("nope")]
    ops.record_check("market_series", "changed", source_as_of="2026-10-26", rows=10, added=10, removed=0,
                     coverage=1.0, calls=3, run_id="r1", note="first")
    ops.record_check("market_series", "unchanged", rows=10, coverage=0.5, run_id="r2")
    ops.record_check("market_series", "skipped", note="paused")
    ops.record_check("prices/serving", "failed", note="boom", checked_on="2026-10-01")
    ops.record_issue("market_series", "parse", "fred:DGS2", "bad row", run_id="r1")
    out["sync1"] = ops.sync_issues("prices/serving", ["price_gap_no_action", "stale"],
                                   [("price_gap_no_action", f"T{i}", f"d{i}") for i in range(5)]
                                   + [("stale", "X", None)], run_id="r3")
    # One unfixed problem, recorded by two runs: resolving owes both rows, on either backend (#516).
    ops.record_issue("funds/etf_holdings", "source_failed", "ARKQ", "first", run_id="r3")
    ops.record_issue("funds/etf_holdings", "source_failed", "ARKQ", "again", run_id="r4")
    out["sync_dupes"] = ops.sync_issues("funds/etf_holdings", ["source_failed"], [])
    out["dupes_open"] = ops.open_issues("funds/etf_holdings", 0)[0]
    out["sync2"] = ops.sync_issues("prices/serving", ["price_gap_no_action"],
                                   [("price_gap_no_action", f"T{i}", "new detail") for i in range(2, 7)])
    # A build that did not look where an open row was found keeps it open (prices on the live copy, #446).
    out["sync_keep"] = ops.sync_issues("prices/serving", ["price_gap_no_action"],
                                       [("price_gap_no_action", "T6", "new detail")],
                                       keep=lambda kind, key: key in ("T2", "T3"))
    ops.start_run("r1", "market_series", "cli", {"once": True})
    ops.finish_run("r1", "ok")
    ops.start_run("r2", "prices/serving", "hourly")
    ops.sample_uw_headers({"x-uw-daily-req-count": "355", "x-uw-req-per-minute-remaining": "9", "other": "no"}, "r2")
    ops.count_calls("uw", 2)
    ops.count_calls("uw", 1, failures=1, http_429=1)
    ops.count_calls("fred", 4)
    log = ops.ReadLog(process="parity")
    for t in ("market_series", "market_series", "macro_series", "need:prices.bars"):
        log.bump(t)
    log.flush()
    out["watch"] = [ops.watch_add("option", "nvda", by="avo", note="earnings"), ops.watch_add("option", "NVDA", by="x"),
                    ops.watch_toggle("stock", "aapl", by="board"), ops.watch_toggle("stock", "AAPL", by="board"),
                    ops.watch_toggle("stock", "msft", by="board"), ops.watch_remove("option", "NVDA", by="avo"),
                    ops.watch_remove("option", "NVDA", by="avo"), ops.watch_replace("option", ["spy", "QQQ", ""], by="r")]
    ops.schedule_set("market_series", "daily", "2026-10-28")
    ops.schedule_pause("market_series", by="avo")
    ops.schedule_pause("prices/serving", by="avo")
    ops.schedule_resume("prices/serving")
    out["fail_orphans"] = ops.fail_orphans()
    today = ops._today()
    out["reads"] = {
        "latest_check": ops.latest_check("market_series"), "none": ops.latest_check("nothing"),
        "read_latest": [ops.read_latest_check("market_series"), ops.read_latest_check("nothing")],
        "need_reads": ops.need_reads("2000-01-01"),
        "recent": ops.recent_checks("market_series", 2), "unskipped": ops.latest_unskipped_check("market_series"),
        "good": ops.latest_good_check("market_series"), "good_none": ops.latest_good_check("prices/serving"),
        "open": ops.open_issues("prices/serving", 3), "open_all": ops.open_issues("market_series"),
        "kinds": ops.open_issue_kinds("prices/serving"), "ever": [ops.ever_built("market_series"),
                                                                   ops.ever_built("prices/serving")],
        "read_queue": ops.read_queue(1), "queue_view": ops.queue_view(5),
        "schedule": [ops.read_schedule("market_series"), ops.schedule_get("prices/serving"), ops.schedule_get("x")],
        "calls": [ops.calls_by_day("2000-01-01", today), ops.calls_today("uw"), ops.calls_today("nope")],
        "uw": ops.latest_uw_sample(), "reads": ops.reads_since("2000-01-01", ["market_series", "macro_series", "x"]),
        "last_run": ops.last_builder_run(), "watch": [ops.watch_count("option"), ops.watch_count("stock"),
                                                      ops.watch_list("option"), ops.watch_list("stock"),
                                                      ops.watch_rows("stock"), ops.watch_last_change() is not None],
        "schema": ops.schema_version(),
        # smembers has no order: sorted, so the two backends compare (#539 review).
        "running": sorted(ops.running_runs(), key=lambda r: r["run_id"]),
    }
    out["tables"] = {t: ops.rows(t) for t in ops.TABLES if t != "ops_schema"}
    return out


def test_both_backends_answer_the_same_script_with_the_same_rows(monkeypatch, tmp_path):
    on_redis = _script()
    assert ops.backend() == "redis" and not ops.ops_path().exists()
    monkeypatch.delenv(ops.REDIS_URL_ENV)
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "sqlite_market")
    assert ops.backend() == "sqlite"
    on_sqlite = _script()
    assert ops.ops_path().exists()
    for key in on_sqlite:
        assert _drop_times(on_redis[key]) == _drop_times(on_sqlite[key]), key
    assert on_redis["tables"]["build_issues"]                       # the script did write rows


def test_rows_keep_their_types_and_column_order():
    ops.record_check("d", "changed", rows=3, coverage=1, run_id="r")
    [row] = ops.rows("dataset_check")
    assert list(row) == ["id", *ops.CHECK_FIELDS]
    assert row["rows"] == 3 and isinstance(row["coverage"], float) and row["note"] is None


def test_each_market_root_is_its_own_namespace(monkeypatch, tmp_path):
    ops.enqueue("a", None, by="cli")
    ops.watch_add("stock", "AAPL", by="avo")
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "other")
    assert ops.read_queue()["active"] == [] and ops.watch_list("stock") == []


def test_two_racing_enqueue_once_calls_queue_one_request():
    got: list = []
    barrier = threading.Barrier(8)

    def click():
        barrier.wait()
        got.append(ops.enqueue_once("prices/serving", None, by="page"))
    threads = [threading.Thread(target=click) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([g for g in got if g is not None]) == 1
    assert len(ops.rows("build_requests")) == 1


def test_racing_counters_and_toggles_lose_nothing():
    barrier = threading.Barrier(6)

    def work():
        barrier.wait()
        for _ in range(10):
            ops.count_calls("uw", 1)
            ops.watch_toggle("stock", "NVDA", by="race")
    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert ops.calls_today("uw") == 60
    actions = [r["action"] for r in ops.rows("watchlist_stock_changes")]
    assert len(actions) == 60 and actions == ["add", "remove"] * 30      # each flip in turn, none lost
    assert ops.watch_list("stock") == []


def test_ids_keep_counting_after_rows_are_copied_in():
    ops.put_rows("build_requests", [{"id": 41, "dataset": "old", "requested_at": 1.0, "requested_by": "x",
                                     "status": "done"}])
    assert ops.enqueue("new", None, by="cli") == 42
    assert [r["dataset"] for r in ops.read_queue()["finished"]] == ["old"]


def test_a_newer_schema_stamp_is_refused():
    ops.ensure()
    assert ops.schema_version() == ops.OPS_SCHEMA_VERSION
    from RevenueChaser.rcdata import ops_redis
    ops_redis._client().set(ops_redis._key("ops_schema"), ops.OPS_SCHEMA_VERSION + 1)
    ops_redis._ENSURED.clear()
    with pytest.raises(ops.OpsSchemaTooNew):
        ops.enqueue("a", None, by="cli")


def test_the_reads_never_write_a_key():
    from RevenueChaser.rcdata import ops_redis
    ops.read_queue(), ops.open_issues("x"), ops.watch_rows("stock"), ops.schema_version(), ops.latest_check("x")
    assert list(ops_redis._client().scan_iter(match=ops_redis._key("*"))) == []


def test_sync_issues_keeps_open_rows_and_resolves_only_its_kinds_at_scale():
    found = [("price_gap_no_action", f"T{i:05d}", "missing") for i in range(3000)]
    assert ops.sync_issues("prices/serving", ["price_gap_no_action"], found) == (3000, 0)
    ops.record_issue("prices/serving", "other_kind", "Z", None)
    assert ops.sync_issues("prices/serving", ["price_gap_no_action"], found[:2990]) == (0, 10)
    n, newest = ops.open_issues("prices/serving", 2)
    assert n == 2991 and [r["kind"] for r in newest] == ["other_kind", "price_gap_no_action"]
    assert ops.open_issue_kinds("prices/serving") == [{"kind": "price_gap_no_action", "count": 2990},
                                                      {"kind": "other_kind", "count": 1}]


def test_a_step_that_keeps_conflicting_gives_up_as_a_busy_store(monkeypatch):
    """Review S1: the optimistic retry is bounded, with a backoff; then WatchError, which callers treat as busy."""
    from RevenueChaser.rcdata import ops_redis
    waits: list[int] = []
    monkeypatch.setattr(ops_redis, "_backoff", waits.append)              # counted, not slept
    tries = []

    def always_raced():
        tries.append(1)
        ops_redis._client().incr(ops_redis._key("read_log", "v"))       # another writer, every time
        return None, lambda p: p.ping()
    with pytest.raises(ops.BUSY_ERRORS):
        ops_redis._transaction(["read_log"], always_raced)
    assert len(tries) == ops_redis.MAX_ATTEMPTS
    assert waits == list(range(ops_redis.MAX_ATTEMPTS - 1))                # a growing pause between every two tries


def test_the_backoff_grows_and_has_jitter():
    from RevenueChaser.rcdata import ops_redis
    waits = [ops_redis._backoff_s(a) for a in range(ops_redis.MAX_ATTEMPTS)]
    assert all(0 < w <= ops_redis.BACKOFF_MAX_S for w in waits)
    assert max(waits[-5:]) > max(waits[:2])
    assert len({ops_redis._backoff_s(5) for _ in range(20)}) > 1          # jitter: two racing writers do not lock-step


def test_the_snapshot_finishes_under_a_steady_writer():
    """Review S1: 3,000 check rows and a writer every 20 ms; a WATCHed read never finished (106 tries in 15 s)."""
    import time
    from RevenueChaser.rcdata import ops_redis
    ops.put_rows("dataset_check", [{"id": i, "dataset": "d", "checked_on": "2026-11-02", "result": "changed"}
                                   for i in range(1, 3001)])
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            ops.count_calls("uw", 1)
            ops.enqueue("x", None, by="writer")
            time.sleep(0.02)
    t = threading.Thread(target=writer)
    t.start()
    try:
        time.sleep(0.1)
        t0 = time.monotonic()
        taken = ops_redis.snapshot()
        took = time.monotonic() - t0
    finally:
        stop.set()
        t.join()
    assert took < 5 and len(taken["dataset_check"]) == 3000
    ids = [r["id"] for r in taken["build_requests"]]
    assert ids == list(range(1, len(ids) + 1))                          # one moment: no request missing in between
    # The writer counts, then enqueues, in two transactions, so the snapshot may land between them: the counter
    # of that same moment is the request count or one more (the write in flight), never less and never further
    # (a read spread over the writer's 20 ms would drift by whole iterations). CI hit the gap: 5 == 4.
    assert 0 <= taken["provider_calls_daily"][0]["calls"] - len(ids) <= 1


def test_an_explicit_namespace_stays_when_the_market_root_moves(monkeypatch, tmp_path):
    """Review S2: with RC_REDIS_NAMESPACE (the server sets it) a moved or remounted root keeps its day counts."""
    import uuid
    monkeypatch.setenv(ops_redis_ns_env(), f"rc:ops:test-{uuid.uuid4().hex}")      # its own, every run
    ops.count_calls("uw", 7)
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "moved")
    assert ops.calls_today("uw") == 7


def ops_redis_ns_env() -> str:
    from RevenueChaser.rcdata import ops_redis
    return ops_redis.NAMESPACE_ENV


def test_an_empty_namespace_beside_an_ops_file_with_rows_refuses_to_start(monkeypatch):
    """Review S2: Redis switched on before the copy (or a namespace lost) must not start an empty store, whose day
    counts read 0, while ops.sqlite still holds the state. A read answers what is there; the first write refuses."""
    url = os.environ["RC_REDIS_URL"]
    monkeypatch.delenv("RC_REDIS_URL")
    ops.count_calls("uw", 5)                                   # the SQLite state of this very root
    monkeypatch.setenv("RC_REDIS_URL", url)
    assert ops.calls_today("uw") == 0
    with pytest.raises(ops.OpsNotCopied, match="ops_copy"):
        ops.count_calls("uw", 1)
    with pytest.raises(ops.OpsNotCopied):
        ops.ensure()
    from RevenueChaser.rcdata import ops_copy
    assert ops_copy.main([]) == 0                              # the copy is the way in
    ops.count_calls("uw", 1)
    assert ops.calls_today("uw") == 6


def test_a_key_part_holding_the_separator_is_refused_not_merged():
    """Review N2: ("a|b", "c") and ("a", "b|c") would share one hash and their counts would merge silently."""
    with pytest.raises(ValueError, match=r"\|"):
        ops.put_rows("read_log", [{"day": "2026-11-02", "process": "a|b", "table_name": "c", "reads": 1}])
    assert ops.rows("read_log") == []


def test_pause_cancels_the_queued_request_on_redis_too():
    """#492 PR (ii): the same answer as SQLite's ``test_pause_cancels_the_queued_request_…``."""
    running = ops.enqueue("market_series", None, by="settings")
    ops.next_request()
    q = ops.enqueue("market_series", None, by="settings")
    other = ops.enqueue("prices/serving", None, by="settings")
    assert ops.schedule_pause("market_series", by="avo") == 1
    rows = {r["id"]: r for r in ops.rows("build_requests")}
    assert [rows[i]["status"] for i in (running, q, other)] == ["running", "skipped", "queued"]
    assert rows[q]["note"].startswith("cancelled: paused by avo at ") and rows[q]["ended_at"] is not None
    assert ops.queue_view(5)["finished"][0]["id"] == q, "a cancelled row leaves the active list"
    assert ops.cancel_queued("market_series", by="avo") == 0


def test_kind_automation_on_redis_too():
    """The same answers as SQLite's ``test_kind_automation``: no row is off, the last set wins, a bad mode writes
    nothing."""
    assert ops.kind_mode("market") == "off" and ops.read_kind_modes() == {}
    ops.kind_mode_set("market", "raw", by="avo")
    ops.kind_mode_set("market", "parquet", by="settings")
    got = {k: (r["mode"], r["set_by"]) for k, r in ops.read_kind_modes().items()}
    assert got == {"market": ("parquet", "settings")}
    with pytest.raises(ValueError):
        ops.kind_mode_set("prices", "x", by="avo")
    assert ops.kind_mode_get("prices") is None


def test_a_sources_own_automation_on_redis_too():
    """#527: the same answers as SQLite's ``test_dataset_automation``: no row follows the kind, ``file`` drops the
    row, a bad mode writes nothing."""
    ops.kind_mode_set("market", "raw", by="avo")
    assert ops.dataset_mode("market_series", "market") == "raw" and ops.read_dataset_modes() == {}
    ops.dataset_mode_set("market_series", "off", by="avo")
    assert ops.dataset_mode("market_series", "market") == "off"
    assert ops.read_dataset_modes()["market_series"]["set_by"] == "avo"
    ops.dataset_mode_set("market_series", "file", by="avo")
    assert ops.read_dataset_modes() == {} and ops.dataset_mode_get("market_series") is None
    with pytest.raises(ValueError):
        ops.dataset_mode_set("market_series", "x", by="avo")
    assert ops.read_dataset_modes() == {}
