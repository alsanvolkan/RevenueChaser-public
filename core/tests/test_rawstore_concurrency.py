"""Concurrency contract for the single-file DuckDB store.

The whole reason ``core/rawstore.py`` opens/closes a handle per operation is
that DuckDB has one writer and no SQLite-style WAL. Two things must hold, and
neither used to be tested:

1. A read and a write that OVERLAP IN ONE PROCESS must both work. The dashboard
   serves requests on Flask's threads while ``app/jobs.py``'s worker thread runs
   a module writer, so this happens constantly. DuckDB refuses a second
   connection to the same file with a *different* configuration, so
   ``connect()`` must never mix read-only and read-write handles.
2. Contention must be RETRIED within the stated budget, not raised on the first
   attempt — and DuckDB signals it as several different exception classes
   (another process holding the lock, the configuration clash above, and a
   thread-vs-thread race on attaching the file).
"""
from __future__ import annotations

import threading
import time

import duckdb
import pytest

from RevenueChaser.core import rawstore


def _stored_watchlist() -> set[str]:
    """The tickers of the store's ``watchlist`` table, read by name: since PR 4 ``rawstore.load_watchlist`` is the
    stock watch list in ops.sqlite, and this file uses the old table as its sample table."""
    return {r["ticker"] for r in rawstore.get_all("watchlist")}


def _seed():
    rawstore.write_rows("ticker_payload_cache", [{"ticker": "AAPL", "payload": {"x": 1},
                                          "fetched_at": 1.0, "fetched_at_iso": ""}])


