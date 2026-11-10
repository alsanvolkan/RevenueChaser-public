"""SPX Gamma review PR 3: a Fetch records its result history through ``core/history.record_run``; a Refresh or a
stored past day records nothing (06 rule 9, F2)."""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.core import rawstore
from RevenueChaser.module_spx_gamma import history as spx_history
from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot

from .conftest import make_raw

DAY = "2026-08-14"                                                       # a Friday
AFTER_CLOSE = datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc).timestamp()       # 17:00 New York: complete
NEXT_MONDAY = datetime(2026, 8, 17, 21, 0, tzinfo=timezone.utc).timestamp()


def _out():
    snapshot = normalize_snapshot(make_raw(), DAY)
    return {"snapshot": snapshot, "latest": {"asof_date": DAY, "requested_date": DAY}, "fetched": True}


def _header(run_id):
    return rawstore.query("SELECT quality, quality_note, session_date, session_snapshot FROM result_runs "
                          "WHERE board = ? AND run_id = ?", [spx_history.BOARD, run_id], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {spx_history.TABLE} WHERE run_id = ?", [run_id], db="history")


def test_a_fetch_records_its_header_and_levels_row():
    out = _out()
    spx_history.record(out, run_id="r1", run_at=AFTER_CLOSE)
    [head] = _header("r1")
    assert head["quality"] == "complete" and str(head["session_date"]) == DAY and head["session_snapshot"]
    [row] = _rows("r1")
    snap = out["snapshot"]
    assert str(row["asof_date"]) == DAY
    assert row["spot"] == snap["current_level"] and row["regime"] == snap["regime"]
    assert row["call_wall"] == snap["levels"]["upper_resistance_band"]
    assert row["s_dir"] == snap["prediction"]["s_dir"]
    from RevenueChaser.module_spx_gamma.scenarios import build_scenarios
    shown = {s["name"]: s["p"] for s in build_scenarios(snap)["scenarios"]}
    assert (row["p_consolidation"], row["p_upside"], row["p_downside"]) == \
        (shown.get("Consolidation"), shown.get("Upside"), shown.get("Downside"))


def test_a_fetch_of_another_day_keeps_its_header_only():
    spx_history.record(_out(), run_id="r2", run_at=NEXT_MONDAY)
    [head] = _header("r2")
    assert head["quality"] == "partial" and "not the session 2026-08-17" in head["quality_note"]
    assert _rows("r2") == []


def test_failed_uw_calls_make_the_run_partial():
    spx_history.record(_out(), run_id="r3", run_at=AFTER_CLOSE, uw_health={"calls": 10, "failed": 2})
    assert _header("r3")[0]["quality"] == "partial" and _rows("r3") == []


def test_the_runner_records_a_fetch_and_not_a_stored_day(monkeypatch):
    from RevenueChaser.app import runners
    from RevenueChaser.module_spx_gamma import run as spx_run

    recorded = []
    monkeypatch.setattr(spx_history, "record", lambda out, **k: recorded.append(out))
    monkeypatch.setattr(runners, "spx_builder_request", lambda: None)
    monkeypatch.setattr(runners, "_SPX_VIEW", None, raising=False)

    monkeypatch.setattr(spx_run, "run", lambda *a, **k: {"fetched": True, "paths": {"report_html": "x"}})
    runners.run_spxgamma(None, DAY)
    monkeypatch.setattr(spx_run, "run", lambda *a, **k: {"paths": {"report_html": "x"}})    # served from disk
    runners.run_spxgamma(None, DAY)
    assert len(recorded) == 1


def test_a_history_failure_is_a_job_note_not_a_failed_fetch(monkeypatch):
    from RevenueChaser.app import runners
    from RevenueChaser.core import ledger
    from RevenueChaser.module_spx_gamma import run as spx_run

    def boom(*a, **k):
        raise OSError("history locked")
    notes = []
    monkeypatch.setattr(spx_history, "record", boom)
    monkeypatch.setattr(ledger, "add_note", notes.append)
    monkeypatch.setattr(runners, "spx_builder_request", lambda: None)
    monkeypatch.setattr(spx_run, "run", lambda *a, **k: {"fetched": True, "paths": {"report_html": "x"}})
    assert runners.run_spxgamma(None, DAY) == "x"
    assert notes and "history locked" in notes[0]
