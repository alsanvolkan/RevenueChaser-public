"""``rcdata.read.universe`` / ``membership`` / ``identity``: the spine's read API (D4), with provenance and status,
one read-only open per call, and no rows (never an error) before the first build."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops, read, serving

from .test_spine_security import built, run_all  # noqa: F401 - the built spine of the 10 names


def test_nothing_built_yet_is_no_rows():
    assert read.universe() == [] and read.membership("sp500") == [] and read.identity(["MSFT"]) == []


@pytest.mark.usefixtures("built")
def test_universe_is_tier_a_by_default_with_provenance_and_status():
    rows = read.universe()
    assert {r["tier"] for r in rows} == {"A"} and "WFC-PC" not in {r["ticker"] for r in rows}
    msft = next(r for r in rows if r["ticker"] == "MSFT")
    assert msft["source"] == "sec/company_tickers_exchange" and msft["source_as_of"] == date(2026, 10, 27)
    assert msft["run_id"] and msft["fetched_at"] and msft["status"] == ops.latest_check("spine/security")["result"]
    both = {r["ticker"] for r in read.universe(tiers=("A", "B"))}
    assert {"WFC-PC", "SPY"} <= both and "OTCX" not in both


@pytest.mark.usefixtures("built")
def test_identity_of_named_tickers_any_tier():
    rows = read.identity(["wfc-pc", "MSFT", "NOPE"])
    assert [r["ticker"] for r in rows] == ["MSFT", "WFC-PC"]
    assert rows[0]["cusip"] == "594918104" and rows[1]["tier"] == "B"
    assert len(read.identity()) == 14


@pytest.mark.usefixtures("built")
def test_membership_now_and_on_a_past_day():
    from RevenueChaser.rcdata.build.spine import funds
    now = read.membership("sp400")
    assert {r["ticker"] for r in now} == {"HEI", "HEI-A"} and {r["source"] for r in now} == {funds.NAME}
    assert now[0]["status"] == ops.latest_check(funds.NAME)["result"]
    assert read.membership("sp400", on=date(2026, 10, 26)) == []          # before the first fund file
    wrds_then = read.membership("sp400", on=date(2026, 10, 1), source="wrds/idxcst_his")
    assert {r["ticker"] for r in wrds_then} == {"HEI", "HEI-A"}
    wrds_now = read.membership("sp400", on=date(2026, 10, 3), source="wrds/idxcst_his")
    assert {r["ticker"] for r in wrds_now} == {"HEI-A"}                  # HEI's period ended (exclusive end)
    assert {r["index_slug"] for r in read.membership()} == {"sp500", "sp400", "ndx"}


@pytest.mark.usefixtures("built")
def test_each_call_opens_the_live_file_once_read_only_and_closes_it(monkeypatch):
    seen = []
    real = serving.open_read

    def spy(kind):
        con = real(kind)
        seen.append(kind)
        return con
    monkeypatch.setattr(serving, "open_read", spy)
    read.universe()
    read.membership("sp500")
    read.identity(["MSFT"])
    assert seen == ["spine", "spine", "spine"]
