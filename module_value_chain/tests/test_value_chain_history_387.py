"""Value Chain board -> result history (#387, A3). Offline; the store is a temp DuckDB, no SEC request.

Every write of ``chain_mentions`` records a run through ``core/history.record_run``: a Value Chain Fetch (with the
board payload) and an EDGAR sync (the dashboard job and the scheduled ``RC-ValueChain-Sync``). The rows are the edges
``chain_mentions`` holds after the write, numbered by ``row_no``. Run board: every run keeps its rows; kept edges,
failed tickers or an SEC stop make the run partial.
"""
from __future__ import annotations

import json
import zlib

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_value_chain import config, engine, history as vc_history, precision, sync
from RevenueChaser.module_value_chain.tests.test_sync import _seed as _seed_sync, _subs
from RevenueChaser.module_value_chain.tests.test_value_chain import _TEN_K

BOARD, TABLE = "module_value_chain", "value_chain_edges_history"


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    return rawstore_tmp_db


def _seed_corpus(text=_TEN_K):
    rawstore.write_rows("chain_filings", [{
        "ticker": "NVDA", "cik": "1", "form": "10-K", "filed": "2026-02-01", "accession": "acc",
        "primary_doc": "http://x", "item_text": text, "quality": "ok"}])


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ? ORDER BY row_no", [run_id], db="history")


def _unpack(cell):
    if isinstance(cell, (bytes, bytearray)):
        return json.loads(zlib.decompress(cell))
    return cell if not isinstance(cell, str) else json.loads(cell)


def _strip(rows):
    return [{k: v for k, v in r.items() if k not in ("run_id", "row_no")} for r in rows]


# ── a Value Chain Fetch ──

def test_a_fetch_records_the_edges_chain_mentions_holds_and_the_payload(store):
    _seed_corpus()
    payload = engine.run(no_fetch=False, tickers=["NVDA"], run_id="r1")
    (head,) = _headers()
    assert head["run_id"] == "r1" and head["quality"] in ("complete", "intraday") and head["quality_note"] is None
    rows = _rows("r1")
    assert [r["row_no"] for r in rows] == list(range(len(rows))) and rows
    assert sorted(_strip(rows), key=str) == sorted(rawstore.get_all("chain_mentions"), key=str)
    stored = _unpack(head["payload"])
    assert stored["edges"] == json.loads(json.dumps(payload["edges"], default=str))
    assert json.loads(head["context"])["trigger"] == "fetch"


def test_the_header_carries_model_version_and_params(store):
    _seed_corpus()
    engine.run(no_fetch=False, tickers=["NVDA"], run_id="r1")
    (head,) = _headers()
    assert head["model_version"] == vc_history.model_version() and head["model_version"].startswith("2+")  # #387 D7
    assert json.loads(head["params"])["MENTION_WINDOW"] == config.MENTION_WINDOW


@pytest.mark.parametrize("module, name, value", [("config", "MENTION_WINDOW", 200), ("config", "MIN_ITEM_CHARS", 10),
                                                 ("precision", "MIN_PRECISION", 0.9),
                                                 ("precision", "MAX_EDGES_PER_TICKER", 10)])
def test_model_version_changes_with_a_setting(monkeypatch, module, name, value):
    before = vc_history.model_version()
    monkeypatch.setattr({"config": config, "precision": precision}[module], name, value)
    assert vc_history.model_version() != before


def test_every_fetch_keeps_its_rows(store):
    _seed_corpus()
    engine.run(no_fetch=False, tickers=["NVDA"], run_id="r1")
    engine.run(no_fetch=False, tickers=["NVDA"], run_id="r2")
    assert len(_rows("r1")) == len(_rows("r2")) > 0


def test_a_refresh_never_records(store):
    _seed_corpus()
    engine.run(no_fetch=True, tickers=["NVDA"])
    assert not rawstore.history_db_path().exists() or _headers() == []


