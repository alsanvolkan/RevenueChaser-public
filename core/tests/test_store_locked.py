"""A lock held by another process must be named, explained, and fast to detect.

Two separate defects lived here:
  - the failure surfaced as ``could not open <path>``, discarding DuckDB's own
    message, which names the holding process;
  - it was retried for the full ~29s read budget even though a foreign lock is
    not transient, so every board stalled for minutes before failing.
"""
from __future__ import annotations

import os
import time

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.rawstore import StoreLockedError

VSCODE = (
    'IO Error: Could not set lock on file "/x/.store/raw.duckdb": Conflicting '
    'lock is held in /Applications/Visual Studio Code.app/Contents/Frameworks/'
    'Code Helper (Plugin).app/Contents/MacOS/Code Helper (Plugin) (PID 2832) '
    'by user someone. However, you would be able to open this database in '
    'read-only mode.'
)
GENERIC = (
    'IO Error: Could not set lock on file "/x/.store/raw.duckdb": Conflicting '
    'lock is held in /usr/local/bin/duckdb (PID 4242) by user someone.'
)


@pytest.fixture(autouse=True)
def _pid_is_gone(monkeypatch):
    """``_process_name`` would run a real ``ps -p <pid>``; PID 2832 can be alive on a runner (#196)."""
    monkeypatch.setattr(rawstore, "_process_name", lambda pid: "")


class TestHolderParsing:
    def test_the_pid_is_extracted(self):
        assert rawstore._lock_error(VSCODE).holder_pid == 2832

    def test_the_holder_path_is_extracted(self):
        assert "Code Helper" in rawstore._lock_error(VSCODE).holder_path

    def test_the_name_falls_back_to_the_binary_when_the_pid_is_gone(self):
        """With the PID gone, ``_process_name`` returns nothing (stubbed by the autouse fixture)."""
        err = rawstore._lock_error(VSCODE)
        assert err.holder_name  # never blank
        assert "Code Helper" in err.holder_name

    def test_a_live_process_name_wins_over_the_binary(self, monkeypatch):
        monkeypatch.setattr(rawstore, "_process_name", lambda pid: "live-name")
        err = rawstore._lock_error(VSCODE)
        assert err.holder_name == "live-name"

    def test_an_unparseable_message_still_produces_an_error(self):
        err = rawstore._lock_error("Conflicting lock is held somewhere unhelpful")
        assert err.holder_pid is None
        assert err.holder_label() == "another process"


class TestMessage:
    def test_the_headline_says_locked_by_another_process(self):
        assert "locked by another process" in rawstore._lock_error(VSCODE).headline().lower()

    def test_the_headline_names_the_holder_and_pid(self):
        head = rawstore._lock_error(VSCODE).headline()
        assert "2832" in head and "Code Helper" in head

    def test_vscode_gets_the_extension_specific_remedy(self):
        remedy = rawstore._lock_error(VSCODE).remedy()
        assert "attached" in remedy and "Reload Window" in remedy

    def test_a_duckdb_cli_gets_its_own_remedy(self):
        assert "DuckDB session" in rawstore._lock_error(GENERIC).remedy()

    def test_an_unknown_holder_gets_lsof_and_the_pid(self):
        detail = "Conflicting lock is held in /opt/mystery/thing (PID 999999) by user x."
        remedy = rawstore._lock_error(detail).remedy()
        assert "lsof" in remedy and "999999" in remedy

    def test_str_carries_both_the_headline_and_the_remedy(self):
        err = rawstore._lock_error(VSCODE)
        assert err.headline() in str(err) and err.remedy() in str(err)

    def test_it_is_a_rawstore_error_so_existing_handlers_still_catch_it(self):
        assert isinstance(rawstore._lock_error(VSCODE), rawstore.RawStoreError)


