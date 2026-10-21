"""#258: the UW Interaction last-run payload is written atomically.

``UW_STATE_JSON`` is read by the board after every Fetch, while a Refresh may
re-read it concurrently. A plain ``write_text`` can leave a half-written file,
which ``load_state`` returns as ``None`` (an empty board). The writer goes
through ``reporting_common.write_artifact`` (temp file in the same folder, then
``os.replace``), so a failed replace leaves the previous payload whole.
"""
from __future__ import annotations

import json
import os

import pytest

from RevenueChaser.module_uw_interaction import config as uwcfg


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    d = tmp_path / "uw_interaction"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(uwcfg, "UW_STATE_DIR", d)
    monkeypatch.setattr(uwcfg, "UW_STATE_JSON", d / "uw_interaction_latest.json")
    return uwcfg.UW_STATE_JSON


def test_a_failed_replace_leaves_the_previous_payload_intact(state_path, monkeypatch):
    uwcfg.save_state({"rows": [{"ticker": "AAPL"}], "row_count": 1})
    before = state_path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        uwcfg.save_state({"rows": [{"ticker": "MSFT"}], "row_count": 1})
    assert state_path.read_text(encoding="utf-8") == before
    assert json.loads(state_path.read_text(encoding="utf-8"))["rows"][0]["ticker"] == "AAPL"


def test_no_temp_file_is_left_behind_after_a_failed_replace(state_path, monkeypatch):
    uwcfg.save_state({"rows": [], "row_count": 0})

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        uwcfg.save_state({"rows": [{"ticker": "MSFT"}], "row_count": 1})
    leftovers = [p.name for p in state_path.parent.iterdir() if p.name != state_path.name]
    assert leftovers == [], f"temporary files left behind: {leftovers}"
