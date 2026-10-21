"""Common K5 (#383 R1): #247 adjacent fiscal years by period end, and the C18 comparability helper.

#247 (approved 2026-10-07): two annual records are year-on-year only when their period ends are 350-380 days apart;
otherwise ``prior`` is None (a None growth stays a fail for a gate, decision 8-A on #151). Records without
``period_ends`` keep the fiscal-year key rule. C18 is rules only here; AG S1 and Fundamentals F1 adopt them.
Synthetic records only.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core import fiscal_year as fy


def _rec(year, end=None, **kw) -> dict:
    rec = {"fiscal_year": year, **kw}
    if end:
        rec["period_ends"] = {f: end for f in ("revenue", *kw) if f != "period_ends"}
    return rec


# ── #247: adjacency by period end ────────────────────────────────────────────────────────────────────────────────
def test_365_days_apart_is_adjacent():
    annual = [_rec(2022, "2022-12-31", revenue=100.0), _rec(2023, "2023-12-31", revenue=120.0)]
    assert fy.prior(annual, 1) == 0


@pytest.mark.parametrize("prev_end, ok", [("2022-07-01", False),   # 548 days: a gap the key hides
                                          ("2023-01-20", False),   # 345 days
                                          ("2022-12-06", False),   # 390 days
                                          ("2023-01-15", True),    # 350 days, inclusive
                                          ("2022-12-16", True)])   # 380 days, inclusive
def test_only_350_to_380_days_count_as_one_year(prev_end, ok):
    annual = [_rec(2022, prev_end, revenue=100.0), _rec(2023, "2023-12-31", revenue=120.0)]
    assert fy.prior(annual, 1) == (0 if ok else None)


def test_a_52_53_week_filer_sharing_a_calendar_key_gives_none():
    # FY2023 ended 2023-01-01 and FY2024 ended 2023-12-30: both key 2023, so the later shadows the earlier, and
    # the record keyed 2022 (FY ended 2022-01-02) is two fiscal years before the 2023 record.
    annual = [_rec(2022, "2022-01-02", revenue=100.0), _rec(2023, "2023-12-30", revenue=150.0)]
    assert fy.prior(annual, 1) is None


def test_records_without_period_ends_keep_the_key_rule():
    annual = [_rec(2022, revenue=100.0), _rec(2023, revenue=120.0)]
    assert fy.prior(annual, 1) == 0
    annual = [_rec(2021, revenue=100.0), _rec(2023, revenue=120.0)]
    assert fy.prior(annual, 1) is None
    # one side without ends: the key rule decides
    annual = [_rec(2022, revenue=100.0), _rec(2023, "2023-12-31", revenue=120.0)]
    assert fy.prior(annual, 1) == 0


def test_records_without_a_fiscal_year_still_check_their_period_ends():
    # yfinance-like shapes pair by position; when both carry period ends, the 350-380 day rule still applies
    annual = [{"revenue": 100.0, "period_ends": {"revenue": "2022-07-01"}},
              {"revenue": 120.0, "period_ends": {"revenue": "2023-12-31"}}]
    assert fy.prior(annual, 1) is None
    annual[0]["period_ends"]["revenue"] = "2022-12-31"
    assert fy.prior(annual, 1) == 0


def test_the_record_end_is_the_revenue_end_else_the_newest_field_end():
    assert fy.period_end({"period_ends": {"revenue": "2023-06-30", "cash": "2023-07-31"}}) == date(2023, 6, 30)
    assert fy.period_end({"period_ends": {"cash": "2023-07-31", "capex": "2023-06-30"}}) == date(2023, 7, 31)
    assert fy.period_end({"period_ends": {}}) is None
    assert fy.period_end({"period_ends": {"revenue": "bad"}}) is None
    assert fy.period_end({}) is None


def test_ag_growth_and_dilution_follow_the_period_end_rule():
    from RevenueChaser.module_asymmetric_growth.engine import score_ticker

    annual = [_rec(2022, "2022-01-02", revenue=100.0, diluted_shares=100.0),
              _rec(2023, "2023-12-30", revenue=150.0, diluted_shares=130.0)]
    fin = {"info": {"country": "United States", "exchange": "NMS", "longName": "Week Co"},
           "annual": annual, "market_cap": 5e9, "source": "sec_edgar"}
    card = score_ticker("WEEK", fin, {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6,
                                      "above_sma50": True}, spine={})
    assert card["rev_growth"] is None
    assert card["dilution_yoy"] is None


# ── C18: comparability rules (helper only) ───────────────────────────────────────────────────────────────────────
def test_growth_over_a_nonpositive_prior_is_unknown():
    assert fy.growth(120.0, 0.0) == (None, ["prior_nonpositive"])
    assert fy.growth(120.0, -50.0) == (None, ["prior_nonpositive"])
    assert fy.growth(120.0, None) == (None, [])
    assert fy.growth(None, 100.0) == (None, [])


def test_growth_over_500_percent_is_flagged_not_dropped():
    value, flags = fy.growth(700.0, 100.0)
    assert value == pytest.approx(6.0) and flags == ["suspicious_growth"]
    value, flags = fy.growth(600.0, 100.0)                     # exactly 500 %: not flagged
    assert value == pytest.approx(5.0) and flags == []


def test_gross_profit_above_revenue_is_unknown_and_flagged():
    assert fy.gross_margin({"revenue": 100.0, "gross_profit": 120.0}) == (None, ["gross_profit_exceeds_revenue"])
    assert fy.gross_margin({"revenue": 0.0, "gross_profit": 10.0}) == (None, ["revenue_nonpositive"])
    assert fy.gross_margin({"revenue": 100.0, "gross_profit": 40.0}) == (pytest.approx(0.4), [])
    assert fy.gross_margin({"revenue": 100.0}) == (None, [])


def test_an_annual_older_than_470_days_is_stale():
    as_of = date(2026, 10, 10)
    assert fy.stale_days({"period_ends": {"revenue": "2025-06-27"}}, as_of) == 470
    assert fy.is_stale({"period_ends": {"revenue": "2025-06-27"}}, as_of) is False
    assert fy.is_stale({"period_ends": {"revenue": "2025-06-26"}}, as_of) is True
    assert fy.is_stale({}, as_of) is False                     # no end date: not judged here


def test_comparability_reads_one_anchor():
    annual = [_rec(2022, "2022-12-31", revenue=100.0, gross_profit=50.0),
              _rec(2023, "2023-12-31", revenue=800.0, gross_profit=900.0),
              _rec(2024, "2024-12-31", gross_profit=10.0)]     # no revenue: never the anchor
    c = fy.comparability(annual, as_of=date(2025, 3, 1))
    assert c["anchor"] == 1 and c["prior"] == 0
    assert c["period_end"] == "2023-12-31"                     # the anchor's end
    assert c["stale_days"] == 60 and c["stale"] is False        # age: the newest record's end, 2024-12-31 (#380)
    assert c["rev_growth"] == pytest.approx(7.0)
    assert c["gross_margin"] is None
    assert c["flags"] == ["suspicious_growth", "gross_profit_exceeds_revenue"]


def _no_revenue(year, end) -> dict:
    """A year whose filing the extractor read without a revenue figure (a revenue concept it misses)."""
    return {"fiscal_year": year, "total_assets": 1000.0, "period_ends": {"total_assets": end}}


def test_staleness_reads_the_newest_record_not_the_revenue_anchor():
    """#380 O-2 fix (PC measurement 2026-10-15): AKAM's newest record ends 2025-12-31 while its last revenue figure
    is FY2017's. The filer is not stale; only a newest record past the limit is."""
    as_of = date(2026, 10, 15)
    annual = [_rec(2017, "2017-12-31", revenue=100.0)] + [_no_revenue(y, f"{y}-12-31") for y in range(2018, 2026)]
    assert fy.latest_is_stale(annual, as_of) is False
    c = fy.comparability(annual, as_of)
    assert c["anchor"] == 0 and c["period_end"] == "2017-12-31"
    assert c["stale_days"] == 288 and c["stale"] is False
    old = annual[:-2]                                          # newest ends 2023-12-31: 1,019 days
    assert fy.latest_is_stale(old, as_of) is True
    assert fy.comparability(old, as_of)["stale_days"] == 1019 and fy.comparability(old, as_of)["stale"] is True
    assert fy.latest_is_stale([], as_of) is False
    assert fy.latest_is_stale([*old, {"fiscal_year": 2025, "revenue": 1.0}], as_of) is False  # no end: not judged


def test_comparability_of_no_records():
    c = fy.comparability([], as_of=date(2025, 3, 1))
    assert c["anchor"] is None and c["prior"] is None and c["rev_growth"] is None and c["stale"] is False
