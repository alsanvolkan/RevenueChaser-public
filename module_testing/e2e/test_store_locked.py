"""A locked store must explain itself, on the page, naming the holder.

DuckDB allows one writer process at a time, so anything else holding
``raw.duckdb`` — a DuckDB CLI, a notebook, the VS Code DuckDB extension —
blocks every board at once. That used to surface as a bare 500 on each tab
with the real cause (DuckDB names the holding process) buried in the log, and
it cost a long debugging session to find.

The lock here is held by the *test process*, so its own PID is the holder and
the assertions can check the page names it.
"""
from __future__ import annotations

import contextlib
import os

import duckdb
import pytest


@pytest.fixture
def store_file(dashboard):
    """``raw.duckdb`` and every board file (S9: Sector Rotation and LEAPS read their own), each made before anyone
    tries to lock it."""
    from RevenueChaser.core import rawstore
    paths = [dashboard.store_dir / "raw.duckdb"] + [
        dashboard.store_dir / rawstore.BOARDS_DIRNAME / f"{b}.duckdb" for b in rawstore.BOARD_DBS]
    for path in paths:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = duckdb.connect(str(path))
            conn.execute("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)")
            conn.close()
    return paths


@contextlib.contextmanager
def _held(paths):
    """Hold every store file open from this process for the block."""
    with contextlib.ExitStack() as stack:
        for path in paths:
            stack.callback(duckdb.connect(str(path)).close)
        yield


@pytest.fixture
def locked_store(store_file, serve_13f_totals):
    """Hold the store open from this process for the duration of the test.

    The 13F board's numbers are the builder's since G9, so it reads ``raw.duckdb`` only for the names and the
    snapshot inputs of the rows it is about to draw: with nothing served it renders its empty state and never
    meets the lock. One served quarter puts it back in the same position as the other boards.
    """
    serve_13f_totals({"NVDA": {"2026-06-30": {"shares": 5_000_000.0, "value": 7e8, "holders": 2250}}})
    with _held(store_file):
        yield


# No "module_leaps": it has no measurement yet (#730) and its page reads no store, so it cannot meet the lock.
BOARDS = ["module_sector_rotation", "module_sec_13f", "module_financials"]


class TestTheBoardExplainsTheLock:
    @pytest.mark.parametrize("board", BOARDS)
    def test_a_locked_store_is_503_not_500(self, dashboard, locked_store, board):
        r = dashboard.view(board, refresh=True)
        assert r.status_code == 503, (
            f"{board} answered {r.status_code}; a lock is temporary and must not "
            "look like a crash")

    @pytest.mark.parametrize("board", BOARDS)
    def test_the_page_says_the_database_is_locked(self, dashboard, locked_store, board):
        body = dashboard.view(board, refresh=True).text
        assert "locked by another process" in body.lower(), (
            f"{board} did not explain the lock. Got: {body[:300]}")

    def test_the_page_names_the_holding_process(self, dashboard, locked_store):
        """The whole point: say WHO has it, not just that something does.

        DuckDB names the holder PID only on Unix; on Windows the message omits it,
        so the page falls back to "another process".
        """
        body = dashboard.view("module_sector_rotation", refresh=True).text
        if os.name == "nt":
            assert "another process" in body.lower()
        else:
            assert str(os.getpid()) in body, (
                f"the holder PID {os.getpid()} is missing from the page")

    def test_the_page_offers_a_concrete_fix(self, dashboard, locked_store):
        body = dashboard.view("module_sector_rotation", refresh=True).text
        assert "lsof" in body or "Reload Window" in body

    def test_the_page_reassures_that_no_data_is_lost(self, dashboard, locked_store):
        body = dashboard.view("module_sector_rotation", refresh=True).text
        assert "No data has been lost" in body

    def test_the_raw_driver_error_is_still_available(self, dashboard, locked_store):
        """Kept for diagnosis, behind a disclosure rather than in your face."""
        body = dashboard.view("module_sector_rotation", refresh=True).text
        assert ("Conflicting lock is held" in body
                or "being used by another process" in body)


class TestApiCallersGetJson:
    """The dashboard polls its own API; a rendered HTML page would break it."""

    def test_api_returns_structured_json(self, dashboard, locked_store):
        r = dashboard.get("/api/rotation/dates")
        assert r.status_code == 503
        body = r.json()
        assert body["error"] == "store_locked"
        assert "locked by another process" in body["message"].lower()
        if os.name != "nt":  # DuckDB names the holder PID only on Unix
            assert body["pid"] == os.getpid()
        assert body["remedy"]

    def test_api_response_is_not_html(self, dashboard, locked_store):
        r = dashboard.get("/api/rotation/dates")
        assert "application/json" in r.headers.get("content-type", "")

    def test_the_provenance_label_degrades_instead_of_erroring(self, dashboard, locked_store):
        """Deliberate exception: `data_as_of` is a label, and a label must never
        be able to break a board — it reports null rather than failing."""
        r = dashboard.get("/api/data_asof/module_sector_rotation")
        assert r.status_code == 200
        assert r.json()["data_as_of"] is None


class TestRecovery:
    def test_boards_work_again_once_the_lock_is_released(self, dashboard, store_file):
        """A lock is transient — nothing may need re-fetching afterwards."""
        with _held(store_file):
            assert dashboard.view("module_sector_rotation", refresh=True).status_code == 503
        r = dashboard.view("module_sector_rotation", refresh=True)
        assert r.status_code == 200, (
            f"board did not recover after the lock was released: {r.status_code}")
