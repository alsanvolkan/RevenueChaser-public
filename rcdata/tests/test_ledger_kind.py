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


# ── the derived tables: ledger/outcomes and ledger/baselines (step 5) ─────────────────────────────────────────────

def _bars_through(day, tickers=("AAA", "BBB", "SPY")):
    """Flat bars from 2026-08-03 to ``day`` (open 10, high 11, low 9, close 10) for ``tickers``."""
    from datetime import timedelta

    from RevenueChaser.core import calendar as cal
    from RevenueChaser.rcdata.tests import prices_seed
    days, d = [], date(2026, 8, 3)
    while d <= day:
        if cal.is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
    for t in tickers:
        prices_seed.seed_bars(t, days, fetched_at=0.0, close=10.0, ohlc={x: (10.0, 11.0, 9.0, 10.0) for x in days})


def _derive(name, params=None):
    ops.enqueue(name, params, by="test")
    return drain(name)


@pytest.mark.parametrize("name", ["ledger/outcomes", "ledger/baselines"])
def test_the_derived_tables_are_datasets_of_the_ledger_kind_due_daily(name):
    ds = DATASETS[name]
    assert ds.kind == "ledger" and ds.needs_parquet is False and not getattr(ds, "by_request", False)
    assert due.is_due(ds) == (True, "never built")


def test_the_derived_datasets_run_once_per_new_day_of_bars(rawstore_tmp_db):
    """The as-of is the prices store's newest session: a run for it is not due again until a newer bar lands."""
    _bars_through(date(2026, 10, 9))
    ds = DATASETS["ledger/outcomes"]
    assert ds.default_as_of() == date(2026, 10, 9)
    _derive("ledger/outcomes")
    assert due.is_due(ds)[0] is False
    _bars_through(date(2026, 10, 12))
    assert due.is_due(ds)[0] is True


def test_the_outcomes_run_writes_its_rows_into_the_ledger_file_and_leaves_the_picks(rawstore_tmp_db):
    _bars_through(date(2026, 10, 9))
    _queue(day=date(2026, 9, 1))
    _drain()
    picks = read.forecast_ledger()
    [check] = _derive("ledger/outcomes")
    assert check["result"] == "changed" and check["added"] == 4 and "4 row(s) computed" in check["note"]
    rows = read.forecast_outcomes(source="swing")
    assert [(r["subject"], r["horizon_days"], r["status"]) for r in rows] == [
        ("AAA", 30, "closed"), ("AAA", 90, "open"), ("BBB", 30, "closed"), ("BBB", 90, "open")]
    assert rows[0]["ref_price"] == 10.0 and rows[0]["mfe"] == pytest.approx(0.1)
    assert read.forecast_ledger() == picks


def test_a_later_freeze_keeps_the_derived_rows(rawstore_tmp_db):
    _bars_through(date(2026, 10, 9))
    _queue(day=date(2026, 9, 1))
    _drain()
    _derive("ledger/outcomes")
    _derive("ledger/baselines")
    before = (read.forecast_outcomes(), read.forecast_baselines())
    _queue(day=date(2026, 10, 9), tickers=("CCC",))
    _drain()
    assert (read.forecast_outcomes(), read.forecast_baselines()) == before and before[0] and before[1]


def test_the_baselines_run_writes_eleven_rows_per_horizon(rawstore_tmp_db):
    _bars_through(date(2026, 10, 9))
    _queue(day=date(2026, 9, 1))
    _drain()
    [check] = _derive("ledger/baselines")
    assert check["result"] == "changed"
    rows = read.forecast_baselines()
    assert len(rows) == 22 and {r["forecast_date"] for r in rows} == {date(2026, 9, 1)}
    universe = [r for r in rows if r["basis"] == "universe" and r["horizon_days"] == 30]
    assert universe[0]["n"] == 3                                  # AAA, BBB and SPY traded that day


def test_a_full_request_recomputes_every_row(rawstore_tmp_db):
    _bars_through(date(2026, 10, 9))
    _queue(day=date(2026, 9, 1))
    _drain()
    _derive("ledger/outcomes")
    first = {(r["subject"], r["horizon_days"]): r["run_id"] for r in read.forecast_outcomes()}
    _derive("ledger/outcomes")                                    # the daily run: the closed 30-day rows stay
    second = {(r["subject"], r["horizon_days"]): r["run_id"] for r in read.forecast_outcomes()}
    assert second[("AAA", 30)] == first[("AAA", 30)] and second[("AAA", 90)] != first[("AAA", 90)]
    _derive("ledger/outcomes", {"full": True})
    third = {(r["subject"], r["horizon_days"]): r["run_id"] for r in read.forecast_outcomes()}
    assert third[("AAA", 30)] != first[("AAA", 30)]


def test_a_derived_build_that_changes_a_pick_is_refused(rawstore_tmp_db, monkeypatch):
    """The ledger's append-only guard runs on every build of the kind, whichever dataset asked for it."""
    _bars_through(date(2026, 10, 9))
    _queue(day=date(2026, 9, 1))
    _drain()
    from RevenueChaser.core import outcomes
    real = outcomes.run

    def tampering(con, **kw):
        con.execute("DELETE FROM forecast_ledger WHERE subject = 'BBB'")
        return real(con, **kw)

    monkeypatch.setattr(outcomes, "run", tampering)
    [check] = _derive("ledger/outcomes")
    assert check["result"] == "failed" and "append-only" in check["note"]
    assert [r["subject"] for r in read.forecast_ledger()] == ["AAA", "BBB"] and read.forecast_outcomes() == []


def test_no_bars_at_all_fails_the_run_and_keeps_the_file(rawstore_tmp_db):
    _queue(day=date(2026, 9, 1))
    _drain()
    [check] = _derive("ledger/outcomes")
    assert check["result"] == "failed" and "no daily bars" in check["note"]
    assert read.forecast_outcomes() == [] and len(read.forecast_ledger()) == 2
