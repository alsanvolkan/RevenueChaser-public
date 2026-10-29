"""#166: ``run_money_flow_latest`` never lets a partial leaderboard replace a whole one.

Since M-ROT-3 (#374, ROT-9) "a whole one" is the newest complete session in result history
(``history.stored_leaderboard``, read through ``runners._money_flow_stored``); the board file is the fallback
only when history cannot be read. An empty pull never replaces a board file with rows.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage

GOOD = {"inflows": [{"ticker": "UP", "name": "", "flow_score": 40.0}],
        "outflows": [{"ticker": "DN", "name": "", "flow_score": -40.0}],
        "generated_at": "2026-09-01T00:00:00", "as_of": "2026-08-29"}


def _partial(missing=("XLK", "SMH")):
    return {"inflows": [{"ticker": "NEW", "name": "", "flow_score": 10.0}], "outflows": [],
            "generated_at": None, "as_of": "2026-09-29", "partial": True, "missing": list(missing)}


@pytest.fixture
def latest(tmp_path, monkeypatch):
    out = tmp_path / "cache"
    path = out / "sector_rotation" / "sector_rotation_latest.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(runners, "OUTPUT_DIR", out)
    monkeypatch.setattr(runners, "MONEY_FLOW_LATEST", path)
    return path


def _compute(monkeypatch, result):
    import RevenueChaser.module_sector_rotation.money_flow as mf
    monkeypatch.setattr(mf, "compute_money_flow", lambda **k: dict(result))


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _stored(monkeypatch, board):
    monkeypatch.setattr(runners, "_money_flow_stored", lambda: board and {**board, "run_id": "job-0"})


def test_a_partial_result_does_not_overwrite_a_stored_whole_one(latest, monkeypatch):
    latest.write_text(json.dumps(GOOD), encoding="utf-8")
    _stored(monkeypatch, GOOD)
    _compute(monkeypatch, _partial())
    with pytest.raises(JobMessage) as exc:
        runners.run_money_flow_latest(uw=None)
    assert "XLK" in exc.value.message and "SMH" in exc.value.message
    assert "2026-08-29" in exc.value.message and "kept" in exc.value.message.lower()
    assert _read(latest) == GOOD


def test_with_nothing_stored_a_partial_result_is_saved_flagged(latest, monkeypatch):
    _compute(monkeypatch, _partial())
    with pytest.raises(JobMessage) as exc:
        runners.run_money_flow_latest(uw=None)
    assert "XLK" in exc.value.message and "partial" in exc.value.message.lower()
    saved = _read(latest)
    assert saved["partial"] is True and saved["missing"] == ["XLK", "SMH"] and saved["inflows"][0]["ticker"] == "NEW"
    assert saved["generated_at"]


def test_a_newer_partial_result_replaces_an_older_partial_one(latest, monkeypatch):
    latest.write_text(json.dumps({**GOOD, "partial": True, "missing": ["XLE"]}), encoding="utf-8")
    _compute(monkeypatch, _partial(("XLK",)))
    with pytest.raises(JobMessage):
        runners.run_money_flow_latest(uw=None)
    assert _read(latest)["missing"] == ["XLK"]


def test_a_whole_result_replaces_a_stored_partial_one_and_is_not_flagged(latest, monkeypatch):
    latest.write_text(json.dumps({**GOOD, "partial": True, "missing": ["XLE"]}), encoding="utf-8")
    _compute(monkeypatch, {"inflows": [{"ticker": "NEW"}], "outflows": [], "generated_at": None, "as_of": "2026-09-29"})
    assert runners.run_money_flow_latest(uw=None) == runners.MONEY_FLOW_LATEST
    saved = _read(latest)
    assert saved["inflows"] == [{"ticker": "NEW"}] and "partial" not in saved and "missing" not in saved


def test_a_whole_result_over_a_stored_whole_one_is_written_as_before(latest, monkeypatch):
    latest.write_text(json.dumps(GOOD), encoding="utf-8")
    whole = {"inflows": [{"ticker": "NEW"}], "outflows": [{"ticker": "OLD"}], "generated_at": None, "as_of": "2026-09-29"}
    _compute(monkeypatch, whole)
    runners.run_money_flow_latest(uw=None)
    saved = _read(latest)
    assert saved["inflows"] == [{"ticker": "NEW"}] and list(saved) == ["inflows", "outflows", "generated_at", "as_of"]


def test_an_empty_partial_result_keeps_the_stored_leaderboard(latest, monkeypatch):
    latest.write_text(json.dumps(GOOD), encoding="utf-8")
    _compute(monkeypatch, {"inflows": [], "outflows": [], "generated_at": None, "as_of": None,
                           "partial": True, "missing": ["XLK"]})
    with pytest.raises(JobMessage, match=r"(?i)kept"):
        runners.run_money_flow_latest(uw=None)
    assert _read(latest) == GOOD


def test_when_history_cannot_be_read_the_board_file_guards_as_before(latest, monkeypatch):
    latest.write_text(json.dumps(GOOD), encoding="utf-8")

    def boom():
        raise RuntimeError("history locked")
    monkeypatch.setattr(runners, "_money_flow_stored", boom)
    _compute(monkeypatch, _partial())
    with pytest.raises(JobMessage, match=r"kept the stored leaderboard from 2026-08-29"):
        runners.run_money_flow_latest(uw=None)
    assert _read(latest) == GOOD


def test_a_partial_board_file_gives_way_to_the_complete_session_history_keeps(latest, monkeypatch):
    """Review 2026-10-16: the job says it kept the stored complete leaderboard, so the board shows that one, not the
    partial file left by a run whose history read failed."""
    latest.write_text(json.dumps({**GOOD, "inflows": [{"ticker": "OLD", "name": "", "flow_score": 5.0}],
                                  "as_of": "2026-09-15", "partial": True, "missing": ["XLE"]}), encoding="utf-8")
    _stored(monkeypatch, GOOD)
    _compute(monkeypatch, _partial())
    with pytest.raises(JobMessage, match="kept the stored leaderboard from 2026-08-29"):
        runners.run_money_flow_latest(uw=None)
    assert _read(latest) == GOOD


def test_a_whole_board_file_history_does_not_back_does_not_block_a_partial_result(latest, monkeypatch):
    latest.write_text(json.dumps(GOOD), encoding="utf-8")
    _stored(monkeypatch, None)
    _compute(monkeypatch, _partial())
    with pytest.raises(JobMessage, match="saved flagged"):
        runners.run_money_flow_latest(uw=None)
    assert _read(latest)["partial"] is True
