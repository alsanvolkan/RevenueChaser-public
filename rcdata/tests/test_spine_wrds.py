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


def test_no_user_name_is_a_failed_login_naming_the_variable(monkeypatch):
    monkeypatch.delenv(wrds.USER_ENV, raising=False)
    with pytest.raises(wrds.LoginFailed, match="WRDS_USERNAME"):
        wrds._connect()


def test_a_raw_file_that_exists_is_not_asked_again(fake):
    conn, _ = fake
    _run(sec_tickers.NAME)
    folder = wrds.raw_dir(wrds.SEC_IDHIST.name) / "2026-10-27"
    ctx = base.RunContext(run_id="r", dataset=wrds.SEC_IDHIST.name)
    wrds._save(ctx, folder / "company.json.gz", "SELECT ...", fx.COMPANY, date(2026, 10, 27))
    _run(wrds.SEC_IDHIST.name, mode="backfill")
    assert [q.split(" FROM ")[1].split()[0] for q in conn.queries] == ["comp.security", "comp.sec_idhist"]


def test_cik_and_dates_are_normalised():
    assert wrds.norm_cik("0000789019") == "789019" and wrds.norm_cik(None) is None
    import pandas as pd
    rows = wrds._records(pd.DataFrame([{"GVKEY": "001", "efffrom": pd.Timestamp("2020-01-02"), "x": float("nan")}]))
    assert rows == [{"gvkey": "001", "efffrom": "2020-01-02", "x": None}]
