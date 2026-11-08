"""#446 step 7: ``fundamentals`` builds on a copy of the live file and reloads only the dirty companies.

The main proof is a metamorphic one: whatever changed in Parquet, the build on the live copy must serve exactly
what a whole build of the same tree serves (``_same_as_whole``). The rest pins when a whole build is still forced.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import date

import duckdb
import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base, dirty
from RevenueChaser.rcdata.build.fundamentals import common, companyfacts, earnings, facts, filings, tables, wrds_fund

from . import fund_fixture as fx

AS_OF = date(2026, 10, 28)
MSFT, ORCL, HEI, TM = "789019", "1341439", "46619", "1094517"


def _ctx() -> base.RunContext:
    return base.RunContext(run_id="t-parquet", dataset="test")


def _write_facts(cik: str, body: dict, as_of: date = AS_OF) -> None:
    raw = companyfacts.cik_dir(cik) / f"{as_of.isoformat()}.json.gz"
    base.write_raw_json(common.meta_path(raw), {"cik": cik, "as_of": as_of.isoformat(), "fetched_at": 1.0,
                                                "run_id": f"r-{cik}", "accessions": []})
    base.write_raw_json(raw, body)
    companyfacts.DATASET.to_parquet_all(_ctx(), [raw])


def _write_filings(cik: str, accessions: list[tuple[str, str, str | None]]) -> None:
    """``accessions``: (accession, form, 8-K item); one document per 10-K."""
    folder = common.parquet_dir(filings.NAME) / f"cik={cik}"
    stamp = {"source_as_of": AS_OF, "fetched_at": 2.0, "run_id": f"f-{cik}"}
    rows = [{"cik": cik, "accession": a, "form": form, "filing_date": date(2026, 10, 1), "report_date": None,
             "primary_document": f"{a}.htm", "item": item, **stamp} for a, form, item in accessions]
    docs = [{"cik": cik, "accession": a, "form": form, "filing_date": date(2026, 10, 1), "report_date": None,
             "primary_document": f"{a}.htm", "item": "business", "text": "text " * 20, "chars": 100,
             "quality": "ok", **stamp} for a, form, _ in accessions if form == "10-K"]
    segs = [{"cik": cik, "accession": a, "form": form, "filing_date": date(2026, 10, 1), "axis": "business",
             "member": "CloudMember", "segment": "Cloud", "concept": "Revenues", "value": 10.0, "unit": "USD",
             "period_start": date(2025, 1, 1), "period_end": date(2025, 12, 31), **stamp}
            for a, form, _ in accessions if form == "10-K"]
    con = duckdb.connect()
    try:
        for name, schema, data in (("filings", filings.FILING, rows), ("documents", filings.DOCUMENT, docs),
                                   ("segments", filings.SEGMENT, segs), ("customers", filings.CUSTOMER, [])):
            base.rows_table(con, name, schema, data)
            base.write_parquet(con, f"SELECT * FROM {name}", folder / f"{name}.parquet")
    finally:
        con.close()


def _write_calendar(symbols: list[str], day: date = AS_OF) -> None:
    rows = [{"symbol": s, "name": s, "report_date": date(2026, 11, 20), "fiscal_date_ending": date(2026, 9, 30),
             "estimate": 1.5, "currency": "USD", "time_of_day": "post-market", "source_as_of": day,
             "fetched_at": 3.0, "run_id": "av"} for s in symbols]
    con = duckdb.connect()
    try:
        base.rows_table(con, "rows", earnings.SCHEMA, rows)
        base.write_parquet(con, "SELECT * FROM rows",
                           common.parquet_dir(earnings.NAME) / f"date={day.isoformat()}" / "part-0.parquet")
    finally:
        con.close()


def _seed(spine_rows: list[dict] | None = None) -> None:
    fx.spine(spine_rows)
    fx.parquet_root()
    _write_facts(MSFT, fx.msft_facts())
    _write_facts(HEI, fx.hei_facts())
    _write_facts(TM, fx.tm_facts())
    _write_filings(MSFT, [("0000950170-25-100001", "10-K", None), ("0000950170-26-000002", "8-K", "2.02")])
    _write_filings(HEI, [("0000046619-26-000001", "10-K", None), ("0000046619-26-000002", "8-K", "5.02")])
    _write_calendar(["MSFT", "HEI", "ORCL"])


def _build(**params) -> dict:
    return fx.run(tables.NAME, AS_OF.isoformat(), **params)


def _note(check: dict) -> str:
    return check.get("note") or ""


def _snapshot() -> dict[str, list]:
    """Every served table and view, its rows sorted, without the build's own ``run_id``."""
    out = {}
    with serving.open_read("fundamentals") as con:
        for name in (*tables.TABLES, *tables.VIEWS):
            cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall() if r[0] != "run_id"]
            rows = con.execute(f"SELECT {', '.join(chr(34) + c + chr(34) for c in cols)} FROM {name}").fetchall()
            out[name] = sorted((tuple(_plain(v) for v in r) for r in rows), key=repr)
    return out


