"""PR 10 (G4): the forecast ledger is a serving kind, ``ledger.duckdb``, written only by the builder.

A board's freeze (``core.ledger.write_picks``) queues one ``ledger/freeze`` request carrying its picks; the builder's
build copies the live tables, appends the picks unless the (source, day) is already frozen (first write wins) and
refuses any build that removes or changes a live row. The file is not rebuildable from raw files, so it is in the
nightly ``rcdata backup`` like ``ops.sqlite``.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.rcdata import backup, ops, read, serving
from RevenueChaser.rcdata.build import DATASETS, base, due
from RevenueChaser.rcdata.tests.ledger_queue import drain

D = date(2026, 10, 14)


def _picks(*tickers, price_session=None):
    return [{"subject": t, "rank": i + 1, "score": 90.0 - i, "bucket": "A", "market_cap": 1e9, "sector": "IT",
             "shown_price": 10.0 + i, "stop": 9.0, "target": 12.0, "price_session": price_session}
            for i, t in enumerate(tickers)]


def _queue(source="swing", day=D, tickers=("AAA", "BBB"), **kw):
    return ledger.write_picks(source, "Swing", _picks(*tickers, **kw), model_version="v1+abc",
                              forecast_date=day, run_id="run1", frozen_at=1000.0)


def _drain():
    return drain()


def test_the_ledger_is_a_serving_kind_at_version_1_and_backed_up_nightly():
    assert serving.SERVING_SCHEMA_VERSION["ledger"] == 1 and "ledger" in serving.KINDS
    assert DATASETS["ledger/freeze"].kind == "ledger"


def test_a_freeze_queues_its_picks_and_writes_nothing_itself(rawstore_tmp_db):
    out = _queue()
    assert out.queued == 2 and out.request_id is not None
    assert out.message == f"Queued 2 Swing picks for {D} (request {out.request_id}); the builder writes them."
    req = ops.queue_view()["active"][0]
    assert req["dataset"] == "ledger/freeze" and req["requested_by"] == "freeze"
    assert [r["subject"] for r in req["params"]["rows"]] == ["AAA", "BBB"]
    assert rawstore.count("forecast_ledger") == 0 and read.forecast_ledger() == []


def test_the_builder_writes_the_queued_picks_without_a_parquet_tree(rawstore_tmp_db):
    _queue(price_session=D)
    [check] = _drain()
    assert check["result"] == "changed" and check["added"] == 2 and check["removed"] == 0
    rows = read.forecast_ledger(source="swing")
    assert [(r["subject"], r["rank"], r["forecast_date"], r["price_session"]) for r in rows] == [
        ("AAA", 1, D, D), ("BBB", 2, D, D)]
    assert rows[0]["model_version"] == "v1+abc" and rows[0]["frozen_at"] == 1000.0
    assert rows[0]["status"] == "changed"
    assert not base.parquet_root().exists()


def test_the_first_freeze_of_a_day_wins_in_the_builder_too(rawstore_tmp_db):
    _queue(tickers=("AAA",))
    _drain()
    rid = ops.enqueue("ledger/freeze", {"rows": [_queue_rows("ZZZ")]}, by="freeze")
    [check] = _drain()
    assert check["result"] == "unchanged" and "already frozen" in check["note"]
    assert [r["subject"] for r in read.forecast_ledger()] == ["AAA"]
    assert rid


def _queue_rows(ticker):
    return {"source": "swing", "forecast_date": D.isoformat(), "subject": ticker, "target_type": "pick",
            "target_date": D.isoformat(), "frozen_at": 1.0, "run_id": "r", "model_version": "v", "rank": 1}


def test_a_day_already_frozen_or_already_queued_is_not_queued_again(rawstore_tmp_db):
    first = _queue()
    again = _queue(tickers=("CCC",))
    assert again.queued == 0 and again.reason == f"already queued for {D} (request {first.request_id})"
    _drain()
    third = _queue(tickers=("DDD",))
    assert third.queued == 0 and third.reason == f"already frozen for {D}"
    assert _queue(source="fundamentals").queued == 2                 # another board's day is its own


def test_a_build_that_drops_or_changes_a_live_row_is_refused(rawstore_tmp_db, monkeypatch):
    _queue()
    _drain()
    from RevenueChaser.rcdata.build.ledger import freeze
    real = freeze._copy_live

    def tampered(con, table, schema):
        real(con, table, schema)
        con.execute("UPDATE forecast_ledger SET score = -1 WHERE subject = 'AAA'")

    monkeypatch.setattr(freeze, "_copy_live", tampered)
    ops.enqueue("ledger/freeze", {"rows": [_queue_rows("NEW") | {"forecast_date": "2026-10-15",
                                                                   "target_date": "2026-10-15"}]}, by="freeze")
    [check] = _drain()
    assert check["result"] == "failed" and "append-only" in check["note"]
    assert {r["score"] for r in read.forecast_ledger()} == {90.0, 89.0}       # the live file as it was


def test_the_freeze_dataset_is_never_due_it_runs_on_request():
    due_now, why = due.is_due(DATASETS["ledger/freeze"])
    assert due_now is False and "request" in why


def test_the_nightly_backup_copies_the_ledger(rawstore_tmp_db, tmp_path, monkeypatch):
    _queue()
    _drain()
    from RevenueChaser.core import config
    monkeypatch.setattr(config, "RAW_STORE_BACKUP_DIR", str(tmp_path / "bk"))
    backup.run_backup()
    copies = sorted((tmp_path / "bk" / backup.SUBDIR).glob("ledger_*.duckdb"))
    assert len(copies) == 1
    con = duckdb.connect(str(copies[0]), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM forecast_ledger").fetchone()[0] == 2
    finally:
        con.close()


@pytest.mark.parametrize("bad", [{"rank": 0}, {"subject": ""}])
def test_a_malformed_request_row_fails_the_build_and_leaves_the_file(rawstore_tmp_db, bad):
    ops.enqueue("ledger/freeze", {"rows": [_queue_rows("AAA") | bad]}, by="freeze")
    [check] = _drain()
    assert check["result"] == "failed"
    assert not serving.live_path("ledger").exists()
