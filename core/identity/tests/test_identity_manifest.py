"""Part 1 of the identity master: the four store tables exist with typed columns."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore

TABLES = {
    "security_master": ["ticker"],
    "identity_conflicts": ["ticker", "kind", "field"],
    "identity_source_cache": ["source", "ticker"],
    "identity_runs": ["run_id"],
}


def test_the_schema_version_pin_is_explicit():
    """The label is pinned to a literal, so a bump is a deliberate act and not a side effect of a new table.

    12: the (accession, line_no) key on ``sec13f_holdings`` (#208). Bump the pin, the doc and the checkpoint
    warning together.
    """
    assert rawstore.schema_version() == "12"


@pytest.mark.parametrize("name,key", TABLES.items())
def test_table_is_in_the_raw_db_and_upserts(name, key):
    spec = rawstore.MANIFEST[name]
    assert spec["key"] == key
    assert spec["write"] == "upsert"
    assert spec.get("db", "raw") == "raw"


def test_security_master_columns():
    cols = rawstore._columns_of(rawstore.MANIFEST["security_master"])
    for c in ("ticker", "cik", "cik_source", "exchange", "name", "name_source",
              "security_type", "type_source", "type_asof", "sector", "sector_source",
              "industry_group", "group_source", "industry", "industry_source",
              "sub_industry", "sub_source", "gics_asof", "updated_at",
              "figi", "composite_figi", "share_class_figi", "exch_code", "security_description", "figi_source",
              "sic", "sic_description", "sic_group2", "sic_source"):
        assert c in cols, c
    assert len(cols) == 29


def test_identity_conflicts_columns():
    cols = rawstore._columns_of(rawstore.MANIFEST["identity_conflicts"])
    assert cols == ["ticker", "kind", "field", "side_a", "side_b", "note",
                    "first_seen", "last_seen"]


def test_source_cache_has_typed_filter_columns_and_json_payload():
    spec = rawstore.MANIFEST["identity_source_cache"]
    cols = rawstore._columns_of(spec)
    for c in ("source", "ticker", "fetched_at", "status", "security_type",
              "security_type2", "sector", "industry", "sub_industry", "cik", "sic",
              "is_etf", "forms", "shell_closed", "payload"):
        assert c in cols, c
    decl = {d.split()[0]: d for d in spec["columns"] if d.split()[0] in cols}
    assert "BOOLEAN" in decl["is_etf"] and "BOOLEAN" in decl["shell_closed"]
    assert "JSON" in decl["payload"]


def test_identity_runs_columns():
    spec = rawstore.MANIFEST["identity_runs"]
    decl = {d.split()[0]: d for d in spec["columns"]}
    assert set(decl) >= {"run_id", "started_at", "finished_at", "status", "sources", "counts"}
    assert "JSON" in decl["sources"] and "JSON" in decl["counts"]


def test_tables_are_created_and_roundtrip(rawstore_tmp_db):
    rawstore.ensure_all_tables()
    rawstore.write_rows("identity_source_cache", [
        {"source": "openfigi", "ticker": "AAPL", "status": "ok", "is_etf": False,
         "payload": {"name": "APPLE INC"}}])
    rawstore.write_rows("identity_source_cache", [
        {"source": "openfigi", "ticker": "AAPL", "status": "ok", "is_etf": True,
         "payload": {"name": "APPLE INC"}}])
    rows = rawstore.get_all("identity_source_cache")
    assert len(rows) == 1 and rows[0]["is_etf"] is True
    assert rows[0]["payload"] == {"name": "APPLE INC"}


def test_every_identity_table_takes_a_row_and_reads_it_back(rawstore_tmp_db):
    """Population test for ``core/tests/test_db_persistence.py``: all four tables round-trip."""
    rawstore.write_rows("security_master", [
        {"ticker": "AAPL", "cik": "320193", "cik_source": "sec_exchange", "security_type": "common",
         "type_source": "openfigi", "sector": "Technology", "sector_source": "provider_uw"}])
    rawstore.write_rows("identity_conflicts", [
        {"ticker": "AAPL", "kind": "sector", "field": "sector", "side_a": "Technology",
         "side_b": "Information Technology", "first_seen": "2026-09-30", "last_seen": "2026-09-30"}])
    rawstore.write_rows("identity_runs", [
        {"run_id": "r1", "status": "ok", "sources": {"openfigi": {"status": "ok", "rows": 2}},
         "counts": {"tickers": 2}}])
    assert rawstore.get("security_master", "AAPL")["sector_source"] == "provider_uw"
    assert rawstore.get("identity_conflicts", ("AAPL", "sector", "sector"))["side_b"] == "Information Technology"
    run = rawstore.get("identity_runs", "r1")
    assert run["sources"]["openfigi"]["rows"] == 2 and run["counts"] == {"tickers": 2}


# ── equity_universe.next_earnings_date (#265): an added column, no schema bump ────────────────
def _forget_reconciliation():
    # A second ensure in the same process is a no-op unless the memos are reset.
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()


def _universe_columns() -> dict[str, str]:
    import duckdb
    con = duckdb.connect(str(rawstore.DB_PATH), read_only=True)
    try:
        return dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                                "WHERE table_name = 'equity_universe'").fetchall())
    finally:
        con.close()


@pytest.fixture
def old_universe(isolate_rawstore):
    """The store as it is before #265: ``equity_universe`` built without the column and holding rows."""
    spec = rawstore.MANIFEST["equity_universe"]
    mp = pytest.MonkeyPatch()
    try:
        mp.setitem(rawstore.MANIFEST, "equity_universe", {
            **spec, "columns": [c for c in spec["columns"] if c.split()[0] != "next_earnings_date"]})
        _forget_reconciliation()
        rawstore.ensure_all_tables()
        rawstore.write_rows("equity_universe", [
            {"ticker": "AAPL", "name": "Apple Inc.", "market_cap": 3.0e12, "sector": "Technology",
             "industry": "Consumer Electronics", "industry_group": "Old Group", "sub_industry": "Old Sub",
             "price": 200.0, "quote": {"symbol": "AAPL", "x": [1, 2]}},
            {"ticker": "ZZZ", "name": "Zed", "market_cap": None, "sector": "Other"}])
    finally:
        mp.undo()
    assert "next_earnings_date" not in _universe_columns()
    _forget_reconciliation()
    return {r["ticker"]: r for r in rawstore.get_all("equity_universe")}


def test_equity_universe_declares_the_earnings_date_as_text():
    decl = {d.split()[0]: d for d in rawstore.MANIFEST["equity_universe"]["columns"]}
    assert decl["next_earnings_date"] == "next_earnings_date TEXT"


def test_the_first_store_call_adds_the_column_and_keeps_every_row(old_universe):
    rawstore.ensure_all_tables()                       # what the dashboard's first store call does
    assert _universe_columns()["next_earnings_date"] == "VARCHAR"
    after = {r["ticker"]: r for r in rawstore.get_all("equity_universe")}
    assert set(after) == set(old_universe)
    for t, was in old_universe.items():
        assert after[t] == {**was, "next_earnings_date": None}, t
    assert rawstore.schema_version() == "12", "the column came without a bump; the label last moved for the 13F v12 tables (#208)"


def test_the_first_write_adds_the_column_without_an_explicit_ensure(old_universe):
    """The path the apply takes on a live store that has not seen the column yet."""
    rawstore.update_columns("equity_universe", "ticker", [{"ticker": "AAPL", "next_earnings_date": "2026-11-19"}],
                            ["next_earnings_date"])
    assert rawstore.get("equity_universe", "AAPL")["next_earnings_date"] == "2026-11-19"
    assert rawstore.get("equity_universe", "AAPL")["quote"] == old_universe["AAPL"]["quote"]
    assert rawstore.get("equity_universe", "ZZZ")["next_earnings_date"] is None


def test_a_universe_rewrite_after_the_migration_still_takes_rows_without_the_column(old_universe):
    """``_save_universe`` rebuilds rows without ``next_earnings_date``: the column is NULL until the apply copies it."""
    rawstore.ensure_all_tables()
    rawstore.replace_all("equity_universe", [{"ticker": "AAPL", "name": "Apple Inc.", "market_cap": 3.0e12}])
    assert rawstore.get("equity_universe", "AAPL")["next_earnings_date"] is None


# ── security_master FIGI columns (#267): added columns, no schema bump ────────────────────────
_FIGI_COLS = ("figi", "composite_figi", "share_class_figi", "exch_code", "security_description", "figi_source")


def _master_columns() -> dict[str, str]:
    import duckdb
    con = duckdb.connect(str(rawstore.DB_PATH), read_only=True)
    try:
        return dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                                "WHERE table_name = 'security_master'").fetchall())
    finally:
        con.close()


