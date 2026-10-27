"""The fixture serving file (PR 3, sub-step 1): built by the builder's own path, read back through ``rcdata.read``."""
from __future__ import annotations

from collections import Counter

from RevenueChaser.rcdata import read, serving

from . import rcdata_fixture as fx


def test_the_fixture_serves_five_sessions_of_two_expiries(rcdata_market):
    for session in fx.SESSIONS:
        rows = read.spx_expiry_strike(session, 0, 1)
        assert Counter(r["dte"] for r in rows) == {0: 155, 1: 239}, session
        assert {r["status"] for r in rows} == {"changed"}
        assert {r["source"] for r in rows} == {"uw"}


def test_a_friday_next_expiry_is_the_monday_at_one_dte(rcdata_market):
    rows = read.spx_expiry_strike(fx.FRIDAY, 0, 1)
    assert {(str(r["expiry"]), r["dte"]) for r in rows} == {("2026-10-23", 0), ("2026-10-26", 1)}


def test_the_series_the_board_reads_are_served(rcdata_market):
    ids = {r["series_id"] for r in read.market_series(["SPX", "VIX", "VVIX", "cftc:tff:13874A:open_interest_all"])}
    assert ids == {"SPX", "VIX", "VVIX", "cftc:tff:13874A:open_interest_all"}
    spx = read.market_series(["SPX"])
    assert spx[-1]["open"] is None and spx[-1]["value"] is not None, "Cboe's SPX file has one column"
    cot = read.market_series(["cftc:tff:13874A:asset_mgr_positions_long"])
    assert [str(r["obs_date"]) for r in cot] == [d.isoformat() for d in fx.COT_REPORTS]
    assert not read.market_series(["cftc:tff:13874A:change_in_asset_mgr_long"]), "PR 1 drops change_*"


def test_every_test_starts_without_a_serving_file():
    assert not serving.live_path("market").exists()
    assert read.spx_expiry_strike(fx.LAST) == []