def _plain(value):
    """A JSON text with its object keys sorted: ``json_group_object`` keeps the rows' order, which a build on the
    live copy does not promise (no reader depends on the order of an object's keys)."""
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.dumps(json.loads(value), sort_keys=True)
        except ValueError:
            return value
    return value


def _same_as_whole(monkeypatch) -> dict[str, list]:
    """Assert the live file (just built on the live copy) serves what a whole build of the same tree serves."""
    incremental = _snapshot()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    check = _build(allow_shrink=True)
    assert "built whole" in _note(check) and check["result"] != "failed", _note(check)
    monkeypatch.delenv(dirty.WHOLE_ENV)
    whole = _snapshot()
    for name in whole:
        assert incremental[name] == whole[name], name
    return whole


def _touch_later(path) -> None:
    later = time.time() + 5
    os.utime(path, (later, later))


# ── the build on the live copy serves what a whole build serves ───────────────────────────────────────────────

def test_the_first_build_is_whole_and_writes_its_manifest():
    _seed()
    check = _build()
    assert check["result"] in ("changed", "unchanged") and "built whole" in _note(check), _note(check)
    with serving.open_read("fundamentals") as con:
        paths = {r[0] for r in con.execute(f"SELECT path FROM {dirty.MANIFEST_TABLE}").fetchall()}
        assert f"sec/companyfacts/cik={MSFT}/part-0.parquet" in paths
        assert con.execute(f"SELECT count(*) FROM {dirty.RULES_TABLE}").fetchone()[0] == 1


def test_nothing_changed_builds_on_the_live_copy_with_no_dirty_company(monkeypatch):
    _seed()
    _build()
    before = _snapshot()
    check = _build()
    assert "built on the live file: 0 Parquet file(s) changed, 0 key(s) dirty" in _note(check), _note(check)
    assert _snapshot() == before


def test_a_company_that_filed_is_reloaded_and_matches_a_whole_build(monkeypatch):
    _seed()
    _build()
    _write_facts(MSFT, fx.msft_facts(with_new_quarter=True), date(2026, 10, 29))
    _write_filings(MSFT, [("0000950170-25-100001", "10-K", None), ("0000950170-26-000002", "8-K", "2.02"),
                          ("0000950170-26-130001", "10-Q", None)])
    check = _build()
    assert "built on the live file" in _note(check) and "1 key(s) dirty" in _note(check), _note(check)
    whole = _same_as_whole(monkeypatch)
    assert (MSFT, date(2026, 9, 30)) in {(r[0], r[1]) for r in whole["fundamental_quarter"]}


def test_a_gone_partition_drops_its_companys_rows(monkeypatch):
    """The mtime alone cannot see a deleted partition (the old xfail); the manifest can."""
    _seed()
    _build()
    shutil.rmtree(common.parquet_dir(filings.NAME) / f"cik={HEI}")
    refused = _build()
    assert refused["result"] == "failed" and "filing_item shrinks" in _note(refused), "the shrink guard still holds"
    check = _build(allow_shrink=True)
    assert "built on the live file" in _note(check), _note(check)
    assert fx.q("SELECT count(*) FROM filing_item WHERE cik = ?", HEI) == [(0,)]
    _same_as_whole(monkeypatch)


def test_a_partition_carrying_an_old_mtime_is_still_dirty(monkeypatch):
    """A restore or an rsync that keeps an older mtime reads as clean against ``built_at``; the manifest sees the
    size and mtime differ (the old xfail)."""
    _seed()
    _build()
    part = common.parquet_dir(companyfacts.NAME) / f"cik={TM}" / "part-0.parquet"
    old = part.stat().st_mtime - 3600
    _write_facts(TM, fx.msft_facts())                        # other rows: another size
    os.utime(part, (old, old))
    check = _build()
    assert "built on the live file" in _note(check) and "1 key(s) dirty" in _note(check), _note(check)
    _same_as_whole(monkeypatch)


def test_a_company_that_joins_the_universe_is_loaded_although_its_partitions_are_old(monkeypatch):
    _seed([c for c in fx.COMPANIES if c["ticker"] != "TM"])
    _build()
    assert fx.q("SELECT count(*) FROM fundamental_company WHERE cik = ?", TM) == [(0,)]
    fx.spine()                                       # TM joins; its Parquet is older than the live file
    check = _build()
    assert "built on the live file" in _note(check), _note(check)
    assert fx.q("SELECT entity_name IS NOT NULL FROM fundamental_company WHERE cik = ?", TM) == [(True,)]
    _same_as_whole(monkeypatch)


