"""Relatedness board -> result history (#388, A3). Offline; the store is a temp DuckDB, no LLM call.

A Relatedness Fetch (dashboard or off-peak CLI, both through ``engine._run``) records its run through
``core/history.record_run``: one ``result_runs`` header with the board payload plus the ``relatedness_scores`` rows
in ``relatedness_scores_history`` (its mirror). Run board: every run keeps its rows; a peak stop or a run that scored
nothing is partial.
"""
from __future__ import annotations

import json
import zlib
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.core.providers import deepseek
from RevenueChaser.module_relatedness import config, engine, history as rel_history, store
from RevenueChaser.module_relatedness.tests.test_relatedness import _prof, _seed_filings, _seed_universe

BOARD, TABLE = "module_relatedness", "relatedness_scores_history"
TICKERS = [f"T{i:02d}" for i in range(6)]
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def seeded(rawstore_tmp_db, monkeypatch, tmp_path):
    ledger.take_notes()
    _seed_universe(TICKERS)
    _seed_filings(TICKERS)
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    for i, t in enumerate(TICKERS):
        store.save_profile(t, "cik-" + t, _prof(products=["gpu", f"p{i % 2}"], end_markets=["dc"]), "k", "m")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: {"no_fetch": True})
    monkeypatch.setattr(engine, "build_profiles", lambda tickers, **k: store.load_profiles(tickers))
    return TICKERS


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ? ORDER BY ticker, peer", [run_id], db="history")


def _unpack(cell):
    if isinstance(cell, (bytes, bytearray)):
        return json.loads(zlib.decompress(cell))
    return cell if not isinstance(cell, str) else json.loads(cell)


def test_a_fetch_records_the_scores_it_saved_and_the_payload(seeded):
    payload = engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    (head,) = _headers()
    assert head["run_id"] == "r1" and head["error"] is None and head["quality_note"] is None
    latest = rawstore.query("SELECT ticker, peer, score FROM relatedness_scores ORDER BY ticker, peer")
    assert [{k: r[k] for k in ("ticker", "peer", "score")} for r in _rows("r1")] == latest and latest
    assert json.loads(head["row_counts"]) == {TABLE: len(latest)}
    stored = _unpack(head["payload"])
    assert stored["n"] == payload["n"] == len(seeded) and stored["peers"] == payload["peers"]


def test_the_header_carries_model_version_params_and_context(seeded):
    engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    (head,) = _headers()
    assert head["model_version"] == rel_history.model_version() and head["model_version"].startswith("2+")
    assert json.loads(head["params"])["TOP_PEERS"] == config.TOP_PEERS
    ctx = json.loads(head["context"])
    assert ctx["requested"] == len(seeded) and ctx["profiled"] == len(seeded) and ctx["pending"] == 0


@pytest.mark.parametrize("name, value", [("TOP_PEERS", 10), ("PROMPT_VERSION", "v3"), ("REASONING_EFFORT", "low"),
                                         ("FIELD_WEIGHTS", {"products": 2.0}), ("PILOT_N", 100),
                                         ("MIN_INPUT_CHARS", 500), ("MAX_INPUT_CHARS", 12_000)])
def test_model_version_changes_with_a_setting(monkeypatch, name, value):
    before = rel_history.model_version()
    monkeypatch.setattr(config, name, value)
    assert rel_history.model_version() != before


def test_model_version_changes_with_the_llm_model(monkeypatch):
    before = rel_history.model_version()
    monkeypatch.setattr(deepseek, "MODEL", "deepseek-other")
    assert rel_history.model_version() != before


def test_every_run_keeps_its_rows(seeded):
    engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    engine.run(no_fetch=False, tickers=seeded, run_id="r2")
    assert len(_rows("r1")) == len(_rows("r2")) > 0 and len(_headers()) == 2


def test_a_refresh_never_records(seeded):
    engine.run(no_fetch=True, tickers=seeded)
    assert not rawstore.history_db_path().exists() or _headers() == []


