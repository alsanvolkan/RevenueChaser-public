"""#379 D2 (avo's card "Kolon + header"): every Co-movement run row names the Fetch that made it, and a Fetch writes
one header-only ``result_runs`` row.

- ``comov_runs`` gains ``run_id``, ``session_date`` (rule B on the run's ``as_of``) and ``dropped_json`` (the
  short-history, no-price and flat-print lists a reload cannot rebuild). Additive ALTER, no schema bump: a store
  that predates them reads NULL.
- One header per Fetch, written by the Fetch runner after the precompute, listing every ``as_of`` the Fetch stored
  or served (its own window plus the precompute windows). ``precompute`` itself, the store-only recompute and a
  Refresh write none (06 rule 9; the Refresh paths are also in ``app/tests/test_refresh_never_records_383.py``).

Offline, temp store.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from RevenueChaser.core import calendar, history, rawstore
from RevenueChaser.module_comovement import config, engine
from RevenueChaser.module_comovement.tests import prices_bars
from RevenueChaser.module_comovement.tests.test_comovement import (
    _market_two_groups, _seed_ohlc, _seed_universe)

B = "module_comovement"


@pytest.fixture
def market(rawstore_tmp_db):
    """Two residual groups on a SPY factor, 400 sessions; tickers exclude SPY."""
    ret = _market_two_groups(n=400)
    tickers = [t for t in ret.columns if t != "SPY"]
    _seed_universe(tickers)
    _seed_ohlc(ret)
    return ret, tickers


@pytest.fixture
def runner(market, monkeypatch):
    from RevenueChaser.app import runners
    from RevenueChaser.module_comovement import ohlc

    monkeypatch.setattr(ohlc, "fetch_bulk", lambda tickers=None, no_fetch=False, **kw:
                        {"requested": 0, "fetched": 0, "no_fetch": no_fetch})
    return runners


@pytest.fixture
def recorded(monkeypatch):
    """Counts every ``record_run`` call (and lets it write)."""
    calls: list[dict] = []
    real = history.record_run

    def spy(board, **kw):
        calls.append({"board": board, **kw})
        return real(board, **kw)

    monkeypatch.setattr(history, "record_run", spy)
    return calls


def _runs() -> list[dict]:
    return rawstore.query("SELECT * FROM comov_runs ORDER BY as_of")


# ── the run columns ─────────────────────────────────────────────────────────
def test_a_stored_run_names_its_run_session_and_drop_lists(market):
    p = engine.run(no_fetch=True, persist=True, run_id="fetch-1")
    row = _runs()[0]
    assert row["run_id"] == "fetch-1"
    assert row["session_date"] == calendar.session_for(datetime.fromisoformat(p["as_of"])).session_date
    assert json.loads(row["dropped_json"]) == {
        "dropped": p["dropped"], "no_prices": p["no_prices"], "flat_prints": p["flat_prints"]}


def test_the_drop_lists_are_the_runs_own(market):
    """A name with too few bars is in the stored list, not an empty one."""
    ret, tickers = market
    _seed_ohlc(ret[["A1"]].tail(20).rename(columns={"A1": "SHORT"}))
    _seed_universe([*tickers, "SHORT"])
    p = engine.run(no_fetch=True, persist=True, run_id="fetch-1")
    assert "SHORT" in p["dropped"]
    assert json.loads(_runs()[0]["dropped_json"])["dropped"] == p["dropped"]


def test_a_persisting_run_with_no_id_gets_a_fresh_one(market):
    engine.run(no_fetch=True, persist=True)
    assert _runs()[0]["run_id"]


def test_one_precompute_shares_one_id_across_its_windows(market):
    engine.precompute()
    ids = {r["run_id"] for r in _runs()}
    assert len(_runs()) == 2 and len(ids) == 1 and None not in ids


def test_precompute_reports_each_windows_as_of(market):
    fetch = engine.run(no_fetch=True, persist=True, run_id="fetch-1")         # 252
    served: dict[int, str] = {}
    assert engine.precompute(run_id="fetch-1", as_ofs=served) == {126: "computed", 252: "reused"}
    assert served[252] == fetch["as_of"]
    assert served[126] == next(r["as_of"] for r in _runs() if r["window_days"] == 126)


def test_a_store_that_predates_the_columns_reads_null(isolate_rawstore):
    """The additive ALTER (``_reconcile_table``): an old row keeps its values and reads NULL in the new columns."""
    new = ("run_id", "session_date", "dropped_json")
    old = [c for c in rawstore.MANIFEST["comov_runs"]["columns"] if c.split()[0] not in new]
    with rawstore.connect() as conn:
        conn.execute(f"CREATE TABLE comov_runs ({', '.join(old)})")
        conn.execute("INSERT INTO comov_runs (as_of, window_days, config_hash) VALUES "
                     "('2026-10-01T20:00:00.000000+00:00', 252, 'abc')")
    rawstore.ensure_table("comov_runs")
    row = rawstore.query("SELECT * FROM comov_runs")[0]
    assert row["window_days"] == 252 and row["config_hash"] == "abc"
    assert row["run_id"] is None and row["session_date"] is None and row["dropped_json"] is None


# ── the header: Fetch only, once, after the precompute ──────────────────────
def test_a_fetch_writes_one_header_after_the_precompute_listing_every_as_of(runner, recorded, monkeypatch):
    order: list[str] = []
    real_pre = engine.precompute
    monkeypatch.setattr(engine, "precompute", lambda **kw: order.append("precompute") or real_pre(**kw))
    real_rec = history.record_run
    monkeypatch.setattr(history, "record_run", lambda board, **kw: order.append("header") or real_rec(board, **kw))

    runner.run_comovement(None)

    assert order == ["precompute", "header"]
    assert [c["board"] for c in recorded] == [B]
    headers = history.runs(B)
    assert len(headers) == 1
    h = headers[0]
    runs = _runs()
    assert sorted(h["params"]["as_of"]) == sorted(r["as_of"] for r in runs)
    assert sorted(h["params"]["windows"]) == sorted(config.PRECOMPUTE_WINDOWS)
    assert {r["run_id"] for r in runs} == {h["run_id"]}
    assert h["row_counts"] == {} and h["quality"] in ("complete", "intraday")
    board = json.loads(runner.COMOVEMENT_JSON.read_text(encoding="utf-8"))
    assert h["params"]["config_hash"] == board["config_hash"]
    assert h["funnel"] == board["funnel"]
    assert h["session_date"] == runs[0]["session_date"]


def test_the_header_lists_a_reused_window_by_its_stored_as_of(runner):
    old = engine.run(no_fetch=True, persist=True, window=126, run_id="older")
    runner.run_comovement(None)
    h = history.last_run(B)
    assert old["as_of"] in h["params"]["as_of"]
    assert len(h["params"]["as_of"]) == 2


def test_a_precompute_failure_is_a_header_warning(runner, monkeypatch):
    def boom(**kw):
        raise RuntimeError("precompute exploded")

    monkeypatch.setattr(engine, "precompute", boom)
    runner.run_comovement(None)
    h = history.last_run(B)
    assert len(h["params"]["as_of"]) == 1
    assert any("precompute exploded" in w for w in h["warnings"])


def test_a_fetch_that_stores_no_run_is_a_partial_header(rawstore_tmp_db, runner):
    prices_bars.clear()
    runner.run_comovement(None)
    h = history.last_run(B)
    assert h["quality"] == "partial" and "no run was stored" in h["quality_note"]
    assert h["params"]["as_of"] == []
    assert rawstore.count("comov_runs") == 0


def test_a_history_failure_is_a_job_note_not_a_failed_fetch(runner, monkeypatch):
    from RevenueChaser.core import ledger

    notes: list[str] = []
    monkeypatch.setattr(ledger, "add_note", notes.append)
    monkeypatch.setattr(history, "record_run", lambda board, **kw: (_ for _ in ()).throw(RuntimeError("locked")))
    path = runner.run_comovement(None)
    assert path.exists() and rawstore.count("comov_runs") == 2
    assert any("Co-movement result history" in n and "locked" in n for n in notes)


def test_precompute_writes_no_header(market, recorded):
    engine.precompute()
    assert recorded == []
    assert not rawstore.history_db_path().exists()


@pytest.mark.parametrize("path", ["refresh", "refresh_window", "recompute"])
def test_a_store_only_path_writes_no_header(runner, recorded, path):
    from RevenueChaser.app.jobs import JobMessage

    engine.run(no_fetch=True, persist=True, run_id="seed")                    # something to serve
    if path == "refresh":
        runner.run_comovement(None, no_fetch=True)
    elif path == "refresh_window":
        runner.run_comovement(None, no_fetch=True, window=126)
    else:
        with pytest.raises(JobMessage):
            runner.run_comovement_recompute(None)
    assert recorded == []
    assert not rawstore.history_db_path().exists()


def test_the_model_version_moves_with_the_linkage(monkeypatch):
    from RevenueChaser.module_comovement import history as comov_history

    before = comov_history.model_version()
    monkeypatch.setattr(config, "LINKAGE", "average")
    assert comov_history.model_version() != before
