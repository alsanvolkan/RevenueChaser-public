"""#446: ``ownership`` builds on a copy of the live file and reloads only the partitions that changed.

The main proof is metamorphic, as for ``fundamentals`` and ``prices``: whatever changed in Parquet or in the spine,
the build on the live copy serves exactly what a whole build of the same tree serves (``_same_as_whole``). Each
source table is keyed by its partition (``date=``, ``half=``, ``day=``); the tickers a source's rows are named by
come from the live spine, so a spine change restates them on rows no partition change touches.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import duckdb
import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base, dirty
from RevenueChaser.rcdata.build.ownership import KIND, common, finra, form4, ftd, ibkr, regsho, sched13dg, sec13f, serve

from . import ownership_fixture as ox



def _part(path: Path, schema: list[tuple[str, str]], rows: list[dict]) -> None:
    con = duckdb.connect()
    try:
        base.rows_table(con, "rows", schema, rows)
        base.write_parquet(con, "SELECT * FROM rows", path)
    finally:
        con.close()


def _finra(day: str, symbols: dict[str, int]) -> None:
    rows = [{"settlement_date": date.fromisoformat(day), "symbol": s, "source_symbol": s, "current_short": n,
             "days_to_cover": 1.5, "fetched_at": 1.0, "run_id": "t"} for s, n in symbols.items()]
    _part(finra.parquet_dir() / f"date={day}" / "part-0.parquet", finra.SCHEMA, rows)


def _regsho(day: str, symbols: list[str]) -> None:
    rows = [{"list_date": date.fromisoformat(day), "market": "nasdaq", "symbol": s, "source_symbol": s,
             "fetched_at": 1.0, "run_id": "t"} for s in symbols]
    _part(regsho.parquet_dir() / f"date={day}" / "part-0.parquet", regsho.SCHEMA, rows)


def _ibkr(day: str, symbols: dict[str, float]) -> None:
    d = date.fromisoformat(day)
    rows = [{"snapshot_date": d, "session_date": d, "symbol": s, "source_symbol": s, "fee_rate": fee,
             "available": 1000, "fetched_at": 1.0, "run_id": "t"} for s, fee in symbols.items()]
    _part(ibkr.parquet_dir() / f"date={day}" / "part-0.parquet", ibkr.SCHEMA, rows)


def _ftd(half: str, day: str, symbols: dict[str, int]) -> None:
    rows = [{"settlement_date": date.fromisoformat(day), "cusip": ox.SPINE_CUSIPS.get(s, "000000000"),
             "symbol": s, "source_symbol": s, "quantity": n, "half": half, "fetched_at": 1.0, "run_id": "t"}
            for s, n in symbols.items()]
    _part(ftd.parquet_dir() / f"half={half}" / "part-0.parquet", ftd.SCHEMA, rows)


def _form4(day: str, trades: list[tuple[str, str | None, str, float]]) -> None:
    """``trades``: (accession, issuer CIK, symbol, shares)."""
    d = date.fromisoformat(day)
    rows = [{"accession": a, "form": "4", "filed": d, "index_day": d, "issuer_cik": cik, "symbol": s,
             "issuer_symbol": s, "tx_table": "non_derivative", "line": 1, "shares": n, "status": "ok",
             "fetched_at": 1.0, "run_id": "t"} for a, cik, s, n in trades]
    _part(form4.DATASET.parquet_dir() / "rows" / f"day={day}" / "part-0.parquet", form4.ROWS, rows)


def _13dg(day: str, stakes: list[tuple[str, str, str, float]]) -> None:
    """``stakes``: (accession, issuer CIK, CUSIP, percent)."""
    d = date.fromisoformat(day)
    rows = [{"accession": a, "form": "SC 13G", "filed": d, "index_day": d, "issuer_cik": cik, "cusip": cusip,
             "person_line": 1, "percent": pct, "status": "ok", "fetched_at": 1.0, "run_id": "t"}
            for a, cik, cusip, pct in stakes]
    _part(sched13dg.DATASET.parquet_dir() / "rows" / f"day={day}" / "part-0.parquet", sched13dg.ROWS, rows)


def _13f(day: str, filings: list[tuple[str, int, list[tuple[str, float, float]]]]) -> None:
    """``filings``: (accession, filer CIK, [(CUSIP, value, shares)]) for the September quarter."""
    d = date.fromisoformat(day)
    fs = [{"accession": a, "cik": str(cik), "filer_name": f"MANAGER {cik}", "form": "13F-HR",
           "period": date(2026, 9, 30), "filed": d, "index_day": d, "is_amendment": False, "holdings": len(h),
           "value_scale": 1, "confidential_omitted": False, "status": "ok", "origin": "edgar", "fetched_at": 1.0,
           "run_id": "t"} for a, cik, h in filings]
    hs = [{"accession": a, "cik": str(cik), "period": date(2026, 9, 30), "filed": d, "line": i, "cusip": c,
           "value": v, "value_usd": v, "shares": n, "sh_type": "SH"}
          for a, cik, h in filings for i, (c, v, n) in enumerate(h)]
    folder = sec13f.DATASET.parquet_dir()
    _part(folder / "filings" / f"day={day}" / "part-0.parquet", sec13f.FILINGS, fs)
    _part(folder / "holdings" / f"day={day}" / "part-0.parquet", sec13f.HOLDINGS, hs)
    _part(folder / "managers" / f"day={day}" / "part-0.parquet", sec13f.MANAGERS, [])


MSFT, AAPL, GME = ox.SPINE_CUSIPS["MSFT"], ox.SPINE_CUSIPS["AAPL"], ox.SPINE_CUSIPS["GME"]


def _seed() -> None:
    ox.make_spine()
    base.parquet_root().mkdir(parents=True, exist_ok=True)
    _finra("2026-09-30", {"MSFT": 100, "BFA": 50, "GME": 900})
    _finra("2026-10-15", {"MSFT": 110, "BFA": 55, "GME": 950, "ZZZZ": 1})
    _regsho("2026-10-26", ["GME", "ZZZZ"])
    _regsho("2026-10-27", ["GME"])
    _ibkr("2026-10-26", {"MSFT": 0.25, "GME": 5.0})
    _ibkr("2026-10-27", {"MSFT": 0.26, "GME": 6.0})
    _ftd("2026-09b", "2026-09-20", {"GME": 1000, "AAPL": 20})
    _ftd("2026-10a", "2026-10-05", {"GME": 1200})
    _form4("2026-10-26", [("0000000001-26-000001", "789019", "MSFT", 10.0),
                          ("0000000001-26-000002", "1326380", "GME", 5.0)])
    _form4("2026-10-27", [("0000000001-26-000003", "320193", "AAPL", 7.0)])
    _13dg("2026-10-26", [("0000000002-26-000001", "1326380", GME, 9.9)])
    _13dg("2026-10-27", [("0000000002-26-000002", "320193", AAPL, 5.1)])
    _13f("2026-10-26", [("0001000001-26-000001", 1000001, [(MSFT, 400_000, 1000), (GME, 20_000, 1000)]),
                        ("0001000002-26-000001", 1000002, [(MSFT, 100_000, 250)])])
    _13f("2026-10-27", [("0001000003-26-000001", 1000003, [(AAPL, 300_000, 1500)])])


def _serve(**params) -> dict:
    return base.run_request({"dataset": serve.NAME, "requested_by": "cli",
                             "params": {"as_of": "2026-10-28", **params}})


def _note(row: dict) -> str:
    return row.get("note") or ""


def _snapshot() -> dict[str, list]:
    with serving.open_read(KIND) as con:
        names = [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() "
            "UNION ALL SELECT view_name FROM duckdb_views() WHERE NOT internal "
            "AND database_name = current_database()").fetchall()]
        return {n: sorted(con.execute(f'SELECT * FROM "{n}"').fetchall(), key=repr)
                for n in sorted(names) if n not in serving.META_TABLES
                and n not in (dirty.MANIFEST_TABLE, dirty.RULES_TABLE, serving.VERSION_TABLE)}


def _same_as_whole(monkeypatch, row: dict, **params) -> dict[str, list]:
    assert row["result"] != "failed" and "built on the live file" in _note(row), _note(row)
    got = _snapshot()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    whole = _serve(**params)
    monkeypatch.delenv(dirty.WHOLE_ENV)
    assert whole["result"] != "failed" and "built whole" in _note(whole), _note(whole)
    want = _snapshot()
    assert set(got) == set(want)
    for name in want:
        if got[name] != want[name]:
            print("DIFF", name, "\n inc-only", sorted(set(got[name]) - set(want[name]), key=repr)[:10],
                  "\n whole-only", sorted(set(want[name]) - set(got[name]), key=repr)[:10])
        assert got[name] == want[name], name
    return want


@pytest.fixture
def seeded():
    _seed()
    first = _serve()
    assert first.get("result") in ("changed", "unchanged"), first
    return first


def _rows(sql: str) -> list[tuple]:
    with serving.open_read(KIND) as con:
        return con.execute(sql).fetchall()


def test_every_ownership_dataset_declares_its_dirty_set():
    assert dirty.incremental(base.kind_datasets(KIND))


def test_nothing_changed_builds_on_the_live_file(seeded, monkeypatch):
    row = _serve()
    assert "built on the live file: 0 Parquet file(s) changed" in _note(row), _note(row)
    _same_as_whole(monkeypatch, row)


def test_new_and_rewritten_partitions_reload_only_their_keys(seeded, monkeypatch):
    _finra("2026-10-31", {"MSFT": 120, "GME": 990})                    # a new settlement date
    _regsho("2026-10-27", ["GME", "BFA"])                             # a day typed again
    _ibkr("2026-10-28", {"MSFT": 0.3})
    _ftd("2026-10a", "2026-10-06", {"GME": 1300, "MSFT": 2})           # a half published again
    _form4("2026-10-28", [("0000000001-26-000009", "789019", "MSFT", 1.0)])
    _13dg("2026-10-27", [("0000000002-26-000002", "320193", AAPL, 6.0)])
    _13f("2026-10-28", [("0001000004-26-000001", 1000004, [(MSFT, 50_000, 125)])])
    row = _serve()
    assert "key(s) dirty" in _note(row) or "built on the live file" in _note(row), _note(row)
    want = _same_as_whole(monkeypatch, row)
    assert ("2026-10-31",) in {(str(r[0]),) for r in want["finra_short_interest"]}


def test_a_gone_partition_takes_its_rows_with_it(seeded, monkeypatch):
    (ftd.parquet_dir() / "half=2026-09b" / "part-0.parquet").unlink()
    (finra.parquet_dir() / "date=2026-09-30" / "part-0.parquet").unlink()
    monkeypatch.setattr(serving, "MAX_SHRINK", 1.0)                   # a source's own table: only its run relaxes
    row = _serve()
    want = _same_as_whole(monkeypatch, row)
    assert all(r[0] != date(2026, 9, 30) for r in want["finra_short_interest"])
    assert not _rows("SELECT * FROM sec_ftd WHERE half = '2026-09b'")


def test_a_spine_change_restates_the_names_of_rows_no_partition_touched(seeded, monkeypatch):
    before = _rows("SELECT ticker FROM finra_short_interest WHERE symbol = 'GME' AND settlement_date = '2026-09-30'")
    assert before == [("GME",)]
    serving.live_path("spine").unlink()
    ox.make_spine(tickers=("MSFT", "BF-A", "AAPL", "ZZZZ"))           # GME left the spine, ZZZZ joined
    row = _serve(allow_shrink=True)                                   # GME's 13F quarter goes with it
    _same_as_whole(monkeypatch, row, allow_shrink=True)
    assert _rows("SELECT ticker FROM finra_short_interest WHERE symbol = 'GME'") == [(None,), (None,)]
    assert _rows("SELECT ticker FROM regsho_threshold WHERE symbol = 'ZZZZ'") == [("ZZZZ",)]
    assert _rows("SELECT ticker FROM insider_transactions WHERE symbol = 'GME'") == [(None,)]


def test_a_source_run_carries_the_13f_tables_on_the_live_file(seeded, monkeypatch):
    _finra("2026-10-31", {"MSFT": 120})
    monkeypatch.setattr(finra.FinraShortInterest, "fetch", lambda self, ctx, as_of: [])
    row = base.run_request({"dataset": finra.NAME, "params": {"as_of": "2026-10-28"}})
    assert "built on the live file" in _note(row), _note(row)
    assert _rows("SELECT count(*) FROM sec13f_ticker_period")[0][0] > 0
    assert _rows("SELECT count(*) FROM finra_short_interest WHERE settlement_date = '2026-10-31'") == [(1,)]
    _same_as_whole(monkeypatch, row)


def test_a_live_file_built_before_the_manifest_still_drops_a_gone_partition(seeded, monkeypatch):
    """The first build after this landed reads the live file's ``built_at`` only, which cannot show a gone file:
    a key the file serves whose partition is gone is dirty all the same."""
    with duckdb.connect(str(serving.live_path(KIND))) as con:
        con.execute(f"DROP TABLE {dirty.MANIFEST_TABLE}")
    old = dirty.live_built_at(KIND) - 60
    for p in base.parquet_root().rglob("*.parquet"):
        os.utime(p, (old, old))
    (ftd.parquet_dir() / "half=2026-09b" / "part-0.parquet").unlink()
    monkeypatch.setattr(serving, "MAX_SHRINK", 1.0)
    row = _serve()
    _same_as_whole(monkeypatch, row)
    assert not _rows("SELECT * FROM sec_ftd WHERE half = '2026-09b'")


def test_a_partition_whose_rows_carry_another_key_is_refused_on_the_live_copy(seeded):
    """A row's key column is what the build on the live copy deletes by: a partition holding another key's rows
    would leave them twice, so the build stops and names the lever."""
    rows = [{"settlement_date": date(2026, 10, 1), "symbol": "MSFT", "current_short": 1, "fetched_at": 1.0,
             "run_id": "t"}]
    _part(finra.parquet_dir() / "date=2026-10-31" / "part-0.parquet", finra.SCHEMA, rows)
    row = _serve()
    assert row["result"] == "failed" and dirty.WHOLE_ENV in _note(row), _note(row)


def test_restate_rewrites_a_row_whose_ticker_source_alone_moved():
    ox.make_spine()
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE t (symbol TEXT, ticker TEXT, ticker_source TEXT)")
        con.execute("INSERT INTO t VALUES ('BF-A', 'BF-A', 'spine:squash'), ('BFA', 'BF-A', 'spine:squash'), "
                    "(NULL, NULL, NULL)")
        notes: list[str] = []
        assert common.restate(con, "t", common.BY_SYMBOL, notes, "x") == 1
        assert sorted(con.execute("SELECT * FROM t").fetchall(), key=repr) == sorted(
            [("BF-A", "BF-A", common.EXACT), ("BFA", "BF-A", common.SQUASHED), (None, None, None)], key=repr)
    finally:
        con.close()
