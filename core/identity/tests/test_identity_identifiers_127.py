"""CUSIP and ISIN on ``security_master`` (#127). Additive columns, no schema bump.

``identity_cusip.current_map`` is keyed by CUSIP. Two present CUSIPs can name one
ticker (the live store was 1:1; the table does not forbid a collision). The tie
is newest ``valid_from``, then lowest CUSIP. No provider call, temp store only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import identity_cusip, rawstore
from RevenueChaser.core.identity.apply import apply_identifiers, ticker_identifiers

RUN_AT = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc).timestamp()
LATER = RUN_AT + 86400
ID_COLS = ("cusip", "isin", "cusip_source", "isin_source")


def _present(ticker):
    return {"status": "present", "ticker": ticker, "figi": "F-1", "share_class_figi": "S-1",
            "share_class": None, "name": ticker, "exch_code": "US", "security_type": "Common Stock",
            "fetched_at": RUN_AT}


def _forget():
    rawstore._TABLE_READY.clear()
    rawstore._ENSURED.clear()


def _columns(table: str) -> dict[str, str]:
    import duckdb
    con = duckdb.connect(str(rawstore.DB_PATH), read_only=True)
    try:
        return dict(con.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall())
    finally:
        con.close()


def test_security_master_declares_the_identifier_columns_as_text_and_tracks_them():
    decl = {d.split()[0]: d for d in rawstore.MANIFEST["security_master"]["columns"]}
    for col in ID_COLS:
        assert decl[col] == f"{col} TEXT", col
        assert col in rawstore.CHANGES["security_master"]["track"]
    assert rawstore.schema_version() == "12"


def test_two_cusips_for_one_ticker_keep_the_newest_valid_from_then_the_lowest_cusip():
    """Design choice (#127): a single column cannot hold two CUSIPs. Newest valid_from wins;
    a date tie keeps the lowest CUSIP. A missing valid_from loses to any date."""
    newer = ticker_identifiers({
        "594918104": {"ticker": "AAPL", "valid_from": "2026-10-14"},
        "037833100": {"ticker": "AAPL", "valid_from": "2026-10-15"},
    })
    assert newer["AAPL"]["cusip"] == "037833100"
    assert newer["AAPL"]["isin"] == "US0378331005"
    assert newer["AAPL"]["cusip_source"] == "openfigi_13f"
    assert newer["AAPL"]["isin_source"] == "derived_cusip"

    tied = ticker_identifiers({
        "594918104": {"ticker": "AAPL", "valid_from": "2026-10-14"},
        "037833100": {"ticker": "AAPL", "valid_from": "2026-10-14"},
    })
    assert tied["AAPL"]["cusip"] == "037833100"

    undated = ticker_identifiers({
        "037833100": {"ticker": "AAPL", "valid_from": None},
        "594918104": {"ticker": "AAPL", "valid_from": "2026-10-14"},
    })
    assert undated["AAPL"]["cusip"] == "594918104"


def test_a_cins_is_kept_but_not_turned_into_a_us_isin():
    got = ticker_identifiers({"G12345678": {"ticker": "SHEL", "valid_from": "2026-10-14"}})
    assert got["SHEL"] == {"cusip": "G12345678", "isin": None,
                            "cusip_source": "openfigi_13f", "isin_source": None}


def test_openfigi_slash_spelling_lands_on_the_spine_ticker():
    got = ticker_identifiers({"037833100": {"ticker": "brk/b", "valid_from": "2026-10-14"}})
    assert set(got) == {"BRK-B"}
    assert got["BRK-B"]["cusip"] == "037833100"


def test_apply_fills_cusip_and_isin_from_a_seeded_map_and_leaves_the_rest_null(rawstore_tmp_db):
    identity_cusip.record({"037833100": _present("AAPL"), "G12345678": _present("SHEL")},
                          run_id="job-1", run_at=RUN_AT)
    rawstore.write_rows("security_master", [
        {"ticker": "AAPL", "name": "Apple"},
        {"ticker": "SHEL", "name": "Shell"},
        {"ticker": "ZZZ", "name": "No cusip"},
    ])
    out = apply_identifiers()
    aapl = rawstore.get("security_master", "AAPL")
    assert (aapl["cusip"], aapl["isin"], aapl["cusip_source"], aapl["isin_source"]) == (
        "037833100", "US0378331005", "openfigi_13f", "derived_cusip")
    shel = rawstore.get("security_master", "SHEL")
    assert shel["cusip"] == "G12345678" and shel["isin"] is None and shel["isin_source"] is None
    zzz = rawstore.get("security_master", "ZZZ")
    assert zzz["cusip"] is None and zzz["isin"] is None
    assert zzz["cusip_source"] is None and zzz["isin_source"] is None
    assert out["cusip_applied"] == 2


def test_a_ticker_that_leaves_the_map_is_cleared(rawstore_tmp_db):
    rawstore.write_rows("security_master", [
        {"ticker": "OLD", "cusip": "037833100", "isin": "US0378331005",
         "cusip_source": "openfigi_13f", "isin_source": "derived_cusip"},
        {"ticker": "AAPL", "name": "Apple"},
    ])
    identity_cusip.record({"594918104": _present("AAPL")}, run_id="job-1", run_at=RUN_AT)
    apply_identifiers()
    old = rawstore.get("security_master", "OLD")
    assert (old["cusip"], old["isin"], old["cusip_source"], old["isin_source"]) == (None, None, None, None)
    assert rawstore.get("security_master", "AAPL")["isin"] == "US5949181045"


def test_an_empty_map_does_not_blank_identifiers(rawstore_tmp_db):
    rawstore.write_rows("security_master", [
        {"ticker": "AAPL", "cusip": "037833100", "isin": "US0378331005",
         "cusip_source": "openfigi_13f", "isin_source": "derived_cusip"},
    ])
    out = apply_identifiers()
    assert out["cusip_applied"] == 0 and out.get("cusip_skipped")
    assert rawstore.get("security_master", "AAPL")["cusip"] == "037833100"


def test_the_same_identifiers_again_append_no_history_row(rawstore_tmp_db):
    rawstore.write_rows("security_master", [{"ticker": "AAPL", "name": "Apple"}])
    identity_cusip.record({"037833100": _present("AAPL")}, run_id="job-1", run_at=RUN_AT)
    apply_identifiers()
    n = rawstore.query("SELECT count(*) AS n FROM security_master_changes")[0]["n"]
    apply_identifiers()
    assert rawstore.query("SELECT count(*) AS n FROM security_master_changes")[0]["n"] == n


def test_a_changed_identifier_appends_a_history_row(rawstore_tmp_db):
    rawstore.write_rows("security_master", [{"ticker": "AAPL", "name": "Apple"}])
    before = rawstore.query("SELECT count(*) AS n FROM security_master_changes")[0]["n"]
    identity_cusip.record({"037833100": _present("AAPL")}, run_id="job-1", run_at=RUN_AT)
    apply_identifiers()
    assert rawstore.query("SELECT count(*) AS n FROM security_master_changes")[0]["n"] == before + 1
    latest = rawstore.query(
        "SELECT cusip, isin, cusip_source, isin_source, status FROM security_master_changes "
        "WHERE ticker = 'AAPL' ORDER BY run_at DESC LIMIT 1")[0]
    assert latest == {"cusip": "037833100", "isin": "US0378331005",
                      "cusip_source": "openfigi_13f", "isin_source": "derived_cusip", "status": "present"}


def test_two_stored_cusips_for_one_ticker_follow_the_tie(rawstore_tmp_db):
    identity_cusip.record({"037833100": _present("AAPL")}, run_id="job-1", run_at=RUN_AT)
    identity_cusip.record({"594918104": _present("AAPL")}, run_id="job-2", run_at=LATER)
    rawstore.write_rows("security_master", [{"ticker": "AAPL", "name": "Apple"}])
    apply_identifiers()
    assert rawstore.get("security_master", "AAPL")["cusip"] == "594918104"


@pytest.fixture
def old_master(isolate_rawstore):
    """A store whose ``security_master`` was built before the identifier columns."""
    spec = rawstore.MANIFEST["security_master"]
    mp = pytest.MonkeyPatch()
    try:
        mp.setitem(rawstore.MANIFEST, "security_master", {
            **spec, "columns": [c for c in spec["columns"] if c.split()[0] not in ID_COLS]})
        _forget()
        rawstore.ensure_all_tables()
        rawstore.write_rows("security_master", [{"ticker": "AAPL", "name": "Apple Inc."}])
    finally:
        mp.undo()
    assert not set(ID_COLS) & set(_columns("security_master"))
    _forget()


def test_opening_an_old_store_adds_the_columns_without_a_bump(old_master):
    rawstore.ensure_all_tables()
    cols = _columns("security_master")
    assert all(cols[c] == "VARCHAR" for c in ID_COLS)
    row = rawstore.get("security_master", "AAPL")
    assert row["name"] == "Apple Inc."
    assert all(row[c] is None for c in ID_COLS)
    assert rawstore.schema_version() == "12"


def test_an_old_changes_table_gains_the_identifier_columns(rawstore_tmp_db):
    """DuckDB will not DROP a middle column an index follows, so the old table is recreated without them."""
    rawstore.ensure_all_tables()
    spec = rawstore.MANIFEST["security_master_changes"]
    old = [c for c in spec["columns"] if c.strip().split()[0].strip('"') not in ID_COLS]
    with rawstore.connect() as conn:
        conn.execute("DROP TABLE security_master_changes")
        conn.execute(f"CREATE TABLE security_master_changes ({', '.join(old)})")
    _forget()
    rawstore.write_rows("security_master", [{"ticker": "AAPL", "name": "Apple", "cusip": "037833100",
                                              "cusip_source": "openfigi_13f"}])
    cols = _columns("security_master_changes")
    assert all(col in cols for col in ID_COLS)
    got = rawstore.query("SELECT cusip, cusip_source FROM security_master_changes WHERE ticker = 'AAPL'")
    assert got == [{"cusip": "037833100", "cusip_source": "openfigi_13f"}]


def test_a_cusip_with_a_character_above_latin1_never_aborts_the_tie():
    """The tie used chr(255 - ord(c)) once and raised on a character above U+00FF; a bad row stays in the append-only
    identity_cusip, so every later identity run would have failed. The date is picked first, then the lowest CUSIP."""
    weird = "03783310\u0416"
    cmap = {
        weird: {"ticker": "AAA", "valid_from": "2026-10-10"},
        "037833100": {"ticker": "AAA", "valid_from": "2026-10-10"},
        "594918104": {"ticker": "BBB", "valid_from": None},
        "88160R101": {"ticker": "BBB", "valid_from": None},
    }
    out = ticker_identifiers(cmap)
    assert out["AAA"]["cusip"] == "037833100"       # same date: the lowest CUSIP
    assert out["BBB"]["cusip"] == "594918104"       # no dates at all: the lowest CUSIP
