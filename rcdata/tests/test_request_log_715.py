"""The per-request log (#715): one row a read, a need ask and a provider call, kept to its newest rows."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base


@pytest.fixture(params=["configured", "sqlite"])
def log(request, monkeypatch):
    """A request log of its own, on the configured ops backend (Redis in CI) and on the file."""
    if request.param == "sqlite":
        monkeypatch.delenv(ops.REDIS_URL_ENV, raising=False)
    clock = [1000.0]
    read_log = ops.ReadLog(process="test", clock=lambda: clock[0])
    monkeypatch.setattr(ops, "read_log", read_log)
    read_log.tick = lambda s: clock.__setitem__(0, clock[0] + s)
    return read_log


def _stored() -> list[tuple]:
    return [(r["kind"], r["what"], r["result"]) for r in ops.rows("request_log")]


def test_a_read_and_a_need_ask_are_one_row_each_written_by_the_flush(log):
    log.bump("market_series")
    log.bump("need:prices.bars")
    assert ops.rows("request_log") == []                              # held in memory, nothing written by a read
    log.flush()
    rows = ops.rows("request_log")
    assert [(r["kind"], r["what"], r["process"]) for r in rows] == [("read", "market_series", "test"),
                                                                     ("need", "prices.bars", "test")]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows) and all(r["at"] > 0 for r in rows)
    assert {r["table_name"] for r in ops.rows("read_log")} == {"market_series", "need:prices.bars"}   # counters as before


def test_the_minute_flush_of_the_read_log_writes_the_requests_too(log):
    log.bump("market_series")
    log.tick(61)
    log.bump("macro_series")
    assert [w for _, w, _ in _stored()] == ["market_series", "macro_series"]


def test_recent_requests_are_newest_first_above_an_id_and_limited(log):
    for name in ("a", "b", "c", "d"):
        log.bump(name)
    log.flush()
    ids = [r["id"] for r in ops.rows("request_log")]
    assert [r["what"] for r in ops.recent_requests()] == ["d", "c", "b", "a"]
    assert [r["what"] for r in ops.recent_requests(limit=2)] == ["d", "c"]
    assert [r["what"] for r in ops.recent_requests(after=ids[1])] == ["d", "c"]
    assert ops.recent_requests(after=ids[-1]) == [] and ops.recent_requests(limit=0) == []


def test_only_the_newest_rows_are_kept(log, monkeypatch):
    monkeypatch.setattr(ops, "REQUEST_LOG_KEEP", 5)
    for i in range(7):
        log.bump(f"t{i}")
    log.flush()
    for i in range(7, 12):
        log.bump(f"t{i}")
    log.flush()
    assert [w for _, w, _ in _stored()] == [f"t{i}" for i in range(7, 12)]
    assert [r["what"] for r in ops.recent_requests()] == [f"t{i}" for i in range(11, 6, -1)]


def test_a_store_that_stays_down_costs_log_rows_never_memory(log, monkeypatch):
    monkeypatch.setattr(ops, "REQUEST_LOG_BUFFER", 3)
    for i in range(5):
        log.bump(f"t{i}")
    assert log.requests.dropped == 2
    log.flush()
    assert [w for _, w, _ in _stored()] == ["t2", "t3", "t4"]


def test_a_failed_request_write_keeps_the_rows_and_still_writes_the_counters(log, monkeypatch):
    real = ops._add_requests

    def down(*a, **k):
        raise OSError("store is down")
    monkeypatch.setattr(ops, "_add_requests", down)
    log.bump("market_series")
    log.flush()                                                       # does not raise: the log is a log
    assert ops.rows("request_log") == []
    assert [r["reads"] for r in ops.rows("read_log")] == [1]
    with pytest.raises(OSError):
        log.requests.flush()                                          # asked for directly, it says so
    log.bump("macro_series")
    monkeypatch.setattr(ops, "_add_requests", real)
    log.flush()
    assert [w for _, w, _ in _stored()] == ["market_series", "macro_series"]     # in the order they were made


def test_the_switch_turns_the_log_off_and_leaves_the_counters(log, monkeypatch):
    monkeypatch.setenv(ops.REQUEST_LOG_ENV, "0")
    log.bump("market_series")
    ops.log_call("uw", "GET api.test/x")
    log.flush()
    assert ops.rows("request_log") == []
    assert [r["reads"] for r in ops.rows("read_log")] == [1]


def test_an_unknown_kind_is_refused(log):
    with pytest.raises(ValueError, match="unknown request kind"):
        log.requests.add("write", "x")


@pytest.mark.parametrize("url, want", [
    ("https://api.stlouisfed.org/fred/series/observations?series_id=DGS2&api_key=SECRETKEY", "api.stlouisfed.org/fred/series/observations"),
    ("https://user:SECRETKEY@host.test:8443/a/b#frag", "host.test/a/b"),
    ("https://host.test/v1/0123456789abcdef0123456789abcdef0123/quotes", "host.test/v1/~/quotes"),
    ("https://www.sec.gov/Archives/edgar/data/320193/0000320193-24-000123.txt",
     "www.sec.gov/Archives/edgar/data/320193/0000320193-24-000123.txt"),
    ("https://host.test", "host.test"),
])
def test_a_url_is_logged_as_host_and_path_only(url, want):
    assert ops.safe_url(url) == want and "SECRETKEY" not in ops.safe_url(url)


def test_http_get_logs_every_attempt_and_never_the_key(log, http_mock):
    http_mock.add("flaky.test", b"oops", status=503)
    ctx = base.RunContext(run_id="r", dataset="market_series", sleep=lambda s: None)
    with pytest.raises(base.SourceError):
        base.http_get(ctx, "fred", "https://flaky.test/fred/obs?file_type=json", params={"api_key": "SECRETKEY"},
                      headers={"Authorization": "Bearer SECRETKEY"})
    log.flush()
    rows = ops.rows("request_log")
    assert [(r["kind"], r["what"], r["result"], r["dataset"]) for r in rows] == [
        ("call", "fred GET flaky.test/fred/obs", "HTTP 503", "market_series")] * base.HTTP_ATTEMPTS
    assert all(isinstance(r["ms"], int) and r["ms"] >= 0 for r in rows)
    assert "SECRETKEY" not in json.dumps(rows) and "api_key" not in json.dumps(rows)
    assert ctx.calls["fred"] == [base.HTTP_ATTEMPTS, 1, 0]                       # the counters are what they were


def test_http_post_logs_the_answer_and_never_the_body(log, http_mock):
    http_mock.add("figi.test", b"[]")
    ctx = base.RunContext(run_id="r", dataset="spine", sleep=lambda s: None)
    base.http_post(ctx, "openfigi", "https://figi.test/v3/mapping", json=[{"idValue": "BODYVALUE"}],
                   headers={"X-OPENFIGI-APIKEY": "SECRETKEY"})
    log.flush()
    rows = ops.rows("request_log")
    assert [(r["what"], r["result"]) for r in rows] == [("openfigi POST figi.test/v3/mapping", "HTTP 200")]
    assert "SECRETKEY" not in json.dumps(rows) and "BODYVALUE" not in json.dumps(rows)


def test_a_client_that_only_counts_gets_a_row_for_the_call_and_one_for_the_failure(log):
    ctx = base.RunContext(run_id="r", dataset="options/ibkr_iv")
    ctx.count("ibkr", calls=1)
    ctx.count("ibkr", failures=1)
    ctx.count("uw", calls=40)
    ctx.count("uw", http_429=1)                                                  # a counter only: no row
    log.flush()
    assert [(w, r) for _, w, r in _stored()] == [("ibkr", "sent"), ("ibkr", "failed"), ("uw", "40 calls sent")]
    assert {r["dataset"] for r in ops.rows("request_log")} == {"options/ibkr_iv"}


def test_what_and_result_are_cut_to_a_length(log):
    ops.log_call("p", "x" * 500, result="y" * 500)
    log.flush()
    [row] = ops.rows("request_log")
    assert len(row["what"]) == ops.REQUEST_WHAT_MAX and len(row["result"]) == ops.REQUEST_WHAT_MAX


def test_the_backup_leaves_the_request_log_out(log):
    from RevenueChaser.rcdata import backup
    log.bump("market_series")
    log.flush()
    assert "request_log" in ops.TABLES and len(ops.rows("request_log")) == 1     # it is a table with a row ...
    assert not backup._is_kept_ops_table("request_log")                          # ... that no backup keeps
    if ops.backend() == "redis":
        from RevenueChaser.rcdata import ops_redis
        assert "request_log" not in ops_redis.snapshot()
