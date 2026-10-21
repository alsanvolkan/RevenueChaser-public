"""Unit tests for the AI & Cloud Data Platform revenue analysis (offline)."""
from __future__ import annotations

import pandas as pd
import pytest

from RevenueChaser.scripts.ai_cloud_revenue import (
    aggregate,
    collect,
    partial_quarters,
    per_ticker_qoq,
    quarter_label,
    render_plot,
)


@pytest.mark.parametrize(
    "period_end,expected",
    [
        ("2025-03-31", "2025Q1"),
        ("2025-02-28", "2025Q1"),
        ("2025-01-31", "2025Q1"),
        ("2025-05-31", "2025Q2"),
        ("2025-07-31", "2025Q3"),
        ("2025-11-30", "2025Q4"),
        ("2025-12-31", "2025Q4"),
        ("2026-06-30", "2026Q2"),
    ],
)
def test_quarter_label_buckets_by_month(period_end, expected):
    assert quarter_label(period_end) == expected


@pytest.mark.parametrize("bad", ["", "2025", "2025-13-01", "not-a-date", "2025-00-01"])
def test_quarter_label_rejects_malformed(bad):
    assert quarter_label(bad) is None


def _fetch(rows_by_ticker):
    return lambda t: rows_by_ticker.get(t, [])


def test_collect_aligns_fiscal_quarters_and_ignores_annual():
    rows = {
        "AAA": [
            {"report_type": "quarterly", "fiscal_date_ending": "2025-02-28", "total_revenue": "100"},
            {"report_type": "quarterly", "fiscal_date_ending": "2025-05-31", "total_revenue": "110"},
            {"report_type": "annual", "fiscal_date_ending": "2025-05-31", "total_revenue": "999"},
            {"report_type": "quarterly", "fiscal_date_ending": "2023-12-31", "total_revenue": "1"},
        ],
        "BBB": [
            {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "total_revenue": "200"},
            {"report_type": "quarterly", "fiscal_date_ending": "2025-06-30", "total_revenue": "210"},
        ],
    }
    df = collect(["AAA", "BBB"], _fetch(rows))
    assert list(df.columns) == ["AAA", "BBB"]
    assert list(df.index) == ["2025Q1", "2025Q2"]
    # AAA's Feb and BBB's Mar both land in 2025Q1.
    assert df.loc["2025Q1", "AAA"] == 100.0
    assert df.loc["2025Q1", "BBB"] == 200.0
    assert df.loc["2025Q2", "AAA"] == 110.0
    # Pre-2024 row dropped; annual row never enters the series.
    assert 999.0 not in df.values


def test_collect_missing_revenue_is_nan_not_zero():
    rows = {
        "AAA": [
            {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "total_revenue": ""},
            {"report_type": "quarterly", "fiscal_date_ending": "2025-06-30", "total_revenue": "120"},
        ],
    }
    df = collect(["AAA"], _fetch(rows))
    # The reported quarter still exists (so it reads as partial), value NaN.
    assert "2025Q1" in df.index
    assert pd.isna(df.loc["2025Q1", "AAA"])  # absent, not 0
    assert df.loc["2025Q2", "AAA"] == 120.0


def test_blank_revenue_quarter_marks_segment_partial():
    rows = {
        "AAA": [
            {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "total_revenue": "100"},
            {"report_type": "quarterly", "fiscal_date_ending": "2025-06-30", "total_revenue": ""},
        ],
        "BBB": [
            {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "total_revenue": "200"},
            {"report_type": "quarterly", "fiscal_date_ending": "2025-06-30", "total_revenue": "210"},
        ],
    }
    df = collect(["AAA", "BBB"], _fetch(rows))
    assert partial_quarters(df) == ["2025Q2"]
    assert "2025Q2" not in aggregate(df).index


def test_collect_restatement_keeps_latest_period_end():
    rows = {
        "AAA": [
            {"report_type": "quarterly", "fiscal_date_ending": "2025-03-30", "total_revenue": "100"},
            {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "total_revenue": "105"},
        ],
    }
    df = collect(["AAA"], _fetch(rows))
    assert df.loc["2025Q1", "AAA"] == 105.0


def test_collect_duplicate_same_end_later_value_wins():
    rows = {
        "AAA": [
            {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "total_revenue": "100"},
            {"report_type": "quarterly", "fiscal_date_ending": "2025-03-31", "total_revenue": "105"},
        ],
    }
    df = collect(["AAA"], _fetch(rows))
    assert df.loc["2025Q1", "AAA"] == 105.0


def test_collect_empty_returns_empty_frame():
    df = collect(["AAA"], _fetch({}))
    assert df.empty
    assert list(df.columns) == ["AAA"]


def _frame():
    return pd.DataFrame(
        {"A": [100.0, 110.0, 120.0, 130.0, 140.0, 150.0],
         "B": [200.0, 210.0, 220.0, 230.0, 240.0, 250.0]},
        index=["2024Q4", "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1"],
    )


def test_aggregate_total_qoq_and_yoy():
    agg = aggregate(_frame())
    assert agg.loc["2025Q1", "total"] == 320.0
    assert agg.loc["2025Q1", "qoq_pct"] == pytest.approx((320 / 300 - 1) * 100)
    # 2026Q1 (400) vs 2025Q1 (320) = +25% YoY.
    assert agg.loc["2026Q1", "yoy_pct"] == pytest.approx(25.0)


def test_partial_quarter_dropped_from_aggregate():
    df = _frame()
    df.loc["2025Q3", "B"] = float("nan")
    assert partial_quarters(df) == ["2025Q3"]
    # A quarter missing a constituent is excluded, never summed as if the
    # missing revenue were 0.
    agg = aggregate(df)
    assert "2025Q3" not in agg.index


def test_per_ticker_qoq():
    qoq = per_ticker_qoq(_frame())
    assert qoq.loc["2025Q2", "A"] == pytest.approx((120 / 110 - 1) * 100)
    assert qoq.loc["2025Q2", "B"] == pytest.approx((220 / 210 - 1) * 100)


def test_render_plot_writes_png(tmp_path):
    df = _frame()
    agg = aggregate(df)
    out = tmp_path / "ai_cloud_revenue.png"
    render_plot(df, agg, out)
    assert out.exists() and out.stat().st_size > 0
