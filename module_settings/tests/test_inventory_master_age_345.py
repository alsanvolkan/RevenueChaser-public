"""Settings inventory age note for ``security_master`` (#345, #381).

The clock is pinned (``FIXED``). The note reads ``MAX(security_master.updated_at)`` only.
"""
from __future__ import annotations

import re

from RevenueChaser.core import rawstore
from RevenueChaser.module_settings import inventory, report
from RevenueChaser.module_settings.tests.test_inventory import FIXED, _rows


def _age_html(html: str) -> str:
    match = re.search(r'<div id="identity-master-age"[^>]*>.*?</div>', html)
    assert match, "the identity row does not show the age note"
    return match.group(0)


def _master(*stamps: str | None):
    rawstore.write_rows("security_master", [
        {"ticker": f"T{i}", "updated_at": stamp} for i, stamp in enumerate(stamps)])


def _run(run_id: str, finished: str, status: str = "ok"):
    rawstore.write_rows("identity_runs", [{
        "run_id": run_id, "started_at": finished, "finished_at": finished, "status": status,
        "sources": {}, "counts": {}}])


def test_a_run_that_changed_no_row_still_counts_as_a_refresh(rawstore_tmp_db):
    """``updated_at`` moves only when a row changes (core/identity/master.py ``changed``): a successful run that
    changed nothing still refreshed the master, so its ``finished_at`` is the age. A failed run is not a refresh."""
    _master("2026-09-01T12:00:00Z")
    _run("r1", "2026-09-30T00:06:00Z", "ok")
    _run("r2", "2026-10-01T00:06:00Z", "error")
    age = _rows(inventory.collect(now=FIXED))["identity"]["master_age"]
    assert age["date"] == "2026-09-30" and age["days"] == 1 and age["warn"] is False
    assert age["text"] == "Identity master: last refreshed 2026-09-30 (1 day ago)"

    _run("r3", "2026-10-01T00:06:00Z", "partial")       # a partial run builds and writes the master too
    age = _rows(inventory.collect(now=FIXED))["identity"]["master_age"]
    assert age["date"] == "2026-10-01" and age["days"] == 0
    assert age["text"] == "Identity master: last refreshed 2026-10-01 (0 days ago)"
