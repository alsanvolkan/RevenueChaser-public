"""#258: the Financials quick-state JSON is written atomically.

``QUICK_STATE`` (financials_latest.json) is read by the Financials tab on every
view, while a Refresh renders in a request thread and the job worker may write
the same file. A plain ``write_text`` can leave a half-written file, which the
reader sees as empty state. The writer goes through ``reporting_common.write_artifact``
(temp file in the same folder, then ``os.replace``), so a failed replace leaves
the previous file whole.
"""
from __future__ import annotations

import os

import pytest


def _patch_state(monkeypatch, tmp_path):
    import RevenueChaser.app.runners as rn
    monkeypatch.setattr(rn, "QUICK_STATE", tmp_path / "financials" / "financials_latest.json")
    return rn


def test_a_failed_replace_leaves_the_previous_state_intact(tmp_path, monkeypatch):
    rn = _patch_state(monkeypatch, tmp_path)
    rn._remember_quick_ticker("aapl")
    before = rn.QUICK_STATE.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        rn._remember_quick_ticker("msft")
    assert rn.QUICK_STATE.read_text(encoding="utf-8") == before


def test_no_temp_file_is_left_behind_after_a_failed_replace(tmp_path, monkeypatch):
    rn = _patch_state(monkeypatch, tmp_path)
    rn._remember_quick_ticker("aapl")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        rn._remember_quick_ticker("msft")
    leftovers = [p.name for p in rn.QUICK_STATE.parent.iterdir() if p.name != rn.QUICK_STATE.name]
    assert leftovers == [], f"temporary files left behind: {leftovers}"
