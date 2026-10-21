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


def test_the_row_shows_the_newest_master_date_and_its_age(rawstore_tmp_db):
    """Newest ``security_master.updated_at`` when no identity run is recorded."""
    _master("2026-09-01T12:00:00Z", "2026-09-30T12:00:00Z")
    payload = inventory.collect(now=FIXED)
    age = _rows(payload)["identity"]["master_age"]
    text = "Identity master: last refreshed 2026-09-30 (1 day ago)"
    assert age["text"] == text
    assert age["date"] == "2026-09-30" and age["days"] == 1 and age["warn"] is False
    html = report.render_settings_dashboard(payload)
    shown = _age_html(html)
    assert text in shown and "warnv" not in shown


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


def test_the_warning_shows_only_past_the_threshold(rawstore_tmp_db):
    assert inventory.IDENTITY_MASTER_STALE_DAYS == 7
    _master("2026-09-24T12:00:00Z")  # exactly 7 days: not past
    fresh = inventory.collect(now=FIXED)
    assert _rows(fresh)["identity"]["master_age"]["warn"] is False
    assert "warnv" not in _age_html(report.render_settings_dashboard(fresh))

    rawstore.execute("DELETE FROM security_master")
    _master("2026-09-23T12:00:00Z")  # 8 days: past
    stale = inventory.collect(now=FIXED)
    age = _rows(stale)["identity"]["master_age"]
    assert age["days"] == 8 and age["warn"] is True
    assert age["text"] == "Identity master: last refreshed 2026-09-23 (8 days ago)"
    shown = _age_html(report.render_settings_dashboard(stale))
    assert 'class="warnv"' in shown and "8 days ago" in shown


def test_unknown_shows_when_the_store_has_no_timestamp(rawstore_tmp_db):
    missing = inventory.collect(now=FIXED)
    assert _rows(missing)["identity"]["master_age"]["text"] == "Identity master: last refreshed unknown"
    assert missing["errors"] == []
    assert "unknown" in _age_html(report.render_settings_dashboard(missing))

    _master(None, "")
    blank = inventory.collect(now=FIXED)
    assert _rows(blank)["identity"]["master_age"]["text"] == "Identity master: last refreshed unknown"
    assert blank["errors"] == []
    assert _rows(blank)["identity"]["master_age"]["warn"] is False


def test_a_read_error_does_not_break_the_page(rawstore_tmp_db, monkeypatch):
    _master("2026-09-01T12:00:00Z")
    real = rawstore.query

    def boom(sql, *args, **kwargs):
        if "updated_at" in str(sql):
            raise RuntimeError("disk")
        return real(sql, *args, **kwargs)

    monkeypatch.setattr(rawstore, "query", boom)
    payload = inventory.collect(now=FIXED)  # must not raise
    html = report.render_settings_dashboard(payload)  # must not raise
    assert _rows(payload)["identity"]["master_age"]["text"] == "Identity master: last refreshed unknown"
    assert "unknown" in _age_html(html)
    assert "Identity spine" in html
    assert any(e.get("table") == "security_master" and e.get("where") == "_identity_master_age"
               for e in payload["errors"])


def test_the_age_note_escapes_its_text():
    evil = "<script>alert(1)</script>"
    html = report.render_settings_dashboard({"rows": [{
        "key": "identity", "label": "Identity spine", "note": "last run ok",
        "master_age": {"text": evil, "warn": True}}]})
    assert evil not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'id="identity-master-age" class="warnv"' in html
