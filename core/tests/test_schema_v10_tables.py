"""Schema 9 -> 10 (#292): 22 G1 tables, manifest only. Nothing already stored is rewritten.

The v9 store is built from a MANIFEST without the 22 tables and filled through
``write_rows``; then the real MANIFEST is restored and ``ensure_all_tables``
runs again, the way the dashboard's first store call does after the merge.
"""
from __future__ import annotations

import datetime as _dt

import duckdb
import pytest

from RevenueChaser.core import rawstore

V10_TABLES = (
    "price_actions", "index_levels_daily", "quote_snapshot", "short_interest",
    "vendor_financials", "vendor_valuation", "earnings_history", "estimate_snapshots",
    "identity_cusip", "ownership_snapshot", "sec_mf_file", "sec_exchange_file",
    "earnings_calendar", "macro_series", "vol_index_daily", "cftc_cot",
    "economic_calendar", "source_checks", "sec13f_filers", "edgar_companyfacts",
    "sec_submissions", "sec_submissions_cik",
)
EDGAR_TABLES = frozenset({"edgar_companyfacts", "sec_submissions", "sec_submissions_cik"})
PRICES_TABLES = frozenset({"price_actions",         # moved to prices.duckdb before its fill (#156, avo 2026-10-09)
                           "index_levels_daily"})   # D03 beside D01, before its writer (#293, avo 2026-10-10)


def _file_of(name: str) -> str:
    return "edgar" if name in EDGAR_TABLES else "prices" if name in PRICES_TABLES else "raw"
_ANNUAL = [{"fy": 2025, "revenue": 1.5e9}, {"fy": 2024, "revenue": 1.2e9}]
_META = {"cik": "0000320193", "name": "Apple Inc."}


def _forget_reconciliation():
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()


def _snapshot(path):
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position").fetchall()
    finally:
        con.close()
    out: dict[str, list] = {}
    for t, c, ty in rows:
        out.setdefault(t, []).append((c, ty))
    return out


def _tables(path) -> set[str]:
    return set(_snapshot(path)) if path.exists() else set()


def _col_name_type(col_def: str):
    parts = col_def.strip().split()
    head = parts[0].strip('"')
    if head.upper() == "PRIMARY":
        return None
    return head, parts[1].upper()


def _sample_row(spec: dict, i: int = 0) -> dict:
    row = {}
    for col_def in spec["columns"]:
        parsed = _col_name_type(col_def)
        if parsed is None:
            continue
        name, typ = parsed
        if typ == "DATE":
            row[name] = _dt.date(2026, 10, 5)
        elif typ == "DOUBLE":
            row[name] = float(1 + i)
        elif typ == "INTEGER":
            row[name] = 1 + i
        elif typ in ("JSON", "BLOB"):
            row[name] = {"field": "value", "n": i}
        else:
            row[name] = f"{name}_v"
    return row


def _spec(name: str) -> dict:
    assert name in rawstore.MANIFEST, name
    return rawstore.MANIFEST[name]


def _non_key_double(spec: dict) -> str:
    key = set(spec["key"])
    for col_def in spec["columns"]:
        parsed = _col_name_type(col_def)
        if parsed is None:
            continue
        name, typ = parsed
        if name not in key and typ == "DOUBLE":
            return name
    raise AssertionError(f"{spec} has no non-key DOUBLE column")


@pytest.mark.parametrize("name", V10_TABLES)
def test_table_is_created_only_in_its_declared_file(rawstore_tmp_db, name):
    expect = _file_of(name)
    assert rawstore._db_of(name) == expect
    files = {"raw": _tables(rawstore.DB_PATH), "edgar": _tables(rawstore.edgar_db_path()),
             "prices": _tables(rawstore.prices_db_path())}
    assert [db for db, tables in files.items() if name in tables] == [expect]


@pytest.mark.parametrize("name", V10_TABLES)
def test_a_written_row_reads_back(rawstore_tmp_db, name):
    spec = _spec(name)
    row = _sample_row(spec)
    rawstore.write_rows(name, [row])
    got = rawstore.get_all(name)
    assert len(got) == 1
    assert set(got[0]) == set(row)
    for col_def in spec["columns"]:
        parsed = _col_name_type(col_def)
        if parsed is None:
            continue
        col, typ = parsed
        if typ == "DOUBLE":
            assert float(got[0][col]) == row[col]
        else:
            assert got[0][col] == row[col]