class TestSameProcessOverlap:
    """Regression: a read during a write raised ConnectionException and 500'd
    the dashboard — and the retry loop never saw it, because it only caught
    IOException."""

    def test_read_while_a_write_handle_is_open(self, rawstore_tmp_db):
        _seed()
        opened = threading.Event()
        release = threading.Event()

        def writer():
            with rawstore.connect() as conn:
                conn.execute("SELECT 1")
                opened.set()
                release.wait(5)

        t = threading.Thread(target=writer)
        t.start()
        try:
            assert opened.wait(5)
            rows = rawstore.query("SELECT * FROM ticker_payload_cache")
            assert [r["ticker"] for r in rows] == ["AAPL"]
            assert rawstore.get("ticker_payload_cache", "AAPL")["payload"] == {"x": 1}
            assert rawstore.count("ticker_payload_cache") == 1
        finally:
            release.set()
            t.join(5)

    def test_write_while_a_read_handle_is_open(self, rawstore_tmp_db):
        _seed()
        opened = threading.Event()
        release = threading.Event()

        def reader():
            with rawstore.connect(read_only=True) as conn:
                conn.execute("SELECT * FROM ticker_payload_cache").fetchall()
                opened.set()
                release.wait(5)

        t = threading.Thread(target=reader)
        t.start()
        try:
            assert opened.wait(5)
            rawstore.write_rows("ticker_payload_cache", [{"ticker": "MSFT", "payload": {"x": 2},
                                                  "fetched_at": 2.0, "fetched_at_iso": ""}])
        finally:
            release.set()
            t.join(5)
        assert rawstore.count("ticker_payload_cache") == 2

    def test_concurrent_readers_and_writers_do_not_raise(self, rawstore_tmp_db):
        _seed()
        errors: list[Exception] = []

        def write(i):
            try:
                for _ in range(5):
                    rawstore.write_rows("ticker_payload_cache", [{
                        "ticker": f"T{i}", "payload": {"x": i},
                        "fetched_at": float(i), "fetched_at_iso": ""}])
            except Exception as exc:  # noqa: BLE001 - recorded and asserted below
                errors.append(exc)

        def read():
            try:
                for _ in range(5):
                    rawstore.query("SELECT * FROM ticker_payload_cache")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(3)]
        threads += [threading.Thread(target=read) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert not errors, f"concurrent access raised: {errors}"

    def test_connect_never_mixes_configurations(self, rawstore_tmp_db, monkeypatch):
        """``read_only`` selects the retry budget only — it must not reach DuckDB,
        or the same-process configuration clash comes straight back."""
        seen = []
        real = duckdb.connect

        def spy(path, *args, **kwargs):
            seen.append(kwargs)
            return real(path, *args, **kwargs)

        monkeypatch.setattr(duckdb, "connect", spy)
        with rawstore.connect(read_only=True):
            pass
        with rawstore.connect(read_only=False):
            pass
        assert seen and all(not k.get("read_only") for k in seen), seen


class TestLockRetry:
    def test_conflict_recognises_every_duckdb_contention_message(self):
        """DuckDB reports contention as at least three exception classes with
        three unrelated messages; all are transient and must be retried."""
        assert rawstore._conflict(Exception("Could not set lock on file /x/raw.duckdb"))
        assert rawstore._conflict(Exception("database is locked"))
        assert rawstore._conflict(Exception(
            "Can't open a connection to same database file with a different "
            "configuration than existing connections"))
        assert rawstore._conflict(Exception(
            'Binder Error: Unique file handle conflict: Cannot attach "raw" - the '
            'database file "/x/raw.duckdb" is already attached by database "raw"'))
        assert not rawstore._conflict(Exception("no such table: nope"))
        assert not rawstore._conflict(Exception("disk I/O error"))

    def test_binder_attach_race_is_retried(self, rawstore_tmp_db, monkeypatch):
        """Two threads calling duckdb.connect() at the same instant race on the
        instance cache. Caught in the wild by the stress test above."""
        real = duckdb.connect
        calls = {"n": 0}

        def racy(path, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise duckdb.BinderException(
                    'Binder Error: Unique file handle conflict: Cannot attach "raw" - '
                    'the database file "x" is already attached by database "raw"')
            return real(path, *args, **kwargs)

        monkeypatch.setattr(duckdb, "connect", racy)
        rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
        assert calls["n"] == 2
        assert _stored_watchlist() == {"AAPL"}

    def test_lock_contention_is_retried_then_succeeds(self, rawstore_tmp_db, monkeypatch):
        real = duckdb.connect
        calls = {"n": 0}

        def flaky(path, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise duckdb.IOException("Could not set lock on file")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(duckdb, "connect", flaky)
        monkeypatch.setattr(rawstore, "_WRITE_DELAYS", (0.01, 0.01, 0.01, 0.01))
        rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
        assert calls["n"] == 3
        assert _stored_watchlist() == {"AAPL"}

    def test_exhausted_budget_raises_rawstore_error(self, rawstore_tmp_db, monkeypatch):
        def always_locked(path, *args, **kwargs):
            raise duckdb.IOException("Could not set lock on file")

        monkeypatch.setattr(duckdb, "connect", always_locked)
        monkeypatch.setattr(rawstore, "_WRITE_DELAYS", (0.01, 0.01))
        with pytest.raises(rawstore.RawStoreError):
            rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])

    def test_non_conflict_error_is_not_retried(self, rawstore_tmp_db, monkeypatch):
        calls = {"n": 0}

        def broken(path, *args, **kwargs):
            calls["n"] += 1
            raise duckdb.IOException("disk I/O error")  # not contention

        monkeypatch.setattr(duckdb, "connect", broken)
        monkeypatch.setattr(rawstore, "_WRITE_DELAYS", (0.01,) * 5)
        t = time.time()
        with pytest.raises(rawstore.RawStoreError):
            rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
        assert calls["n"] == 1, "a non-lock error must fail fast, not burn the budget"
        assert time.time() - t < 1


class TestConcurrentWritesToTheSameRow:
    """DuckDB uses optimistic concurrency: two transactions touching the same
    row both start, and the loser fails at COMMIT. That happens after connect,
    so the connect-level retry cannot see it — the operation itself is retried.
    Reachable from two rapid /api/watchlist/toggle requests, or a request thread
    racing the app/jobs.py worker on one row.
    """

    def test_many_threads_upserting_the_same_keys(self, rawstore_tmp_db):
        errors: list[Exception] = []

        def write(tid):
            try:
                for i in range(30):
                    rawstore.write_rows("store_meta", [{"key": f"k{i}", "value": str(tid)}])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not errors, f"same-row writes raised: {errors[:2]}"
        assert rawstore.count("store_meta") == 30

    def test_concurrent_watchlist_toggles(self, rawstore_tmp_db):
        """Whole-list read-modify-write of a DuckDB table from several threads raises nothing (``replace_all`` on the
        store's ``watchlist`` table; the watch lists themselves are in ops.sqlite since PR 4, and their whole-list
        race is in rcdata/tests/test_ops_watchlists.py)."""
        errors: list[Exception] = []

        def toggle(ticker):
            try:
                for _ in range(10):
                    wl = _stored_watchlist()
                    wl.add(ticker)
                    rawstore.replace_all("watchlist", [{"ticker": t} for t in sorted(wl)], allow_empty=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=toggle, args=(t,)) for t in ("AAPL", "MSFT", "NVDA")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not errors, f"concurrent toggles raised: {errors[:2]}"

    def test_commit_conflict_is_recognised_and_retried(self, rawstore_tmp_db, monkeypatch):
        # both ways a same-row race loses, observed in the stress test above
        assert rawstore._commit_conflict(Exception(
            'TransactionContext Error: Failed to commit: PRIMARY KEY or UNIQUE '
            'constraint violation: duplicate key "probe0"'))
        assert rawstore._commit_conflict(Exception("Conflict on transaction"))
        assert rawstore._commit_conflict(Exception(
            'Constraint Error: Duplicate key "key: k0" violates primary key constraint.'))
        assert not rawstore._commit_conflict(Exception("no such table: nope"))
        assert not rawstore._commit_conflict(Exception("Could not convert string to DOUBLE"))

        calls = {"n": 0}
        real = rawstore._apply_write

        def flaky(conn, name, spec, rows):
            calls["n"] += 1
            if calls["n"] == 1:
                raise duckdb.TransactionException(
                    "TransactionContext Error: Failed to commit: duplicate key")
            return real(conn, name, spec, rows)

        monkeypatch.setattr(rawstore, "_apply_write", flaky)
        monkeypatch.setattr(rawstore, "_WRITE_DELAYS", (0.01, 0.01, 0.01))
        rawstore.write_rows("watchlist", [{"ticker": "AAPL"}])
        assert calls["n"] == 2
        assert _stored_watchlist() == {"AAPL"}

    def test_a_real_constraint_error_is_not_swallowed_by_the_retry(self, rawstore_tmp_db):
        """A bad batch must still raise, not spin the retry budget."""
        with pytest.raises(Exception) as exc:
            rawstore.write_rows("swing_ohlc", [{
                "ticker": "AAPL", "ts": "2026-01-01", "open": "not-a-number",
                "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "cached_at": 1.0}])
        assert "not-a-number" in str(exc.value)

    def test_a_genuine_duplicate_key_still_raises_after_the_budget(self, rawstore_tmp_db,
                                                                   monkeypatch):
        """Retrying a real (non-racy) PK violation must not turn it into a
        silent success — it just surfaces a few retries later."""
        monkeypatch.setattr(rawstore, "_WRITE_DELAYS", (0.01, 0.01))
        with pytest.raises(Exception) as exc:
            # append mode with no key -> plain INSERT into a PK'd table twice
            rawstore.write_rows("sec13f_submissions", [{"accession": "a", "cik": "1"}])
            with rawstore.connect() as conn:
                conn.executemany(
                    "INSERT INTO sec13f_submissions (accession, cik) VALUES (?, ?)",
                    [["a", "1"], ["a", "2"]])
        assert "violates primary key" in str(exc.value).lower()
