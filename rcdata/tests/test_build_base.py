"""``rcdata/build/base.py``: the pacer, the keyless HTTP helper, raw and Parquet writes, the idle gate."""
from __future__ import annotations

import pytest

from RevenueChaser.core import job_lock
from RevenueChaser.rcdata.build import base


def test_the_pacer_never_lets_120_calls_into_one_minute():
    now = [0.0]
    sent = []

    def sleep(s):
        now[0] += s

    pacer = base.Pacer(110, clock=lambda: now[0], sleep=sleep)
    for _ in range(400):
        pacer.wait()
        sent.append(now[0])
        now[0] += 0.01                      # a fast answer
    for i, t in enumerate(sent):
        in_window = sum(1 for u in sent[i:] if u - t < 60.0)
        assert in_window <= 110
    assert sent[-1] > 180.0                 # it really waited


def test_http_get_retries_5xx_counts_every_attempt_and_raises(http_mock):
    http_mock.add("flaky.test", b"oops", status=503)
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    with pytest.raises(base.SourceError, match="HTTP 503"):
        base.http_get(ctx, "cboe", "https://flaky.test/x.csv")
    assert ctx.calls["cboe"] == [base.HTTP_ATTEMPTS, 1, 0]
    assert len(http_mock.calls("flaky.test")) == base.HTTP_ATTEMPTS


def test_http_get_does_not_retry_a_404(http_mock):
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    with pytest.raises(base.SourceError):
        base.http_get(ctx, "fed", "https://nothing.test/")
    assert ctx.calls["fed"] == [1, 1, 0]


def test_raw_files_round_trip_and_gzip_by_name(tmp_path):
    p = base.write_raw(tmp_path / "a" / "x.json.gz", b'{"a": 1}')
    assert p.read_bytes()[:2] == b"\x1f\x8b" and base.read_raw_json(p) == {"a": 1}
    assert not list(tmp_path.rglob("*.tmp"))


def test_upsert_partition_merges_by_key_and_the_newest_fetch_wins(tmp_path):
    schema = [("k", "TEXT"), ("v", "DOUBLE"), ("fetched_at", "DOUBLE")]
    part = tmp_path / "year=2026" / "part-0.parquet"
    base.upsert_partition([{"k": "a", "v": 1, "fetched_at": 1}, {"k": "b", "v": 2, "fetched_at": 1}], schema, ["k"], part)
    base.upsert_partition([{"k": "b", "v": 3, "fetched_at": 2}, {"k": "c", "v": 4, "fetched_at": 2}], schema, ["k"], part)
    import duckdb
    rows = duckdb.connect().execute(f"SELECT k, v FROM read_parquet({base.sql_path(part)}) ORDER BY k").fetchall()
    assert rows == [("a", 1.0), ("b", 3.0), ("c", 4.0)]
    assert not list(tmp_path.rglob("*.tmp"))


def test_the_builder_is_not_idle_while_a_job_lock_is_held():
    assert base.idle_problem() is None
    with job_lock.acquire("daily_bars", wait=0) as got:
        assert got
        assert "daily_bars" in base.idle_problem()
    assert base.idle_problem() is None


def test_session_date_is_empty_outside_the_calendar_table():
    from datetime import date
    assert base.session_date_of(date(2026, 10, 25)) == date(2026, 10, 23)    # a Sunday -> Friday
    assert base.session_date_of(date(1990, 1, 2)) is None


def test_the_user_agent_carries_the_contact_from_rc_http_contact(http_mock, monkeypatch):
    http_mock.add("ua.test", b"ok")
    monkeypatch.setenv("RC_HTTP_CONTACT", "builder-contact@example.invalid")
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    base.http_get(ctx, "bls", "https://ua.test/x")
    assert "builder-contact@example.invalid" in http_mock.requests[-1].headers["user-agent"]
    monkeypatch.setenv("RC_HTTP_CONTACT", "")
    assert "RC_HTTP_CONTACT" in base.user_agent()


def test_a_403_says_what_to_set(http_mock, monkeypatch):
    monkeypatch.setenv("RC_HTTP_CONTACT", "")
    http_mock.add("bls.test", b"Access Denied", status=403)
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    with pytest.raises(base.SourceError, match=r"HTTP 403 \(refused: set RC_HTTP_CONTACT"):
        base.http_get(ctx, "bls", "https://bls.test/schedule/news_release/bls.ics")
    assert ctx.calls["bls"] == [1, 1, 0]


def test_a_403_to_a_keyed_request_points_at_the_key_not_the_contact(http_mock, monkeypatch):
    # The Opus review's N2: Alpaca answers a wrong key with 403, and the contact hint sent the reader the wrong way.
    monkeypatch.setenv("RC_HTTP_CONTACT", "")
    http_mock.add("alpaca.test", b"forbidden", status=403)
    ctx = base.RunContext(run_id="r", dataset="d", sleep=lambda s: None)
    with pytest.raises(base.SourceError, match=r"HTTP 403 \(refused: check the key") as err:
        base.http_get(ctx, "alpaca", "https://alpaca.test/v2/stocks/bars", headers={"APCA-API-KEY-ID": "test-key"},
                      keyed=True)
    assert "RC_HTTP_CONTACT" not in str(err.value) and "test-key" not in str(err.value)