@pytest.fixture
def old_master(isolate_rawstore):
    """The store as it is before #267: ``security_master`` built without the FIGI columns and holding rows."""
    spec = rawstore.MANIFEST["security_master"]
    mp = pytest.MonkeyPatch()
    try:
        mp.setitem(rawstore.MANIFEST, "security_master", {
            **spec, "columns": [c for c in spec["columns"] if c.split()[0] not in _FIGI_COLS]})
        _forget_reconciliation()
        rawstore.ensure_all_tables()
        rawstore.write_rows("security_master", [
            {"ticker": "AAPL", "cik": "320193", "cik_source": "sec_exchange", "name": "Apple Inc.",
             "security_type": "common", "type_source": "openfigi", "sector": "Technology",
             "sector_source": "provider_uw", "industry": "Consumer Electronics", "industry_source": "provider_uw",
             "updated_at": "2026-10-01T00:00:00Z"},
            {"ticker": "ZZZ", "security_type": "unknown"}])
    finally:
        mp.undo()
    assert not set(_FIGI_COLS) & set(_master_columns())
    _forget_reconciliation()
    return {r["ticker"]: r for r in rawstore.get_all("security_master")}


def test_security_master_declares_the_figi_columns_as_text():
    decl = {d.split()[0]: d for d in rawstore.MANIFEST["security_master"]["columns"]}
    for c in _FIGI_COLS:
        assert decl[c] == f"{c} TEXT", c


