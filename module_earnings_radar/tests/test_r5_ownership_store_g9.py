"""G9 (build plan PR 17, #256, #252): Earnings R5 reads its squeeze inputs from the ownership store, as a tag.

* ``stored_inputs.short_rows`` reads FINRA's short interest and days to cover and IBKR's borrow fee and shares
  available (a count) from ``ownership.duckdb``; there is no float, so no SI %;
* a FINRA settlement is seen only once it could have been read (fetched, or the eighth session after it): no
  look-ahead (#252);
* the Fetch asks UW's ``/short_screener`` for nothing;
* the squeeze is a ``none`` / ``fuel`` tag from SI % of float or the borrow fee, with no Z ladder and no score; low
  short interest is not bearish (redesign.md R5, #256).

Fixture serving files in a temp ``MARKET_DIR``; offline.
"""
from __future__ import annotations

from datetime import date, datetime

import duckdb
import pytest

from RevenueChaser.module_earnings_radar import stored_inputs
from RevenueChaser.module_earnings_radar.rules import rule5_positioning
from RevenueChaser.module_sec_13f.tests.ownership_store import market  # noqa: F401 - the fixture

AS_OF = date(2026, 10, 28)                 # a Wednesday session
NY = stored_inputs.NY


def _epoch(d: date, hour: int = 18) -> float:
    return datetime(d.year, d.month, d.day, hour, tzinfo=NY).timestamp()


def _write(market, finra=(), ibkr=()):
    con = duckdb.connect(str(market / "ownership.duckdb"))
    try:
        con.execute("CREATE TABLE finra_short_interest (settlement_date DATE, symbol TEXT, ticker TEXT, "
                    "current_short BIGINT, days_to_cover DOUBLE, avg_daily_volume BIGINT, fetched_at DOUBLE, "
                    "source TEXT)")
        for t, d, n, dtc, fetched in finra:
            con.execute("INSERT INTO finra_short_interest VALUES (?, ?, ?, ?, ?, 1000, ?, 'finra')",
                        [d, t, t, n, dtc, _epoch(fetched)])
        con.execute("CREATE TABLE ibkr_shortable (session_date DATE, snapshot_at TEXT, symbol TEXT, ticker TEXT, "
                    "rebate_rate DOUBLE, fee_rate DOUBLE, available BIGINT, available_capped BOOLEAN, "
                    "fetched_at DOUBLE, source TEXT)")
        for t, s, fee, rebate, avail, capped in ibkr:
            con.execute("INSERT INTO ibkr_shortable VALUES (?, '09:45:02', ?, ?, ?, ?, ?, ?, 0, 'ibkr')",
                        [s, t, t, rebate, fee, avail, capped])
    finally:
        con.close()


# ── the reader ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_short_rows_read_finra_and_ibkr_from_the_ownership_store(market):
    _write(market,
           finra=[("AAA", date(2026, 9, 30), 1_000_000, 1.5, date(2026, 10, 9)),
                  ("AAA", date(2026, 10, 15), 2_500_000, 2.25, date(2026, 10, 24))],
           ibkr=[("AAA", date(2026, 10, 28), 35.0, -30.0, 120_000, False),
                 ("BBB", date(2026, 10, 28), 0.25, 3.5, 10_000_000, True)])
    rows, settled = stored_inputs.short_rows(["aaa", "BBB", "ZZZ"], as_of=AS_OF)
    assert settled == date(2026, 10, 15)
    assert rows["AAA"] == {"si_pct": None, "short_interest": 2_500_000.0, "days_to_cover": 2.25,
                           "short_as_of": date(2026, 10, 15), "borrow_fee": 35.0, "rebate_rate": -30.0,
                           "shares_available": 120_000.0}
    assert rows["BBB"] == {"si_pct": None, "borrow_fee": 0.25, "rebate_rate": 3.5, "shares_available": 10_000_000.0,
                           "shares_available_capped": True}
    assert "ZZZ" not in rows


def test_a_settlement_is_not_seen_before_it_could_have_been_read(market):
    """Oct 15 settles; fetched in a backfill on Nov 3, it is first readable on its eighth session (Oct 27)."""
    _write(market, finra=[("AAA", date(2026, 9, 30), 1_000_000, 1.5, date(2026, 10, 9)),
                          ("AAA", date(2026, 10, 15), 2_500_000, 2.25, date(2026, 11, 3))])
    rows, _ = stored_inputs.short_rows(["AAA"], as_of=date(2026, 10, 22))
    assert rows["AAA"]["short_as_of"] == date(2026, 9, 30) and rows["AAA"]["short_interest"] == 1_000_000.0
    rows, _ = stored_inputs.short_rows(["AAA"], as_of=date(2026, 10, 27))
    assert rows["AAA"]["short_as_of"] == date(2026, 10, 15)
    assert stored_inputs.finra_visible_from(date(2026, 10, 15), _epoch(date(2026, 10, 21))) == date(2026, 10, 21)