@pytest.mark.parametrize("name", V10_TABLES)
def test_a_second_write_of_the_same_key_keeps_or_replaces(rawstore_tmp_db, name):
    spec = _spec(name)
    row = _sample_row(spec)
    flipped = _non_key_double(spec)
    rawstore.write_rows(name, [row])
    rawstore.write_rows(name, [{**row, flipped: 999.0}])
    got = rawstore.get_all(name)
    assert len(got) == 1
    if spec["write"] == "append":
        assert float(got[0][flipped]) == row[flipped]
    else:
        assert spec["write"] == "upsert" and name == "source_checks"
        assert float(got[0][flipped]) == 999.0


@pytest.mark.parametrize("name", V10_TABLES)
def test_a_null_in_a_key_column_is_refused(rawstore_tmp_db, name):
    spec = _spec(name)
    row = _sample_row(spec)
    row[spec["key"][-1]] = None
    with pytest.raises(Exception, match="NOT NULL"):
        rawstore.write_rows(name, [row])


@pytest.mark.parametrize("name", V10_TABLES)
def test_every_compressed_column_round_trips(rawstore_tmp_db, name):
    spec = _spec(name)
    assert not spec.get("history")
    compress = spec.get("compress") or {}
    blob_cols = []
    for col in spec["columns"]:
        parsed = _col_name_type(col)
        if parsed and parsed[1] == "BLOB":
            blob_cols.append(parsed[0])
    assert set(blob_cols) == set(compress)
    if name in ("quote_snapshot", "vendor_valuation"):
        assert compress == {"payload": "json"}
    if not compress:
        return
    row = _sample_row(spec, i=3)
    rawstore.write_rows(name, [row])
    got = rawstore.get_all(name)[0]
    for col, kind in compress.items():
        assert kind == "json"
        assert got[col] == row[col]
    with rawstore.connect(db=rawstore._db_of(name)) as conn:
        for col in compress:
            stored = conn.execute(f'SELECT typeof("{col}") FROM {name}').fetchone()[0]
            assert stored == "BLOB"


@pytest.fixture
def v9_store(isolate_rawstore):
    """A populated v9 store (the 22 G1 tables not yet declared); yields prior row counts."""
    missing = [t for t in V10_TABLES if t not in rawstore.MANIFEST]
    if missing:
        pytest.fail(f"G1 tables not in MANIFEST: {missing}")
    mp = pytest.MonkeyPatch()
    try:
        for t in V10_TABLES:
            mp.delitem(rawstore.MANIFEST, t)
        _forget_reconciliation()
        rawstore.ensure_all_tables()
        rawstore.write_rows("board_settings", [{"board": "swing", "key": "k", "value": 1.5,
                                                "updated_at": "2026-10-01"}])
        rawstore.write_rows("edgar_financials", [{"ticker": "AAPL", "annual": _ANNUAL,
                                                  "quarterly": [], "meta": _META}])
        rawstore.write_rows("job_runs", [{"run_id": "r9", "status": "done", "module": "swing"}])
        counts = {t: rawstore.count(t) for t in rawstore.MANIFEST}
    finally:
        mp.undo()
    for t in V10_TABLES:
        assert t not in _tables(rawstore.DB_PATH)
        assert t not in _tables(rawstore.edgar_db_path())
        assert t not in _tables(rawstore.prices_db_path())
    yield counts


def test_v9_store_gains_the_22_tables_and_keeps_every_row(v9_store):
    _forget_reconciliation()
    rawstore.ensure_all_tables()
    files = {"raw": _tables(rawstore.DB_PATH), "edgar": _tables(rawstore.edgar_db_path()),
             "prices": _tables(rawstore.prices_db_path())}
    assert len(V10_TABLES) == 22
    for name in V10_TABLES:
        assert [db for db, tables in files.items() if name in tables] == [_file_of(name)], name
        assert rawstore.count(name) == 0
    for name, n in v9_store.items():
        assert rawstore.count(name) == n, name
    assert rawstore.get("board_settings", ("swing", "k"))["value"] == 1.5
    edgar = rawstore.get("edgar_financials", "AAPL")
    assert edgar["annual"] == _ANNUAL and edgar["meta"] == _META
    assert rawstore.get("job_runs", "r9")["status"] == "done"

    files = (rawstore.DB_PATH, rawstore.edgar_db_path(), rawstore.prices_db_path())
    before = [_snapshot(p) for p in files]
    counts = {t: rawstore.count(t) for t in rawstore.MANIFEST}
    _forget_reconciliation()
    rawstore.ensure_all_tables()
    assert [_snapshot(p) for p in files] == before
    assert {t: rawstore.count(t) for t in rawstore.MANIFEST} == counts
