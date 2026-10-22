"""PL6-a (#26): one identity refresh at a time — ``job_lock`` name ``"identity"``.

The lock lives in ``core.identity.refresh.refresh()``, so it covers both entries at once: the dashboard job
(``app.runners.run_identity``) and ``python -m RevenueChaser.core.identity``. While another refresh holds it,
``refresh()`` still never raises: it returns a run record with ``status == "busy"`` and the holder's PID, writes no
identity row (not even an ``identity_runs`` one), and ``summary()`` reads as a sentence rather than a row of zeros.
The CLI exits ``BUSY_EXIT`` (3); the dashboard job turns the same sentence into its ``JobMessage``.
"""
from __future__ import annotations

import os
import time

import pytest

from RevenueChaser.core import job_lock, rawstore
from RevenueChaser.core.identity import __main__ as cli
from RevenueChaser.core.identity import refresh as R
from RevenueChaser.core.identity.tests.test_identity_refresh import UNIVERSE, Fakes
from RevenueChaser.core.tests.lock_holders import lock_held


@pytest.fixture(autouse=True)
def universe():
    """One universe row per test: without it ``_run`` stops before its first write, and a lock taken too late
    would pass this file's "writes nothing" assertions for the wrong reason."""
    rawstore.write_rows("equity_universe", [{"ticker": t, "name": n, "market_cap": c} for t, n, c in UNIVERSE])


# ── a held lock refuses, and writes nothing ──────────────────────────────────

def test_a_held_lock_makes_refresh_busy_and_writes_no_identity_row(rawstore_tmp_db):
    fk = Fakes()
    started = time.monotonic()
    with lock_held("identity"):
        rec = R.refresh(limit=1, clients=fk.clients())
    assert time.monotonic() - started < 10, "a busy refresh must refuse at once, not wait on the lock"
    assert rec["status"] == "busy"
    assert rec["holder_pid"] == os.getpid()
    assert not rec.get("counts"), "a busy run carries no counts"
    assert fk.calls == {}, "a busy refresh must not ask a source anything"
    for table in ("identity_runs", "security_master", "identity_conflicts", "sec_company_tickers"):
        assert rawstore.query(f"SELECT * FROM {table}") == [], f"a busy refresh wrote {table}"


def test_the_busy_record_reads_as_a_sentence_not_a_row_of_zeros(rawstore_tmp_db):
    with lock_held("identity"):
        rec = R.refresh(limit=1, clients=Fakes().clients())
    line = R.summary(rec)
    assert "another identity refresh" in line and "nothing was written" in line
    assert f"pid {os.getpid()}" in line
    assert "0 tickers" not in line and "sources ok" not in line


def test_the_cli_exits_with_its_busy_code_when_the_lock_is_held(rawstore_tmp_db, capsys):
    with lock_held("identity"):
        assert cli.main(["--refresh"]) == cli.BUSY_EXIT == 3
    err = capsys.readouterr().err
    assert "another identity refresh" in err and "nothing was written" in err


# ── a free lock runs, and releases ───────────────────────────────────────────

def test_a_normal_run_takes_the_lock_and_releases_it(rawstore_tmp_db):
    rec = R.refresh(limit=1, sources=["nasdaq"], clients=Fakes().clients())
    assert rec["status"] != "busy" and rec["run_id"]
    assert "identity" not in dict(job_lock.holders()), "the lock outlived the run"
    assert len(rawstore.query("SELECT * FROM identity_runs")) == 1