def test_a_stale_ibkr_snapshot_and_an_old_settlement_are_not_read(market):
    _write(market, finra=[("AAA", date(2026, 8, 14), 1_000_000, 1.5, date(2026, 8, 26))],
           ibkr=[("AAA", date(2026, 10, 20), 35.0, -30.0, 120_000, False)])
    assert stored_inputs.short_rows(["AAA"], as_of=AS_OF) == ({}, None)


@pytest.mark.parametrize("session, read", [(date(2026, 10, 23), True), (date(2026, 10, 22), False)])
def test_an_ibkr_snapshot_is_read_up_to_max_lag_sessions_back(market, session, read):
    """Oct 23 is three sessions before Oct 28 (``MAX_LAG["short_interest"]``), Oct 22 four."""
    _write(market, ibkr=[("AAA", session, 35.0, -30.0, 120_000, False)])
    assert ("AAA" in stored_inputs.short_rows(["AAA"], as_of=AS_OF)[0]) is read


def test_no_ownership_file_is_no_rows(market):
    assert stored_inputs.short_rows(["AAA"], as_of=AS_OF) == ({}, None)


def test_the_pipeline_reads_the_store_and_asks_uw_for_nothing(market, monkeypatch):
    from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
    _write(market, finra=[("AAA", date(2026, 10, 15), 2_500_000, 2.25, date(2026, 10, 24))],
           ibkr=[("AAA", date(2026, 10, 28), 35.0, -30.0, 120_000, False)])
    monkeypatch.setattr(stored_inputs.calendar, "latest_complete_session", lambda *a, **k: AS_OF)

    class _UW:
        def short_screener(self, *_a, **_k):
            raise AssertionError("R5 must not ask UW for short interest")
    p = EarningsPipeline(["AAA", "BBB"], uw=_UW())
    shorts = p._warm_short_interest()
    assert shorts["AAA"]["borrow_fee"] == 35.0 and "BBB" not in shorts
    assert p.input_sources["short_interest"] == {"stored": 1, "live": 0, "source": "rcdata:ownership",
                                                 "session": "2026-10-15"}


# ── the tag ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_expensive_borrow_is_fuel_and_scores_nothing():
    r = rule5_positioning.score(borrow_fee=35.0, short_interest=2_500_000, days_to_cover=2.25,
                                shares_available=120_000)
    assert r.available and r.score == 0.0 and r.coverage == 0.5
    assert r.raw_metrics["squeeze"] == "fuel" and "squeeze fuel (borrow fee 35.0%)" in r.reasons
    assert r.raw_metrics["shares_available"] == 120_000 and r.raw_metrics["short_interest"] == 2_500_000


def test_a_cheap_borrow_and_low_short_interest_are_none_never_bearish():
    for kwargs in ({"borrow_fee": 0.25}, {"short_interest_pct_float": 1.0}):
        r = rule5_positioning.score(**kwargs)
        assert r.score == 0.0 and r.raw_metrics["squeeze"] == "none" and "squeeze: none" in r.reasons, kwargs


@pytest.mark.parametrize("si, fee", [(25.0, None), (5.0, 12.0)])
def test_either_field_over_its_line_is_fuel(si, fee):
    assert rule5_positioning.score(short_interest_pct_float=si, borrow_fee=fee).raw_metrics["squeeze"] == "fuel"


def test_finras_shares_alone_make_no_tag():
    """No float and no borrow fee: the tag cannot be read, so the squeeze is missing, not ``none``."""
    r = rule5_positioning.score(short_interest=2_500_000, days_to_cover=2.25)
    assert not r.available and "squeeze" not in r.raw_metrics and r.raw_metrics["days_to_cover"] == 2.25


def test_a_lean_and_a_tag_are_full_coverage_and_only_the_lean_scores():
    r = rule5_positioning.score(institutional_net_delta=100_000.0, borrow_fee=35.0)
    assert r.coverage == 1.0 and r.score == 20.0 and r.raw_metrics["squeeze"] == "fuel"
