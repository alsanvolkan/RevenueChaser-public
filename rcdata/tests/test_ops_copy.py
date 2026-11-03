"""``rcdata/ops_copy.py``: the one-time copy of ``ops.sqlite`` into Redis (master plan Order 3).

Read-only on the file, a dry run that only counts, a refusal when Redis already holds this market root's state, and
a check after the copy that exits non-zero on any difference. The price issues are copied like every other row.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest

from RevenueChaser.core import config
from RevenueChaser.rcdata import ops, ops_copy

pytestmark = pytest.mark.skipif(not os.getenv("RC_REDIS_URL"), reason="needs a Redis at RC_REDIS_URL (CI runs one)")


@pytest.fixture
def source(monkeypatch, tmp_path):
    """An ops.sqlite written by the SQLite backend under another market root: open price issues among the rows."""
    url = os.environ["RC_REDIS_URL"]
    here = config.MARKET_DIR
    monkeypatch.delenv("RC_REDIS_URL")
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "live_market")
    found = [("price_gap_no_action", f"T{i}", "price missing") for i in range(30)] + [("stale_close", "SPY", "old")]
    ops.sync_issues("prices/serving", ["price_gap_no_action", "stale_close"], found, run_id="r0")
    ops.sync_issues("prices/serving", ["stale_close"], [])                      # one resolved row too
    ops.record_issue("market_series", "parse", "fred:DGS2", None)
    ops.enqueue("market_series", {"x": 1}, by="settings")
    ops.next_request()
    ops.enqueue("prices/serving", None, by="hourly")
    ops.record_check("market_series", "changed", rows=10, coverage=0.75, run_id="r1")
    ops.start_run("r1", "market_series", "cli", {"once": True})
    ops.count_calls("uw", 7, failures=1)
    ops.sample_uw_headers({"x-uw-daily-req-count": "355"}, run_id="r1")
    ops.watch_add("stock", "AAPL", by="avo", note="pinned")
    ops.watch_toggle("option", "NVDA", by="board")
    ops.schedule_pause("market_series", by="avo")
    log = ops.ReadLog(process="cli")
    log.bump("market_series")
    log.flush()
    path = ops.ops_path()
    snapshot = {t: ops.rows(t) for t in ops_copy.COPIED}
    monkeypatch.setattr(config, "MARKET_DIR", here)
    monkeypatch.setenv("RC_REDIS_URL", url)
    return path, snapshot


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_dry_run_counts_per_table_and_per_issue_kind_and_writes_nothing(source, capsys):
    path, snapshot = source
    before = _sha(path)
    assert ops_copy.main(["--sqlite", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"build_issues: {len(snapshot['build_issues'])}" in out
    assert "open prices/serving price_gap_no_action: 30" in out and "open issues: 31" in out
    assert "dry run: nothing written" in out
    assert ops.rows("build_issues") == [] and ops.schema_version() is None
    assert _sha(path) == before


def test_the_copy_holds_every_row_as_it_was_and_keeps_the_price_issues(source, capsys):
    path, snapshot = source
    before = _sha(path)
    assert ops_copy.main(["--sqlite", str(path)]) == 0
    assert "copy verified" in capsys.readouterr().out
    for table, want in snapshot.items():
        assert ops.rows(table) == want, table
    assert ops.open_issues("prices/serving", 0)[0] == 30
    assert ops.open_issue_kinds("prices/serving") == [{"kind": "price_gap_no_action", "count": 30}]
    assert ops.schema_version() == ops.OPS_SCHEMA_VERSION
    assert _sha(path) == before and not path.with_name(path.name + "-journal").exists()
    # The app goes on where the file stopped: new ids follow the copied ones, and a sync keeps the open rows.
    assert ops.enqueue("x", None, by="cli") == max(r["id"] for r in snapshot["build_requests"]) + 1
    found = [("price_gap_no_action", f"T{i}", "price missing") for i in range(30)]
    assert ops.sync_issues("prices/serving", ["price_gap_no_action"], found) == (0, 0)


def test_a_redis_that_already_holds_this_root_is_refused_unless_forced(source, capsys):
    path, _ = source
    ops.watch_add("stock", "MSFT", by="someone")
    assert ops_copy.main(["--sqlite", str(path)]) == 2
    assert "already holds" in capsys.readouterr().out
    assert ops.rows("build_issues") == []
    assert ops_copy.main(["--sqlite", str(path), "--force"]) == 1       # copied, but MSFT makes the check differ
    assert "MISMATCH watchlist_stock" in capsys.readouterr().out


def test_a_row_lost_on_the_way_fails_the_check(source, monkeypatch, capsys):
    path, _ = source
    from RevenueChaser.rcdata import ops_redis
    real = ops_redis.put_rows
    monkeypatch.setattr(ops_redis, "put_rows", lambda table, rows, **kw: real(table, list(rows)[1:], **kw))
    assert ops_copy.main(["--sqlite", str(path)]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH build_issues" in out and "copy verified" not in out


def test_without_a_redis_url_or_a_file_nothing_starts(source, monkeypatch, tmp_path, capsys):
    path, _ = source
    assert ops_copy.main(["--sqlite", str(tmp_path / "missing.sqlite")]) == 2
    monkeypatch.delenv("RC_REDIS_URL")
    assert ops_copy.main(["--sqlite", str(path)]) == 2
    assert "RC_REDIS_URL" in capsys.readouterr().out


def test_the_file_is_opened_read_only(source):
    path, _ = source
    with pytest.raises(sqlite3.OperationalError):
        ops_copy._open_read_only(path.with_name("missing.sqlite"))       # read-only never creates a file
    assert not path.with_name("missing.sqlite").exists()
    con = ops_copy._open_read_only(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM build_issues")
    finally:
        con.close()