def test_kept_edges_make_the_run_partial_and_are_the_rows(store):
    rawstore.replace_all("chain_mentions", [{"src": "NVDA", "dst_name": "TSMC", "dst_ticker": "TSM",
                                             "relation": "supplier", "snippet": "s", "filed": "2026-02-01"}])
    _seed_corpus(text="nothing here names a company")
    engine.run(no_fetch=False, tickers=["NVDA"], run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "no edge came out of 1 filings, so the 1 stored edges were kept" in \
        head["quality_note"]
    assert _strip(_rows("r1")) == rawstore.get_all("chain_mentions")


def test_a_fetch_with_failed_tickers_is_partial(store, monkeypatch):
    _seed_corpus()
    real = engine.fetch_corpus
    monkeypatch.setattr(engine, "fetch_corpus", lambda **k: {**real(**k), "failed": 2})
    engine.run(no_fetch=False, tickers=["NVDA"], run_id="r1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "2 tickers' filings could not be fetched or synced" in head["quality_note"]


def test_a_history_failure_is_a_job_note_and_the_fetch_still_returns(store, monkeypatch):
    _seed_corpus()

    def boom(*a, **k):
        raise RuntimeError("history file locked")

    monkeypatch.setattr(vc_history, "record", boom)
    payload = engine.run(no_fetch=False, tickers=["NVDA"], run_id="r1")
    assert payload["edges"] and rawstore.get_all("chain_mentions")
    assert any("Value Chain result history was not recorded" in n and "history file locked" in n
               for n in ledger.take_notes())


def test_the_dashboard_fetch_records_under_the_job_id(store, monkeypatch):
    from RevenueChaser.app import runners
    _seed_corpus()
    monkeypatch.setattr(engine, "fetch_corpus", lambda **k: {"fetched": 1, "failed": 0})
    monkeypatch.setattr(runners, "run_id", lambda: "job-v")
    runners.run_value_chain(None, no_fetch=False)
    runners.run_value_chain(None, no_fetch=True)
    assert [h["run_id"] for h in _headers()] == ["job-v"]


# ── an EDGAR sync ──

def test_a_sync_records_the_rebuilt_edges_without_a_payload(store, monkeypatch):
    _seed_sync(monkeypatch, _subs("acc-1"))
    out = sync.sync_filings(run_id="job-s")
    (head,) = _headers()
    assert head["run_id"] == out["run_id"] == "job-s" and head["payload"] is None
    ctx = json.loads(head["context"])
    assert ctx["trigger"] == "sync" and ctx["counts"]["updated"] == 1
    assert len(_rows("job-s")) == ctx["edges"] == out["edges"]


def test_a_sync_stopped_by_sec_is_partial(store, monkeypatch):
    from RevenueChaser.core.providers import sec_edgar
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1e9, "name": "AAA"}])
    rawstore.write_rows("sec_exchange_file", [{"ticker": "AAA", "cik": "1", "name": "AAA", "status": "present", "run_at": 1.0}])

    def _blocked(url, client=None):
        raise sec_edgar.SECUnavailable("SEC unavailable")

    monkeypatch.setattr(sync.sec_edgar, "get_json", _blocked)
    sync.sync_filings(workers=1, run_id="s1")
    (head,) = _headers()
    assert head["quality"] == "partial" and "SEC stopped answering; 1 tickers were left pending" in head["quality_note"]


def test_a_locked_sync_writes_no_edges_and_records_nothing(store, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def busy(name, wait=0):
        yield False

    _seed_sync(monkeypatch, _subs("acc-1"))
    monkeypatch.setattr(sync.job_lock, "acquire", busy)
    assert sync.sync_filings()["locked"] is True
    assert not rawstore.history_db_path().exists() or _headers() == []


def test_the_scheduled_sync_records_under_its_task_run_id(store, monkeypatch):
    """RC-ValueChain-Sync runs ``python -m RevenueChaser.module_value_chain --sync-filings``, which calls
    ``sync_filings`` with no id: the header takes the task's ``job_runs`` id (``RC_RUN_ID``, #129 A1)."""
    monkeypatch.setenv(rawstore.RUN_ID_ENV, "task-9")
    _seed_sync(monkeypatch, _subs("acc-1"))
    sync.sync_filings()
    assert [h["run_id"] for h in _headers()] == ["task-9"]


def test_the_history_payload_leaves_the_theme_catalog_out(store):
    """#383 C1b review (N6): the Theme facet's catalog is a store read at render time (the pinned theme run), not part
    of the Fetch's result: the history copy of the payload leaves it out and keeps the rest."""
    payload = {"as_of": "2026-10-06T22:00:00+00:00", "edges": [],
               "filings": [{"ticker": "NVDA", "gics": ["Technology", "Semiconductors"], "theme_ids": ["r:c0"]}],
               "theme_catalog": {"r:c0": {"label": "Chips", "level": "coarse", "parent": ""}}}
    vc_history.record([], run_id="r1", run_at=1_791_000_000.0, payload=payload)
    stored = _unpack(_headers()[0]["payload"])
    assert "theme_catalog" not in stored
    assert stored["filings"] == payload["filings"]
    assert "theme_catalog" in payload                  # the caller's payload (the board) keeps it
