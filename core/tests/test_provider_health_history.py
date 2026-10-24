"""Provider health as a structured ``result_runs`` column (issue PH-1, "#23").

A board run already summarises its provider calls into prose (``result_runs.quality_note``); this column keeps the
same ``ProviderHealth.health()`` dict as data, so a 14-day view can read it without parsing the prose. One additive
JSON column, ``provider_health``: nullable, no default, added to an existing store file by ``_reconcile_table``.
``_SCHEMA_VERSION`` stays "12". Temp stores only, no network.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from RevenueChaser.core import history, rawstore
from RevenueChaser.module_earnings_radar import history as earnings_history
from RevenueChaser.module_leaps import history as leaps_history
from RevenueChaser.module_oipd import history as oipd_history
from RevenueChaser.module_sector_rotation import history as rotation_history

# 2026-10-06 is a Tuesday: 22:00 UTC is 18:00 ET (the session is complete).
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()

HEALTH = {"provider": "uw", "calls": 12, "failed": 1, "auth_error": None,
          "failures": [{"endpoint": "/api/screener", "error": "HTTP 500"}],
          "breaker_open": False, "skipped_calls": 0}

# The result_runs shape before this change (schema 12), for the reconcile test.
_OLD_RESULT_RUNS = (
    "CREATE TABLE result_runs (board TEXT, run_id TEXT, session_date DATE, run_at DOUBLE, quality TEXT, "
    "quality_note TEXT, session_snapshot BOOLEAN, model_version TEXT, params JSON, context JSON, funnel JSON, "
    "row_counts JSON, warnings JSON, error TEXT, payload BLOB, PRIMARY KEY (board, run_id))")


def _raw(board: str, run_id: str):
    """The stored cell, as DuckDB returns it (JSON text, not decoded)."""
    rows = rawstore.query("SELECT provider_health FROM result_runs WHERE board = ? AND run_id = ?",
                          [board, run_id], db="history")
    assert len(rows) == 1, f"{board}/{run_id} was not recorded"
    return rows[0]["provider_health"]


def _stored(board: str, run_id: str):
    """The stored cell as the history readers decode it."""
    rows = rawstore.query("SELECT * FROM result_runs WHERE board = ? AND run_id = ?", [board, run_id], db="history")
    assert len(rows) == 1, f"{board}/{run_id} was not recorded"
    return rawstore._decode_row("result_runs", rows[0])["provider_health"]


def _columns(table: str = "result_runs") -> dict[str, str]:
    return {r["column_name"]: r["data_type"] for r in rawstore.query(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?", [table], db="history")}


# ── the writer (1, 2, 4) ────────────────────────────────────────────────────────────────────────────────────────


def test_record_run_stores_a_provider_health_dict_as_json(rawstore_tmp_db):
    history.record_run("module_earnings_radar", run_id="ph1", run_at=COMPLETE, model_version="m", rows={},
                       provider_health=HEALTH)
    assert _stored("module_earnings_radar", "ph1") == HEALTH
    raw = _raw("module_earnings_radar", "ph1")
    assert isinstance(raw, str) and isinstance(json.loads(raw), dict), "the value was JSON-encoded more than once"
    assert json.loads(raw) == HEALTH


def test_record_run_does_not_double_encode_an_already_encoded_value(rawstore_tmp_db):
    """A caller may hand over an encoded payload; it must still be stored as JSON once."""
    history.record_run("module_earnings_radar", run_id="ph1", run_at=COMPLETE, model_version="m", rows={},
                       provider_health=json.dumps(HEALTH))
    assert json.loads(_raw("module_earnings_radar", "ph1")) == HEALTH


def test_record_run_without_provider_health_stores_null(rawstore_tmp_db):
    history.record_run("module_earnings_radar", run_id="ph1", run_at=COMPLETE, model_version="m", rows={})
    assert _raw("module_earnings_radar", "ph1") is None


def test_a_list_of_per_provider_dicts_round_trips_as_a_list(rawstore_tmp_db):
    per_provider = [HEALTH, {**HEALTH, "provider": "yfinance", "calls": 3, "failed": 0, "failures": []}]
    history.record_run("module_earnings_radar", run_id="ph1", run_at=COMPLETE, model_version="m", rows={},
                       provider_health=per_provider)
    assert _stored("module_earnings_radar", "ph1") == per_provider


# ── a store file that predates the column (3) ─────────────────────────────────────────────────────────────────


def test_an_existing_store_gains_the_column_and_its_old_rows_read_null(rawstore_tmp_db):
    with rawstore.connect(db="history") as conn:
        conn.execute(_OLD_RESULT_RUNS)
        conn.execute("INSERT INTO result_runs (board, run_id, run_at, quality) "
                     "VALUES ('module_earnings_radar', 'old', 1.0, 'complete')")
    assert "provider_health" not in _columns()

    history.record_run("module_earnings_radar", run_id="ph1", run_at=COMPLETE, model_version="m", rows={},
                       provider_health=HEALTH)

    assert _columns()["provider_health"] == "JSON"
    assert _raw("module_earnings_radar", "old") is None
    assert _stored("module_earnings_radar", "ph1") == HEALTH


# ── the five call sites pass the same dict that made the prose warning ────────────────────────────────────────


def test_earnings_radar_records_the_uw_health_it_summarised(rawstore_tmp_db):
    earnings_history.record([], {}, run_id="e1", run_at=COMPLETE, uw_health=HEALTH)
    assert _stored("module_earnings_radar", "e1") == HEALTH


def test_leaps_records_the_uw_health_it_summarised(rawstore_tmp_db):
    leaps_history.record([], run_id="l1", run_at=COMPLETE, uw_health=HEALTH)
    assert _stored("module_leaps", "l1") == HEALTH


def test_oipd_records_the_uw_health_it_summarised(rawstore_tmp_db):
    oipd_history.record({"ticker": "TST"}, {}, run_id="o1", run_at=COMPLETE, uw_health=HEALTH)
    assert _stored("module_oipd", "o1") == HEALTH


def test_rotation_records_the_uw_health_it_summarised(rawstore_tmp_db):
    rotation_history.record_rotation([], {}, {}, run_id="rot1", run_at=COMPLETE, uw_health=HEALTH)
    assert _stored("module_sector_rotation", "rot1") == HEALTH


def test_money_flow_records_the_uw_health_it_summarised(rawstore_tmp_db):
    rotation_history.record_money_flow({"inflows": [], "outflows": []}, run_id="mf1", run_at=COMPLETE,
                                       days=5, window=20, top_n=10, uw_health=HEALTH)
    assert _stored("module_money_flow", "mf1") == HEALTH


def test_the_readers_return_the_column(rawstore_tmp_db):
    """#23 review MUST-1: the column was write-only — `_HEADER` did not list it, so `last_run`, `runs` and
    `sessions` never returned it and the 14-day view PH-2 is meant to build had nothing to read."""
    history.record_run("module_oipd", run_id="r1", run_at=COMPLETE, model_version="v1", provider_health=HEALTH)
    assert history.last_run("module_oipd")["provider_health"] == HEALTH
    assert history.runs("module_oipd")[0]["provider_health"] == HEALTH
    board = next((b for b in ("module_earnings_radar", "module_sector_rotation", "module_leaps", "module_oipd")
                  if history.is_session_board(b)), None)
    if board:                                  # sessions() lists snapshots only; a run board has none
        history.record_run(board, run_id="s1", run_at=COMPLETE, model_version="v1", provider_health=HEALTH)
        snaps = history.sessions(board)
        assert snaps and snaps[0]["provider_health"] == HEALTH


def test_a_non_json_string_keeps_the_run(rawstore_tmp_db):
    """#23 review SHOULD-1: the decode guard used to raise, which lost the header AND the run's rows."""
    history.record_run("module_oipd", run_id="r2", run_at=COMPLETE, model_version="v1",
                       provider_health="not json at all")
    assert history.last_run("module_oipd")["run_id"] == "r2"
