"""G9: the 13F board reads its totals from the ownership serving store, not ``raw.duckdb``.

``rcdata/build/ownership`` has built ``sec13f_ticker_period`` since PR 16, while the board kept reading the old
``sec13f_totals`` rows the retired derive wrote. On a machine where only the builder ever ran, that table is empty
and the board renders nothing (homesrv, 5 Nov 2026).
"""
from __future__ import annotations

from datetime import date

import duckdb

from RevenueChaser.module_sec_13f import engine as sec_13f
from RevenueChaser.rcdata import serving

METRICS = {"shares": 500.0, "value": 1500.0, "holders": 3, "top10_pct": 42.5, "hhi": 120.5, "new_holders": 1,
           "exited_holders": 2, "buy_flow": 10.0, "sell_flow": 4.0, "breadth": 86.4216, "breadth_chg": 1.5}


def _store(rows) -> None:
    """A live ``ownership.duckdb`` holding only ``sec13f_ticker_period``, as a fresh build would."""
    path = serving.live_path("ownership")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE sec13f_ticker_period (ticker TEXT, period DATE, shares DOUBLE, value DOUBLE, "
                    "holders INTEGER, top10_pct DOUBLE, hhi DOUBLE, new_holders INTEGER, exited_holders INTEGER, "
                    "exited_confidential INTEGER, exited_no_filing INTEGER, buy_flow DOUBLE, sell_flow DOUBLE, "
                    "breadth DOUBLE, breadth_chg DOUBLE, prev_period DATE, filers INTEGER, cusips INTEGER, "
                    "filing_deadline DATE, filed_through DATE, source TEXT)")
        con.executemany(
            "INSERT INTO sec13f_ticker_period (ticker, period, shares, value, holders, top10_pct, hhi, "
            "new_holders, exited_holders, buy_flow, sell_flow, breadth, breadth_chg, filing_deadline) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [[t, p, METRICS["shares"], METRICS["value"], METRICS["holders"], METRICS["top10_pct"], METRICS["hhi"],
              METRICS["new_holders"], METRICS["exited_holders"], METRICS["buy_flow"], METRICS["sell_flow"],
              METRICS["breadth"], METRICS["breadth_chg"], date(p.year, p.month, 15)] for t, p in rows])
    finally:
        con.close()


def test_load_totals_reads_the_ownership_store():
    _store([("AAPL", date(2026, 3, 31)), ("AAPL", date(2026, 6, 30)), ("MSFT", date(2026, 6, 30))])

    totals = sec_13f.load_totals()

    assert sorted(totals) == ["AAPL", "MSFT"]
    assert sorted(totals["AAPL"]) == ["2026-03-31", "2026-06-30"]
    # ``complete`` rides with the metrics: the period's filing deadline against today (#514).
    assert totals["AAPL"]["2026-06-30"] == {**METRICS, "complete": True}
    assert totals["MSFT"] == {"2026-06-30": {**METRICS, "complete": True}}


def test_load_totals_is_empty_without_a_store():
    assert sec_13f.load_totals() == {}


def test_the_board_metrics_are_the_stores_metrics():
    """The cell keys are the store's own ``METRICS``, so the two cannot drift apart silently."""
    from RevenueChaser.core.ownership import SEC13F_METRICS
    from RevenueChaser.rcdata.build.ownership.serve import METRICS as store_metrics

    assert SEC13F_METRICS == store_metrics