def test_the_first_store_call_adds_the_figi_columns_and_keeps_every_row(old_master):
    rawstore.ensure_all_tables()
    cols = _master_columns()
    assert all(cols[c] == "VARCHAR" for c in _FIGI_COLS)
    after = {r["ticker"]: r for r in rawstore.get_all("security_master")}
    assert set(after) == set(old_master)
    for t, was in old_master.items():
        assert after[t] == {**was, **dict.fromkeys(_FIGI_COLS)}, t
    assert rawstore.schema_version() == "12", "the columns came without a bump; the label last moved for the 13F v12 tables (#208)"


def test_the_first_master_write_after_the_migration_takes_the_figi_columns(old_master):
    rawstore.write_rows("security_master", [
        {"ticker": "AAPL", "cik": "320193", "figi": "BBG000B9XRY4", "composite_figi": "BBG000B9XRY4",
         "share_class_figi": "BBG001S5N8V8", "exch_code": "US", "security_description": "AAPL",
         "figi_source": "openfigi"}])
    row = rawstore.get("security_master", "AAPL")
    assert (row["figi"], row["share_class_figi"], row["figi_source"]) == ("BBG000B9XRY4", "BBG001S5N8V8", "openfigi")
    assert rawstore.get("security_master", "ZZZ")["figi"] is None


# ── security_master raw SIC group (#272, schema v9) ───────────────────────────────────────────
_SIC_COLS = ("sic", "sic_description", "sic_group2", "sic_source")


@pytest.fixture
def pre_272_master(isolate_rawstore):
    """The store as it is before #272: ``security_master`` built without the SIC columns, holding rows with FIGI."""
    spec = rawstore.MANIFEST["security_master"]
    mp = pytest.MonkeyPatch()
    try:
        mp.setitem(rawstore.MANIFEST, "security_master", {
            **spec, "columns": [c for c in spec["columns"] if c.split()[0] not in _SIC_COLS]})
        _forget_reconciliation()
        rawstore.ensure_all_tables()
        rawstore.write_rows("security_master", [
            {"ticker": "AAPL", "cik": "320193", "cik_source": "sec_exchange", "name": "Apple Inc.",
             "security_type": "common", "type_source": "openfigi", "sector": "Technology",
             "sector_source": "provider_uw", "industry": "Consumer Electronics", "industry_source": "provider_uw",
             "figi": "BBG000B9XRY4", "figi_source": "openfigi", "updated_at": "2026-10-03T00:00:00Z"},
            {"ticker": "ZZZ", "security_type": "unknown"}])
    finally:
        mp.undo()
    assert not set(_SIC_COLS) & set(_master_columns())
    _forget_reconciliation()
    return {r["ticker"]: r for r in rawstore.get_all("security_master")}


def test_security_master_declares_the_sic_columns_as_text():
    decl = {d.split()[0]: d for d in rawstore.MANIFEST["security_master"]["columns"]}
    for c in _SIC_COLS:
        assert decl[c] == f"{c} TEXT", c


def test_the_first_store_call_adds_the_sic_columns_and_keeps_every_row(pre_272_master):
    rawstore.ensure_all_tables()
    cols = _master_columns()
    assert all(cols[c] == "VARCHAR" for c in _SIC_COLS)
    after = {r["ticker"]: r for r in rawstore.get_all("security_master")}
    assert set(after) == set(pre_272_master)
    for t, was in pre_272_master.items():
        assert after[t] == {**was, **dict.fromkeys(_SIC_COLS)}, t
    assert after["AAPL"]["figi"] == "BBG000B9XRY4", "the FIGI columns of #267 are untouched"


def test_the_first_master_write_after_the_migration_takes_the_sic_columns(pre_272_master):
    rawstore.write_rows("security_master", [
        {"ticker": "AAPL", "cik": "320193", "sic": "3571", "sic_description": "Electronic Computers",
         "sic_group2": "35", "sic_source": "sec_submissions"}])
    row = rawstore.get("security_master", "AAPL")
    assert (row["sic"], row["sic_description"], row["sic_group2"], row["sic_source"]) == (
        "3571", "Electronic Computers", "35", "sec_submissions")
    assert rawstore.get("security_master", "ZZZ")["sic"] is None


def test_a_four_digit_code_with_a_leading_zero_survives_the_store():
    """TEXT, not INTEGER: ``0100`` (agricultural production) must not read back as 100."""
    rawstore.write_rows("security_master", [{"ticker": "AGRI", "sic": "0100", "sic_group2": "01"}])
    row = rawstore.get("security_master", "AGRI")
    assert (row["sic"], row["sic_group2"]) == ("0100", "01")