class TestForeignLockDetection:
    def test_another_process_is_foreign(self):
        assert rawstore._foreign_lock(VSCODE) is True

    def test_our_own_pid_is_not_foreign(self):
        detail = f"Conflicting lock is held in /x (PID {os.getpid()}) by user x."
        assert rawstore._foreign_lock(detail) is False

    def test_an_in_process_race_is_not_a_foreign_lock(self):
        """These clear in milliseconds and must keep the full retry budget."""
        assert rawstore._foreign_lock("BinderException: unique file handle conflict") is False
        assert rawstore._foreign_lock("ConnectionException: ... different configuration") is False

    def test_the_foreign_budget_is_far_shorter_than_the_read_budget(self):
        assert sum(rawstore._FOREIGN_LOCK_DELAYS) < 1.0
        assert sum(rawstore._READ_DELAYS) > 20.0


class TestFailFast:
    def test_a_foreign_lock_does_not_burn_the_full_read_budget(self, monkeypatch):
        """The regression that made every board take minutes to fail."""
        import duckdb

        def _locked(*a, **kw):
            raise duckdb.IOException(VSCODE)

        monkeypatch.setattr(duckdb, "connect", _locked)
        # Naming the holder runs `ps` via subprocess, whose POSIX wait polls
        # with time.sleep (1 ms doubling to 50 ms); with sleep patched to a
        # recorder that loop adds seconds to the sum. Only the retry budget is
        # under test here.
        monkeypatch.setattr(rawstore, "_process_name", lambda pid: "")
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)

        with pytest.raises(StoreLockedError):
            rawstore._connect_with_retry(rawstore._READ_DELAYS)

        assert sum(slept) < 1.0, (
            f"waited {sum(slept):.2f}s on a lock that will not clear; the read "
            f"budget is {sum(rawstore._READ_DELAYS):.2f}s")

    def test_an_in_process_race_still_gets_the_full_budget(self, monkeypatch):
        """Fail-fast must not make the real transient races flaky."""
        import duckdb

        def _race(*a, **kw):
            raise duckdb.BinderException("unique file handle conflict")

        monkeypatch.setattr(duckdb, "connect", _race)
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)

        with pytest.raises(rawstore.RawStoreError):
            rawstore._connect_with_retry(rawstore._READ_DELAYS)

        assert sum(slept) > 20.0, "transient contention lost its retry budget"

    def test_a_non_contention_error_is_not_retried_at_all(self, monkeypatch):
        import duckdb

        def _broken(*a, **kw):
            raise duckdb.IOException("disk is on fire")

        monkeypatch.setattr(duckdb, "connect", _broken)
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)

        with pytest.raises(rawstore.RawStoreError) as exc:
            rawstore._connect_with_retry(rawstore._READ_DELAYS)

        assert slept == []
        assert not isinstance(exc.value, StoreLockedError)
        assert "disk is on fire" in str(exc.value)


class TestBriefWait:
    """``brief_wait`` (#282): best-effort bookkeeping writes must not wait ~29 s for a peer process."""

    @staticmethod
    def _peer_lock(monkeypatch):
        import duckdb

        def _locked(*a, **kw):
            raise duckdb.IOException(VSCODE)

        monkeypatch.setattr(duckdb, "connect", _locked)
        monkeypatch.setattr(rawstore, "_process_name", lambda pid: "")
        monkeypatch.setattr(rawstore, "_holder_is_peer", lambda msg, path: True)
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)
        return slept

    def test_a_peer_lock_costs_the_full_budget_by_default(self, monkeypatch):
        slept = self._peer_lock(monkeypatch)
        with pytest.raises(StoreLockedError):
            rawstore._connect_with_retry(rawstore._WRITE_DELAYS)
        assert sum(slept) > 20.0

    def test_inside_brief_wait_a_peer_lock_costs_the_foreign_budget(self, monkeypatch):
        slept = self._peer_lock(monkeypatch)
        with rawstore.brief_wait(), pytest.raises(StoreLockedError):
            rawstore._connect_with_retry(rawstore._WRITE_DELAYS)
        assert sum(slept) < 1.0

    def test_the_flag_is_restored_on_exit(self):
        with rawstore.brief_wait():
            pass
        assert getattr(rawstore._LOCAL, "brief", False) is False