def test_a_whole_reloaded_source_is_reloaded_on_the_live_copy(monkeypatch):
    _seed()
    _build()
    _write_calendar(["MSFT"], date(2026, 10, 29))
    check = _build()
    assert "built on the live file" in _note(check), _note(check)
    _same_as_whole(monkeypatch)


# ── when the build is still whole ─────────────────────────────────────────────────────────────────────────────

def test_a_change_of_the_build_rules_builds_whole(monkeypatch):
    _seed()
    _build()
    monkeypatch.setenv("RC_UW_PRELIMINARY", "1")                # a rules_state() of uw/financials
    check = _build()
    assert "built whole: the build code of fundamentals changed" in _note(check), _note(check)


def test_more_than_half_the_files_changed_builds_whole():
    _seed()
    _build()
    for p in sorted(base.parquet_root().rglob("*.parquet")):
        _touch_later(p)
    check = _build()
    assert "built whole" in _note(check) and "Parquet files changed" in _note(check), _note(check)


def test_a_new_wrds_snapshot_builds_whole(monkeypatch):
    _seed()
    _build()
    ds = next(d for d in wrds_fund.DATASETS if d.revisions)
    snap = ds.snapshot_file("2026-10-29")
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE s AS SELECT 'x' AS gvkey")
        base.write_parquet(con, "SELECT * FROM s", snap)
    finally:
        con.close()
    ctx = _ctx()
    ctx.changed = (snap,)
    assert ds.full_rebuild_reason(ctx) == "a new monthly snapshot (snapshot=2026-10-29)"


def test_a_serving_schema_bump_builds_whole(monkeypatch):
    _seed()
    _build()
    monkeypatch.setitem(serving.SERVING_SCHEMA_VERSION, "fundamentals", serving.SERVING_SCHEMA_VERSION["fundamentals"]
                        + 1)
    check = _build()
    assert "built whole: no live fundamentals file at serving schema version" in _note(check), _note(check)


def test_the_lever_by_hand_builds_whole(monkeypatch):
    _seed()
    _build()
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")
    assert "built whole: RC_BUILD_WHOLE=1" in _note(_build())


def test_a_live_file_without_a_manifest_falls_back_to_its_build_stamp(monkeypatch):
    """The live file the first deploy finds was built before the manifest: its ``built_at`` is the cutoff."""
    _seed()
    _build()
    live = serving.live_path("fundamentals")
    con = duckdb.connect(str(live))
    try:
        con.execute(f"DROP TABLE {dirty.MANIFEST_TABLE}")
        con.execute(f"DROP TABLE {dirty.RULES_TABLE}")
    finally:
        con.close()
    for p in base.parquet_root().rglob("*.parquet"):   # written a minute before that build, not in its second
        then = p.stat().st_mtime - 60
        os.utime(p, (then, then))
    _write_facts(MSFT, fx.msft_facts(with_new_quarter=True), date(2026, 10, 29))
    check = _build()
    assert "built on the live file" in _note(check) and "1 key(s) dirty" in _note(check), _note(check)
    _same_as_whole(monkeypatch)


def test_a_kind_whose_datasets_declare_no_dirty_set_is_built_whole_with_no_manifest():
    from RevenueChaser.rcdata.build import DATASETS
    market = [d for d in DATASETS.values() if d.kind == "market"]
    assert market and not dirty.incremental(market)
    assert dirty.plan("market", _ctx()) is None


def test_every_fundamentals_dataset_declares_a_dirty_set():
    assert dirty.incremental(base.kind_datasets("fundamentals"))
    keyed = {d.name for d in base.kind_datasets("fundamentals") if getattr(d, "dirty_keyed", False)}
    assert keyed == set(common.KEYED)


def test_a_partition_gone_before_the_first_manifest_is_still_dropped(monkeypatch):
    """The fallback cannot see a deleted file, and the manifest it writes would never show it: the keyed sources
    add the companies the live file serves whose folder is gone (review of #554)."""
    _seed()
    _build()
    con = duckdb.connect(str(serving.live_path("fundamentals")))
    try:
        con.execute(f"DROP TABLE {dirty.MANIFEST_TABLE}")
        con.execute(f"DROP TABLE {dirty.RULES_TABLE}")
    finally:
        con.close()
    for p in base.parquet_root().rglob("*.parquet"):
        then = p.stat().st_mtime - 60
        os.utime(p, (then, then))
    shutil.rmtree(common.parquet_dir(filings.NAME) / f"cik={HEI}")
    check = _build(allow_shrink=True)
    assert "built on the live file" in _note(check) and "1 key(s) dirty" in _note(check), _note(check)
    assert fx.q("SELECT count(*) FROM filing_item WHERE cik = ?", HEI) == [(0,)]
    _same_as_whole(monkeypatch)
