"""The WRDS datasets against a fake connection (never WRDS): one login per process for all three, attended only,
a monthly full snapshot, the new-name query between snapshots, and a failed login that keeps the last snapshot."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import sec_tickers, wrds

from . import spine_fixture as fx


@pytest.fixture
def fake(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    http_mock.add("company_tickers_exchange.json", fx.SEC_FILE)
    conn = fx.FakeWrds(with_new=False)
    logins = []

    def factory():
        logins.append(1)
        return conn
    monkeypatch.setattr(wrds, "_factory", factory)
    wrds.reset()
    yield conn, logins
    wrds.reset()


def _run(name, day="2026-10-27", by="cli", mode=None):
    params = {"as_of": day} if mode is None else {"mode": mode, "to": day}
    return base.run_request({"dataset": name, "requested_by": by, "params": params})


def test_three_datasets_one_login_and_one_query_per_table(fake):
    conn, logins = fake
    _run(sec_tickers.NAME)
    for ds in (wrds.SEC_IDHIST, wrds.CO_HGIC, wrds.IDXCST_HIS):
        row = _run(ds.name, mode="backfill")
        assert row["result"] == "changed", row
    assert logins == [1]
    assert [q.split(" FROM ")[1].split()[0] for q in conn.queries] == [
        "comp.company", "comp.security", "comp.sec_idhist", "comp.co_hgic", "comp.idxcst_his"]
    assert "gvkeyx IN ('000003', '024248', '030824')" in conn.queries[-1]
    assert ops.calls_today("wrds") == 5


def test_a_wrds_dataset_queued_by_the_schedule_or_a_page_is_skipped_without_a_login(fake):
    conn, logins = fake
    for by in ("schedule", "settings"):
        row = _run(wrds.SEC_IDHIST.name, by=by)
        assert row["result"] == "skipped" and "attended" in row["note"]
    assert logins == [] and conn.queries == []


def test_between_snapshots_only_the_new_names_are_asked(fake):
    conn, _ = fake
    _run(sec_tickers.NAME)
    _run(wrds.SEC_IDHIST.name, mode="backfill")
    conn.with_new = True
    conn.queries.clear()
    row = _run(wrds.SEC_IDHIST.name, day="2026-11-03")
    assert row["result"] == "changed"
    cik_q, sec_q, hist_q = conn.queries
    # The listed CIKs comp.company did not hold: VYLR (the OTC name and the one with no exchange are never asked).
    assert "cik IN ('0002000001')" in cik_q
    assert "FROM comp.security WHERE gvkey IN ('040001')" in sec_q
    assert "FROM comp.sec_idhist WHERE gvkey IN ('040001')" in hist_q
    with serving.open_read("spine") as con:
        row = con.execute("SELECT cusip, gvkey FROM security WHERE ticker = 'VYLR'").fetchone()
    assert row == ("92921V109", "040001")


def test_no_new_names_sends_no_query(fake):
    conn, _ = fake
    conn.with_new = True
    _run(sec_tickers.NAME)
    _run(wrds.SEC_IDHIST.name, mode="backfill")
    conn.queries.clear()
    row = _run(wrds.SEC_IDHIST.name, day="2026-11-03")
    assert conn.queries == [] and "no new names" in row["note"]


def test_a_failed_login_fails_the_run_and_keeps_the_served_snapshot(fake, monkeypatch):
    _run(sec_tickers.NAME)
    _run(wrds.SEC_IDHIST.name, mode="backfill")
    live = serving.live_path("spine").read_bytes()
    wrds.reset()

    def refused():
        raise wrds.LoginFailed("WRDS login failed: OperationalError")
    monkeypatch.setattr(wrds, "_factory", refused)
    row = _run(wrds.SEC_IDHIST.name, day="2026-12-01", mode="backfill")
    assert row["result"] == "failed" and "login failed" in row["note"]
    assert serving.live_path("spine").read_bytes() == live
    assert len(wrds._snapshots(wrds.SEC_IDHIST.name)) == 1


def test_no_wrds_interpreter_is_a_failed_login_naming_the_variable(monkeypatch, tmp_path):
    monkeypatch.delenv(wrds.PYTHON_ENV, raising=False)
    with pytest.raises(wrds.LoginFailed, match="RC_WRDS_PYTHON"):
        wrds.Worker()
    monkeypatch.setenv(wrds.PYTHON_ENV, str(tmp_path / "no-such-python"))
    with pytest.raises(wrds.LoginFailed, match="RC_WRDS_PYTHON"):
        wrds.Worker()


def test_a_raw_file_that_exists_is_not_asked_again(fake):
    conn, _ = fake
    _run(sec_tickers.NAME)
    raw = wrds.raw_dir(wrds.SEC_IDHIST.name) / "2026-10-27" / f"company{wrds.RAW_SUFFIX}"
    base.write_raw_json(wrds._meta(raw), {"as_of": "2026-10-27", "fetched_at": 1.0, "run_id": "r0",
                                          "query": "SELECT ...", "rows": len(fx.COMPANY)})
    fx.write_jsonl(raw, fx.COMPANY)
    _run(wrds.SEC_IDHIST.name, mode="backfill")
    assert [q.split(" FROM ")[1].split()[0] for q in conn.queries] == ["comp.security", "comp.sec_idhist"]


def test_cik_is_normalised_and_text_values_are_typed_by_the_schema(tmp_path):
    assert wrds.norm_cik("0000789019") == "789019" and wrds.norm_cik(None) is None
    raw = tmp_path / f"security{wrds.RAW_SUFFIX}"
    fx.write_jsonl(raw, [{"gvkey": "001", "iid": "01", "tic": "X", "cusip": None, "exchg": "11.0",
                          "excntry": "USA", "secstat": "A", "tpci": "0", "dldtei": "2020-01-02"}])
    import duckdb
    row = duckdb.connect().execute(wrds.raw_rows(raw, wrds.SECURITY)).fetchone()
    assert row == ("001", "01", "X", None, 11, "USA", "A", "0", date(2020, 1, 2))


def _security_raw(rows, day="2026-10-27"):
    raw = wrds.raw_dir(wrds.SEC_IDHIST.name) / day / f"security{wrds.RAW_SUFFIX}"
    base.write_raw_json(wrds._meta(raw), {"as_of": day, "fetched_at": 1.0, "run_id": "r0",
                                          "query": "SELECT ...", "rows": len(rows)})
    fx.write_jsonl(raw, rows)
    return raw


def _sec_row(**kw):
    row = {"gvkey": "001", "iid": "01", "tic": "X", "cusip": "000000001", "exchg": "11", "excntry": "USA",
           "secstat": "A", "tpci": "0", "dldtei": None}
    row.update(kw)
    return row


def test_two_raw_rows_of_one_key_keep_the_same_one_whatever_their_order():
    import duckdb
    kept = []
    for order in (["B", "A"], ["A", "B"]):
        raw = _security_raw([_sec_row(tic=t) for t in order])
        ctx = base.RunContext(run_id="r", dataset=wrds.SEC_IDHIST.name)
        (out,) = wrds.SEC_IDHIST.to_parquet_all(ctx, [raw])
        kept.append(duckdb.connect().execute(f"SELECT tic FROM read_parquet({base.sql_path(out)})").fetchall())
    assert kept == [[("A",)], [("A",)]]


def test_a_value_the_schema_cannot_type_is_a_build_issue_not_a_silent_null():
    raw = _security_raw([_sec_row(), _sec_row(iid="02", exchg="eleven", dldtei="not a date"),
                         _sec_row(iid="03", exchg=None)])
    ctx = base.RunContext(run_id="r", dataset=wrds.SEC_IDHIST.name)
    wrds.SEC_IDHIST.to_parquet_all(ctx, [raw])
    n, rows = ops.open_issues(wrds.SEC_IDHIST.name)
    got = sorted((r["kind"], r["key"], r["detail"]) for r in rows)
    assert got == [("value_untyped", "security.dldtei", "1 row(s) of 3 nulled (as DATE)"),
                   ("value_untyped", "security.exchg", "1 row(s) of 3 nulled (as INTEGER)")]
