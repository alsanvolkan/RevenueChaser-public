"""#374 Rotation and Money Flow writers: the dashboard Fetches record their boards in result history. Offline, temp
stores, no UW call.

Fetch only (the Rotation Refresh renders from stored snapshots and records nothing); the header carries the job's id;
the history is recorded after the board file; a Money Flow run that kept the stored leaderboard still records its
header; a history failure is a job note and never blocks the Fetch.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_sector_rotation import engine as rot_engine, history as rot_history, money_flow
from RevenueChaser.module_sector_rotation import report as rot_report, store as rotation_store
from RevenueChaser.module_sector_rotation.tests.test_rotation_history_374 import FLOW, HOLDINGS, SNAPS, TREE

COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()   # Tuesday 18:00 ET
SAVED = {"as_of_date": "2026-10-06", "ticker": "XLK", "name": "Technology", "group": "sector", "close_price": 1.0,
         "rs_1w": 0.1, "rs_1m": 0.2, "rs_3m": 0.3, "rs_ratio": 0.4, "rs_momentum": 0.5, "rs_ratio_z": 0.6,
         "rs_momentum_z": 0.7, "quadrant": "LEADING", "benchmark": "SPY", "generated_at": "x", "source": "live"}


class _UW:
    def health(self):
        return {"calls": 40, "failed": 0, "auth_error": None, "failures": []}


@pytest.fixture
def fetch(rawstore_tmp_db, monkeypatch, tmp_path):
    ledger.take_notes()
    monkeypatch.setattr(runners, "run_id", lambda: "job-x")
    monkeypatch.setattr(runners, "time", SimpleNamespace(time=lambda: COMPLETE))
    return rawstore_tmp_db


@pytest.fixture
def rotation(fetch, monkeypatch):
    order = []

    class _Engine:
        def __init__(self, uw, benchmark="SPY"):
            pass

        def process_all(self):
            return list(SNAPS)

    monkeypatch.setattr(rot_engine, "RotationEngine", _Engine)
    monkeypatch.setattr(rot_engine, "fetch_all_holdings", lambda uw, snaps: HOLDINGS)
    monkeypatch.setattr(rot_engine, "sector_etf_trees", lambda: TREE)
    monkeypatch.setattr(rotation_store, "save_snapshot", lambda snaps, benchmark="SPY", **k: [dict(SAVED)])
    monkeypatch.setattr(rot_report, "export_rotation_dashboard",
                        lambda *a, **k: order.append("board") or runners.ROTATION_LATEST)
    return order


@pytest.fixture
def flow(fetch, monkeypatch, tmp_path):
    monkeypatch.setattr(runners, "MONEY_FLOW_LATEST", tmp_path / "mf.json")
    monkeypatch.setattr(runners, "_write_output",
                        lambda m, content, s, ext="html": (tmp_path / "mf.json").write_text(content))
    monkeypatch.setattr(money_flow, "compute_money_flow", lambda **k: json.loads(json.dumps(FLOW)))


def _headers(board):
    return rawstore.query("SELECT * FROM result_runs WHERE board = ?", [board], db="history")


# ── Rotation ──

def test_a_rotation_fetch_records_the_holdings_and_tree_under_the_job_id(rotation):
    runners.run_rotation_latest(_UW())
    (head,) = _headers("module_sector_rotation")
    assert head["run_id"] == "job-x" and head["quality"] == "complete"
    assert json.loads(head["row_counts"]) == {"rotation_holdings_history": 3, "sector_tree_history": 3,
                                              "rotation_snapshots_history": 1}


def test_the_rotation_history_is_recorded_after_the_board_file(rotation, monkeypatch):
    monkeypatch.setattr(rot_history, "record_rotation", lambda *a, **k: rotation.append("history"))
    runners.run_rotation_latest(_UW())
    assert rotation == ["board", "history"]


def test_the_runner_hands_the_uw_health_and_the_range(rotation, monkeypatch):
    from datetime import date
    seen = {}
    monkeypatch.setattr(rotation_store, "backfill_weekly", lambda uw, s, e: 0)
    monkeypatch.setattr(runners, "_load_rotation_snapshots", lambda start=None, end=None: list(SNAPS))
    monkeypatch.setattr(rot_history, "record_rotation", lambda *a, **k: seen.update(k))
    runners.run_rotation_latest(_UW(), start="2026-08-31", end="2026-08-01")
    assert seen["uw_health"]["calls"] == 40 and seen["window"] == (date(2026, 8, 1), date(2026, 8, 31))
    assert seen["run_id"] == "job-x" and seen["warnings"] is None and seen["snapshot_rows"] == []


def test_the_backfill_note_rides_on_the_header(rotation, monkeypatch):
    monkeypatch.setattr(rotation_store, "backfill_weekly",
                        lambda uw, s, e: SimpleNamespace(message=lambda: "2 days were not written"))
    monkeypatch.setattr(runners, "_load_rotation_snapshots", lambda start=None, end=None: list(SNAPS))
    with pytest.raises(JobMessage, match="2 days were not written"):
        runners.run_rotation_latest(_UW(), start="2026-08-01", end="2026-08-31")
    (head,) = _headers("module_sector_rotation")
    assert json.loads(head["warnings"]) == ["2 days were not written"]


def test_a_rotation_history_failure_is_a_job_note(rotation, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(rot_history, "record_rotation", boom)
    assert runners.run_rotation_latest(_UW()) == runners.ROTATION_LATEST
    assert any("Rotation result history was not recorded" in n and "history file locked" in n
               for n in ledger.take_notes())


# ── Money Flow ──

def test_a_money_flow_fetch_records_its_leaderboard_under_the_job_id(flow):
    assert runners.run_money_flow_latest(_UW()) == runners.MONEY_FLOW_LATEST
    (head,) = _headers("module_money_flow")
    assert head["run_id"] == "job-x" and json.loads(head["row_counts"]) == {"money_flow_history": 3}
    assert json.loads(head["params"])["top_n"] == 10 and json.loads(head["params"])["window"] == 21


def test_the_money_flow_history_is_recorded_after_the_file(flow, monkeypatch):
    order = []
    real = runners._write_output
    monkeypatch.setattr(runners, "_write_output", lambda *a, **k: (order.append("board"), real(*a, **k))[1])
    monkeypatch.setattr(rot_history, "record_money_flow", lambda *a, **k: order.append("history"))
    runners.run_money_flow_latest(_UW())
    assert order == ["board", "history"]


def test_a_kept_leaderboard_still_records_a_partial_header(flow, monkeypatch, tmp_path):
    (tmp_path / "mf.json").write_text(json.dumps({**FLOW, "as_of": "2026-10-05"}))
    monkeypatch.setattr(money_flow, "compute_money_flow", lambda **k: {"inflows": [], "outflows": [],
                                                                           "as_of": None})
    with pytest.raises(JobMessage, match="kept the"):
        runners.run_money_flow_latest(_UW())
    (head,) = _headers("module_money_flow")
    assert head["quality"] == "partial" and "kept the stored leaderboard from 2026-10-05" in head["quality_note"]


def test_a_partial_pull_that_keeps_a_whole_board_records_a_partial_header(flow, monkeypatch, tmp_path):
    # The guard is the newest complete session in history (M-ROT-3, ROT-9): a complete run of the day before.
    rot_history.record_money_flow({**FLOW, "as_of": "2026-10-05"}, run_id="job-w", run_at=COMPLETE - 86400, days=30,
                                  window=21, top_n=10)
    (tmp_path / "mf.json").write_text(json.dumps({**FLOW, "as_of": "2026-10-05"}))
    monkeypatch.setattr(money_flow, "compute_money_flow",
                        lambda **k: {**FLOW, "partial": True, "missing": ["XLB"]})
    with pytest.raises(JobMessage, match="kept the stored leaderboard from 2026-10-05"):
        runners.run_money_flow_latest(_UW())
    (head,) = [h for h in _headers("module_money_flow") if h["run_id"] == "job-x"]
    assert "XLB" in head["quality_note"] and "kept the stored leaderboard" in head["quality_note"]


def test_a_money_flow_history_failure_is_a_job_note(flow, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(rot_history, "record_money_flow", boom)
    assert runners.run_money_flow_latest(_UW()) == runners.MONEY_FLOW_LATEST
    assert any("Money Flow result history was not recorded" in n and "history file locked" in n
               for n in ledger.take_notes())


def test_the_rotation_refresh_records_nothing(fetch, monkeypatch):
    from RevenueChaser.app import dashboard
    monkeypatch.setattr(rotation_store, "list_snapshot_dates", lambda: ["2026-10-06"])
    monkeypatch.setattr(rotation_store, "load_snapshot", lambda d: {"etfs": [{"ticker": "XLK"}]})
    monkeypatch.setattr(rot_report, "_rows_to_snapshots", lambda rows: list(SNAPS))
    monkeypatch.setattr(rot_report, "export_rotation_dashboard", lambda *a, **k: runners.ROTATION_LATEST)
    dashboard._render_rotation_dashboard()
    assert not rawstore.history_db_path().exists() or _headers("module_sector_rotation") == []
