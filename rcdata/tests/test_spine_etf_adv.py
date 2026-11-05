"""``etf.adv_usd_20d``: the 20-session average daily dollar volume, read from Compustat's raw ``comp.secd``.

Issue #357, avo's decision B of 5 Nov 2026: a fund N-CEN gives no AUM for (GLD, SLV, IBIT) is decided on its own
dollar volume, and that volume is measured from the **raw Compustat Parquet**, not from the live ``prices.duckdb``.
The prices store keeps only today's universe (#382), so reading it would mean a fund already outside the universe
could never enter it again.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.rcdata.build.prices import stage as prices_stage, wrds_history
from RevenueChaser.rcdata.build.spine import etf

PROVENANCE = {"source_as_of": date(2026, 11, 2), "fetched_at": 1.0, "run_id": "test"}


def _row(gvkey: str, iid: str, day: date, tic: str, close: float, volume: float, **over) -> dict:
    row = {"gvkey": gvkey, "iid": iid, "datadate": day, "tic": tic, "cusip": "78462F103", "curcdd": "USD",
           "prccd": close, "prcod": close, "prchd": close, "prcld": close, "cshtrd": volume, "cshoc": 1e9,
           "ajexdi": 1.0, "trfd": 1.0, "exchg": 17, **PROVENANCE}
    row.update(over)
    return row


def put_secd(rows: list[dict]) -> None:
    """``rows`` written into Compustat's secd Parquet partitions, one file per year, as the fetch writes them."""
    by_year: dict[int, list[dict]] = {}
    for r in rows:
        by_year.setdefault(r["datadate"].year, []).append(r)
    for year, part in by_year.items():
        prices_stage.write_rows(part, wrds_history.SECD + wrds_history.PROVENANCE,
                                wrds_history.partition_path("secd", year), order="gvkey, iid, datadate")


def series(gvkey: str, iid: str, tic: str, sessions: int, dollars: float, *, close: float = 100.0,
           last: date = date(2026, 11, 2), **over) -> list[dict]:
    """``sessions`` priced days of one Compustat series whose close times volume is ``dollars``, newest ``last``."""
    return [_row(gvkey, iid, last - timedelta(days=i), tic, close, dollars / close, **over) for i in range(sessions)]


def test_a_fund_with_twenty_sessions_gets_its_average_dollar_volume():
    put_secd(series("131855", "01", "GLD", 20, 2_000_000_000.0))
    assert etf.adv_usd_20d(["GLD"]) == pytest.approx({"GLD": 2_000_000_000.0})


def test_a_fund_with_fewer_than_twenty_sessions_stays_unmeasured():
    """avo's rule is the last 20 sessions: an average over three of them is not that number, so the fund is
    absent from the answer and its ``adv_usd_20d`` stays NULL (undecided, not thin)."""
    put_secd(series("131855", "01", "GLD", 19, 2_000_000_000.0))
    assert etf.adv_usd_20d(["GLD"]) == {}


def test_only_the_newest_twenty_sessions_count():
    rows = series("131855", "01", "GLD", 20, 1_000_000_000.0)
    rows += series("131855", "01", "GLD", 20, 9_000_000_000.0, last=date(2026, 9, 1))
    put_secd(rows)
    assert etf.adv_usd_20d(["GLD"]) == pytest.approx({"GLD": 1_000_000_000.0})


def test_a_dot_class_ticker_is_read_with_the_spines_dash():
    put_secd(series("012345", "01", "BRK.B", 20, 500_000_000.0))
    assert etf.adv_usd_20d(["BRK-B"]) == pytest.approx({"BRK-B": 500_000_000.0})


def test_a_series_that_carried_the_ticker_long_ago_is_not_this_fund():
    """A reused ticker: the old series' newest priced day is far behind Compustat's own newest, so it does not
    name the fund (``TIC_RECENT_DAYS``, the rule of PR 420's ``UNIVERSE_SQL``)."""
    rows = series("099999", "01", "IBIT", 20, 8_000_000_000.0, last=date(2024, 3, 1))
    rows += series("131855", "01", "GLD", 20, 2_000_000_000.0)
    put_secd(rows)
    assert etf.adv_usd_20d(["IBIT", "GLD"]) == pytest.approx({"GLD": 2_000_000_000.0})


def test_two_live_series_on_one_ticker_leave_the_larger_volume():
    rows = series("131855", "01", "GLD", 20, 2_000_000_000.0)
    rows += series("222222", "02", "GLD", 20, 5_000_000.0)
    put_secd(rows)
    assert etf.adv_usd_20d(["GLD"]) == pytest.approx({"GLD": 2_000_000_000.0})


def test_a_non_usd_or_unpriced_or_volumeless_day_is_no_session():
    put_secd(series("888888", "01", "MAPLE", 20, 2_000_000_000.0, curcdd="CAD")
             + series("777777", "01", "DEAD", 20, 2_000_000_000.0, prccd=0.0)
             + series("666666", "01", "QUIET", 20, 2_000_000_000.0, cshtrd=None))
    assert etf.adv_usd_20d(["MAPLE", "DEAD", "QUIET"]) == {}


def test_a_day_after_the_builds_date_is_not_read():
    """A rebuild as of an earlier day sees that day's Compustat, not today's rows (the build is reproducible)."""
    put_secd(series("131855", "01", "GLD", 20, 2_000_000_000.0, last=date(2026, 11, 2))
             + series("131855", "01", "GLD", 20, 9_000_000_000.0, last=date(2026, 12, 1), close=200.0))
    assert etf.adv_usd_20d(["GLD"], as_of=date(2026, 11, 2)) == pytest.approx({"GLD": 2_000_000_000.0})


def test_a_window_that_crosses_the_year_reads_both_partitions():
    """The window is 20 sessions, the partitions are calendar years: a build in early January reads last year's
    file too (:data:`etf.ADV_YEARS`)."""
    put_secd(series("131855", "01", "GLD", 20, 2_000_000_000.0, last=date(2027, 1, 5)))
    assert etf.adv_usd_20d(["GLD"]) == pytest.approx({"GLD": 2_000_000_000.0})


def test_no_compustat_partition_at_all_answers_nothing():
    assert etf.adv_usd_20d(["GLD"]) == {}


def test_the_answer_does_not_read_the_live_prices_store(monkeypatch):
    """The source is the raw Parquet: a live ``prices.duckdb`` is not opened at all (#382 filters it to the
    universe, so a fund outside it has no bar there)."""
    from RevenueChaser.rcdata import serving
    put_secd(series("131855", "01", "GLD", 20, 2_000_000_000.0))

    def refuse(kind, *a, **k):
        raise AssertionError(f"the spine build opened the live {kind} store")

    monkeypatch.setattr(serving, "live_path", refuse)
    assert etf.adv_usd_20d(["GLD"]) == pytest.approx({"GLD": 2_000_000_000.0})


def test_the_window_and_the_recency_rule_are_the_numbers_the_plan_names():
    assert etf.ADV_SESSIONS == 20
    # Two weeks, the same recency rule as the prices build's own ticker-to-series step
    # (``prices/universe.ETF_TIC_RECENT_DAYS`` of PR 382, which is not on main yet).
    assert etf.TIC_RECENT_DAYS == 14
