"""The edgar-sync job passes its run id to the submissions sweep and reports it (#299, D15)."""
from __future__ import annotations

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.module_value_chain import sync

_BASE = {"requested": 3, "updated": 1, "current": 2, "no_filing": 0, "failed": 0}
_SUBS = {"ciks": 7, "requested": 8, "filings_appended": 40, "headers_appended": 2, "headers_unchanged": 5,
         "headers_absent": 0, "failed": 0, "failed_ciks": []}


def _run(monkeypatch, tmp_path, res):
    seen = {}

    def fake_sync(**kw):
        seen.update(kw)
        return res

    monkeypatch.setattr(sync, "sync_filings", fake_sync)
    monkeypatch.setattr(runners, "run_value_chain", lambda uw=None, no_fetch=False: tmp_path / "vc.html")
    monkeypatch.setattr(runners, "run_id", lambda: "job-42")
    with pytest.raises(JobMessage) as exc:
        runners.run_edgar_sync()
    return exc.value.message, seen


def test_the_job_id_stamps_the_sweep(monkeypatch, tmp_path):
    _msg, seen = _run(monkeypatch, tmp_path, {**_BASE, "submissions": _SUBS})
    assert seen["run_id"] == "job-42"


def test_the_message_reports_the_sweep(monkeypatch, tmp_path):
    msg, _ = _run(monkeypatch, tmp_path, {**_BASE, "submissions": _SUBS})
    assert "SEC submissions: 7 of 8 CIKs answered, 40 new filings, 2 header changes" in msg
    assert "failed" not in msg and "not stored" not in msg and "no longer served" not in msg


def test_failed_ciks_are_named(monkeypatch, tmp_path):
    subs = {**_SUBS, "failed": 12, "failed_ciks": [str(i) for i in range(12)], "headers_absent": 1}
    msg, _ = _run(monkeypatch, tmp_path, {**_BASE, "submissions": subs})
    assert "12 CIKs failed (0, 1, 2, 3, 4, 5, 6, 7, 8, 9 and 2 more)" in msg
    assert "1 no longer served" in msg


def test_a_store_error_and_pending_are_named(monkeypatch, tmp_path):
    subs = {**_SUBS, "error": "RawStoreError: edgar locked", "pending": 3}
    msg, _ = _run(monkeypatch, tmp_path, {**_BASE, "submissions": subs})
    assert "not stored: RawStoreError: edgar locked" in msg and "3 CIKs pending" in msg


def test_no_sweep_adds_nothing(monkeypatch, tmp_path):
    msg, _ = _run(monkeypatch, tmp_path, dict(_BASE))
    assert "SEC submissions" not in msg