def test_a_peak_stop_is_partial_and_keeps_its_rows(seeded, monkeypatch):
    def peak(tickers, **k):
        raise deepseek.PeakHoursError(datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(engine, "build_profiles", peak)
    engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "stopped at the DeepSeek peak boundary" in head["quality_note"]
    assert "2026-10-07 01:00" in head["quality_note"] and len(_rows("r1")) > 0


def test_a_run_that_scored_nothing_is_partial_and_the_latest_table_keeps_its_rows(seeded, monkeypatch):
    engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    kept = rawstore.count("relatedness_scores")
    monkeypatch.setattr(engine, "build_profiles", lambda tickers, **k: {})
    engine.run(no_fetch=False, tickers=seeded, run_id="r2")
    assert rawstore.count("relatedness_scores") == kept
    second = [h for h in _headers() if h["run_id"] == "r2"][0]
    assert second["quality"] == "partial" and "scored no pair" in second["quality_note"] and _rows("r2") == []


def test_the_history_is_recorded_after_the_scores_are_saved(seeded, monkeypatch):
    order = []
    real_save = store.save_scores
    monkeypatch.setattr(store, "save_scores", lambda peers: (order.append("save"), real_save(peers))[1])
    monkeypatch.setattr(rel_history, "record", lambda *a, **k: order.append("history"))
    engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    assert order == ["save", "history"]


def test_a_history_failure_is_a_job_note_and_the_run_still_returns(seeded, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(rel_history, "record", boom)
    payload = engine.run(no_fetch=False, tickers=seeded, run_id="r1")
    assert payload["n"] == len(seeded) and rawstore.count("relatedness_scores") > 0
    assert any("Relatedness result history was not recorded" in n and "history file locked" in n
               for n in ledger.take_notes())


def test_a_run_without_an_id_gets_a_fresh_one(seeded):
    engine.run(no_fetch=False, tickers=seeded)
    (head,) = _headers()
    assert head["run_id"]


def test_the_dashboard_fetch_records_under_the_job_id_and_its_refresh_does_not(seeded, monkeypatch):
    from RevenueChaser.app import runners
    monkeypatch.setattr(engine, "select_pilot", lambda limit=None: list(seeded))
    monkeypatch.setattr(runners, "run_id", lambda: "job-r")
    runners.run_relatedness(None, no_fetch=False)
    runners.run_relatedness(None, no_fetch=True)
    assert [h["run_id"] for h in _headers()] == ["job-r"]


def test_record_scores_rows_match_save_scores(seeded):
    peers = {"A": [{"peer": "B", "score": 0.5}], "B": [{"peer": "A", "score": 0.5}]}
    assert rel_history.record({"peers": peers}, requested=2, run_id="x", run_at=COMPLETE) == {TABLE: 2}
    assert store.score_rows(peers) == [{"ticker": "A", "peer": "B", "score": 0.5},
                                       {"ticker": "B", "peer": "A", "score": 0.5}]


def test_the_off_peak_cli_run_records_under_its_task_run_id(seeded, monkeypatch):
    """RC-Relatedness-OffPeak runs ``python -m RevenueChaser.module_relatedness --run``, which calls ``engine.run``
    with no id: the header takes the task's ``job_runs`` id (``RC_RUN_ID``, #129 A1) through ``ledger.new_run_id``."""
    monkeypatch.setenv(rawstore.RUN_ID_ENV, "task-7")
    engine.run(no_fetch=False, tickers=seeded)
    assert [h["run_id"] for h in _headers()] == ["task-7"]


def test_selection_version_is_pinned():
    """C-F2: the C12 percentile and C14 cap change what a run selects, so the version is 2."""
    assert rel_history.SELECTION_VERSION == "2"


def test_the_history_payload_leaves_the_theme_catalog_out(rawstore_tmp_db):
    """#383 C1b review (N6): the Theme facet's catalog is a store read at render time (the pinned theme run), not part
    of the Fetch's result: the history copy of the payload leaves it out and keeps the rest."""
    ledger.take_notes()
    payload = {"as_of": "2026-10-06T22:00:00+00:00", "n": 1, "peers": {},
               "companies": [{"ticker": "NVDA", "gics": ["Technology", "Semiconductors"], "theme_ids": ["r:c0"]}],
               "theme_catalog": {"r:c0": {"label": "Chips", "level": "coarse", "parent": ""}}}
    rel_history.record(payload, requested=1, run_id="r1", run_at=COMPLETE)
    stored = _unpack(_headers()[0]["payload"])
    assert "theme_catalog" not in stored
    assert stored["companies"] == payload["companies"]
    assert "theme_catalog" in payload                  # the caller's payload (the board) keeps it
