"""The fundamentals layer's WRDS datasets against a fake worker (never WRDS): ``wrds/fundq`` and ``wrds/funda``, a
monthly snapshot whose changes against the one before are the revision table, and ``wrds/firm_ratio``; all three a
check of the SEC values (the agreement notes), attended, one login per process."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.fundamentals import companyfacts, wrds_fund
from RevenueChaser.rcdata.build.spine import wrds as spine_wrds

from . import fund_fixture as fx

GVKEYS = "gvkey IN ('005567', '010581', '012141', '012142')"
COMPUSTAT = "indfmt = 'INDL' AND datafmt = 'STD' AND popsrc = 'D' AND consol = 'C'"


@pytest.fixture
def fake(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_edgar, "SEC_DATA_BASE", "https://data.sec.test")
    fx.spine()
    fx.sec_routes(http_mock, {"789019": fx.msft_facts(), "46619": fx.hei_facts()})
    fx.run(companyfacts.NAME, mode="backfill", to="2026-10-26")
    conn, logins = fx.FakeFundWrds(), []

    def factory():
        logins.append(1)
        return conn
    monkeypatch.setattr(spine_wrds, "_factory", factory)
    spine_wrds.reset()
    yield conn, logins
    spine_wrds.reset()


def test_one_login_the_tier_a_gvkeys_and_the_compustat_filters(fake):
    conn, logins = fake
    for name in (wrds_fund.FUNDQ.name, wrds_fund.FUNDA.name, wrds_fund.FIRM_RATIO.name):
        check = fx.run(name, mode="backfill", to="2026-10-27")
        assert check["result"] == "changed", check["note"]
    assert logins == [1] and ops.calls_today("wrds") == 3
    fundq, funda, ratio = conn.queries
    assert "FROM comp.fundq" in fundq and GVKEYS in fundq and COMPUSTAT in fundq
    assert "datadate >= DATE '2016-10-27'" in fundq                     # ten years
    assert "FROM comp.funda" in funda and GVKEYS in funda and COMPUSTAT in funda
    assert "FROM wrds_finratio.firm_ratio" in ratio and GVKEYS in ratio and "public_date >= DATE '2023-10-27'" in ratio


def test_a_scheduled_run_is_skipped_without_a_login(fake):
    conn, logins = fake
    row = base.run_request({"dataset": wrds_fund.FUNDQ.name, "requested_by": "schedule",
                            "params": {"as_of": "2026-10-27"}})
    assert row["result"] == "skipped" and logins == [] and conn.queries == []


def test_the_monthly_snapshots_make_the_revision_table(fake):
    conn, _ = fake
    check = fx.run(wrds_fund.FUNDQ.name, mode="backfill", to="2026-10-27")
    # MSFT's Sep-2025 quarter agrees with the SEC on all three; HEI's revenue does not.
    assert "revenue 1 of 2, net income 1 of 1, diluted EPS 1 of 1 agree" in check["note"]
    assert fx.q("SELECT count(*) FROM fundamental_revision") == [(0,)]
    check = fx.run(wrds_fund.FUNDQ.name, "2026-11-10")
    assert len(conn.queries) == 1 and "snapshot of 2026-10-27 is current" in check["note"]
    conn.month = 2
    check = fx.run(wrds_fund.FUNDQ.name, "2026-11-27")
    assert len(conn.queries) == 2
    assert fx.q("SELECT cik, gvkey, source, datadate, item, old_value, new_value, old_as_of, new_as_of "
                "FROM fundamental_revision ORDER BY datadate") == [
        ("789019", "012141", wrds_fund.FUNDQ.name, date(2025, 6, 30), "revtq", 0.075, None, date(2026, 10, 27),
         date(2026, 11, 27)),
        ("789019", "012141", wrds_fund.FUNDQ.name, date(2025, 9, 30), "niq", 0.027, 0.0272, date(2026, 10, 27),
         date(2026, 11, 27))]                                    # the new quarter is not a revision
    assert "2 revision(s) in the snapshot of 2026-11-27 against 2026-10-27" in check["note"]


def test_funda_and_firm_ratio_agree_with_the_sec(fake):
    check = fx.run(wrds_fund.FUNDA.name, mode="backfill", to="2026-10-27")
    assert "revenue 1 of 1, net income 1 of 1, diluted EPS 1 of 1 agree" in check["note"]
    check = fx.run(wrds_fund.FIRM_RATIO.name, mode="backfill", to="2026-10-27")
    # MSFT's newest quarter (Sep 2025) only; HEI has no SEC total debt to compare.
    assert "debt to assets within 0.02 of WRDS on 1 of 1 companies" in check["note"]
