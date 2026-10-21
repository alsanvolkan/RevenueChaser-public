"""Common K2 (#383 R1): the result-history readers ``last_run`` / ``runs`` / ``sessions`` in ``core/history``.

- ``last_run(board)``: the newest header (``warnings``, ``context``, ``quality``, ...), decoded; the compressed board
  ``payload`` only when asked.
- ``runs(board, limit)``: every recorded run of a run board (or the headers of a session board), newest first.
- ``sessions(board, limit)``: a session board's snapshots, one per session, newest first, with the session's run
  count.
- A read never creates ``history.duckdb``: an absent file reads as ``[]`` / ``None`` (J3).
- AG's ``recorded_sessions`` reads through ``sessions``.
- The shared rule (C22): no Refresh path (``no_fetch``) reaches ``record_run``.
Temp stores only.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import history, rawstore

TUE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()            # 18:00 ET: session complete
TUE_LATER = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc).timestamp()
WED = datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc).timestamp()
WED_INTRADAY = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc).timestamp()

SESSION_BOARD, SESSION_TABLE = "module_money_flow", "money_flow_history"
RUN_BOARD, RUN_TABLE = "module_earnings_radar", "earnings_cards_history"


def _flow(*ranks):
    return [{"side": "in", "rank": r, "etf": f"E{r}", "name": "n", "flow_score": float(r)} for r in ranks]


@pytest.fixture
def store(rawstore_tmp_db):
    return rawstore_tmp_db


def _session_runs():
    history.record_run(SESSION_BOARD, run_id="tue", run_at=TUE, model_version="m1", rows={SESSION_TABLE: _flow(1)},
                       context={"n": 1}, warnings=["w1"], payload={"board": "tue"})
    history.record_run(SESSION_BOARD, run_id="tue-2", run_at=TUE_LATER, model_version="m1",
                       rows={SESSION_TABLE: _flow(1, 2)}, context={"n": 2})                  # header only (rule B)
    history.record_run(SESSION_BOARD, run_id="wed-early", run_at=WED_INTRADAY, model_version="m1",
                       rows={SESSION_TABLE: _flow(3)})                                       # intraday: header only
    history.record_run(SESSION_BOARD, run_id="wed", run_at=WED, model_version="m2", rows={SESSION_TABLE: _flow(4)},
                       context={"n": 4}, payload={"board": "wed", "cards": [1, 2]})


def test_an_absent_history_file_reads_empty_and_is_not_created(store):
    assert not rawstore.history_db_path().exists()
    assert history.last_run(SESSION_BOARD) is None
    assert history.last_run(SESSION_BOARD, payload=True) is None
    assert history.runs(RUN_BOARD) == []
    assert history.sessions(SESSION_BOARD) == []
    assert not rawstore.history_db_path().exists()


def test_last_run_is_the_newest_header_decoded(store):
    _session_runs()
    h = history.last_run(SESSION_BOARD)
    assert (h["run_id"], h["model_version"], h["quality"]) == ("wed", "m2", "complete")
    assert h["context"] == {"n": 4} and h["session_date"] == date(2026, 10, 7)
    assert h["row_counts"] == {SESSION_TABLE: 1}
    assert "payload" not in h                              # the board payload only when asked
    assert history.last_run(SESSION_BOARD, payload=True)["payload"] == {"board": "wed", "cards": [1, 2]}
    assert history.last_run(RUN_BOARD) is None             # another board's runs are not this board's


def test_last_run_carries_warnings_and_the_partial_reason(store):
    history.record_run(RUN_BOARD, run_id="r1", run_at=TUE, model_version="m", warnings=["2 of 9 calls failed"],
                       partial="the chain was partial", rows={RUN_TABLE: []})
    h = history.last_run(RUN_BOARD)
    assert h["warnings"] == ["2 of 9 calls failed"]
    assert h["quality"] == "partial" and h["quality_note"] == "the chain was partial"


def test_runs_lists_every_run_newest_first_with_a_limit(store):
    for i, when in enumerate((TUE, TUE_LATER, WED)):
        history.record_run(RUN_BOARD, run_id=f"r{i}", run_at=when, model_version="m",
                           rows={RUN_TABLE: [{"ticker": f"T{i}"}]})
    assert [r["run_id"] for r in history.runs(RUN_BOARD)] == ["r2", "r1", "r0"]
    assert [r["run_id"] for r in history.runs(RUN_BOARD, limit=2)] == ["r2", "r1"]
    assert all("payload" not in r for r in history.runs(RUN_BOARD))


def test_sessions_lists_one_snapshot_per_session_with_its_run_count(store):
    _session_runs()
    wed, tue = history.sessions(SESSION_BOARD)
    assert (wed["session_date"], wed["run_id"], wed["runs"]) == (date(2026, 10, 7), "wed", 2)
    assert (tue["session_date"], tue["run_id"], tue["runs"]) == (date(2026, 10, 6), "tue", 2)
    assert tue["warnings"] == ["w1"] and tue["context"] == {"n": 1}
    assert [s["run_id"] for s in history.sessions(SESSION_BOARD, limit=1)] == ["wed"]


def test_a_run_board_has_no_sessions(store):
    history.record_run(RUN_BOARD, run_id="r1", run_at=TUE, model_version="m", rows={RUN_TABLE: []})
    assert history.sessions(RUN_BOARD) == []


def test_ag_recorded_sessions_reads_through_sessions(store, monkeypatch):
    from RevenueChaser.module_asymmetric_growth import history as ag_history
    seen = []
    real = history.sessions
    monkeypatch.setattr(history, "sessions", lambda board, limit=10: seen.append((board, limit)) or real(board, limit))
    assert ag_history.recorded_sessions(limit=3) == []
    assert seen == [(ag_history.BOARD, 3)]
