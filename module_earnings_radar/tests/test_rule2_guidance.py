"""R2 — EPS track record vs the street. Table-driven, no network.

Pins (Phase 1): surprise uses ``(actual-est)/max(|est|, 0.10)`` winsorized ±50%;
central read = recency-weighted mean (weights 8..1) + median; beat = surprise
> +1%; sign gate = recency-mean AND median agree AND beat-rate ≥ 5/8 (bull) or
≤ 3/8 (bear), else mixed 0; trend term needs ≥2 recent + ≥2 prior quarters and
calls "improving" only with ≥4 consecutive beats. Coverage tiers 3-4 -> 0.40,
5-7 -> 0.60, 8+ -> 0.80. <3 usable quarters => unavailable (never 0).
"""
import pytest

from RevenueChaser.module_earnings_radar.rules.rule2_guidance import score


def _history(surprises: list[tuple[float, float]]) -> list[dict]:
    """[(actual_eps, street_mean_est)] -> earnings_history rows."""
    return [{"actual_eps": a, "street_mean_est": e} for a, e in surprises]


def _nq(actuals: list[float], estimates: list[float] | None = None) -> list[dict]:
    """actuals (est 1.0 each by default) -> n equal-strength quarters."""
    estimates = estimates or [1.0] * len(actuals)
    return _history(list(zip(actuals, estimates)))


def _stock_rows(actuals, estimates=None):
    """Stock-earnings print shape (report_type quarterly + reported/estimated)."""
    estimates = estimates or [1.0] * len(actuals)
    return [
        {"report_type": "quarterly", "fiscal_date_ending": f"2026-Q{i}",
         "reported_eps": str(a), "estimated_eps": str(e)}
        for i, (a, e) in enumerate(zip(actuals, estimates))
    ]


def test_insufficient_quarters_unavailable():
    r = score("AAPL", _history([(1.10, 1.00), (1.15, 1.00)]))
    assert r.available is False
    assert r.score is None  # never 0
    assert r.coverage == 0


def test_empty_history_unavailable():
    r = score("AAPL", [])
    assert r.available is False
    assert r.score is None


def test_estimates_zero_or_missing_unavailable():
    r = score("AAPL", [
        {"actual_eps": 1.10, "street_mean_est": 0.0},
        {"actual_eps": 1.10, "street_mean_est": None},
        {"actual_eps": 1.10},
    ])
    assert r.available is False
    assert r.score is None


def test_strong_beat_history():
    r = score("AAPL", _history([(1.10, 1.00), (1.15, 1.00), (1.20, 1.00)]))
    assert r.available is True
    assert r.score == 45.0  # +30 (recency-wtd ~17%) + +15 (beat rate 100%)
    assert r.coverage == 0.4  # 3 usable quarters -> lowest coverage tier
    assert any("strong beat history" in x for x in r.reasons)


def test_modest_positive_bias():
    r = score("AAPL", _history([(1.03, 1.00), (1.04, 1.00), (1.02, 1.00)]))
    assert r.available is True
    assert r.score == 30.0  # +15 (wtd ~3%) + +15 (beat rate 100%)
    assert r.coverage == 0.4


def test_consistent_miss():
    r = score("AAPL", _history([(0.90, 1.00), (0.85, 1.00), (0.88, 1.00)]))
    assert r.available is True
    assert r.score == -45.0  # -30 (miss ~-12%) + -15 (beat rate 0%)
    assert any("consistent miss" in x for x in r.reasons)


def test_penny_print_is_mixed_not_a_beat():
    """+0.01 surprise is <= the +1% beat bar, so no side -> mixed 0."""
    r = score("AAPL", _history([(1.01, 1.00), (0.99, 1.00), (1.00, 1.00)]))
    assert r.available is True
    assert r.score == 0.0
    assert any("mixed" in x for x in r.reasons)


def test_raw_metrics_exposed():
    r = score("AAPL", _history([(1.10, 1.00), (1.15, 1.00), (1.20, 1.00)]))
    assert r.raw_metrics["avg_bias"] == pytest.approx(0.167, abs=1e-3)
    assert r.raw_metrics["median_surprise"] == pytest.approx(0.15, abs=1e-3)
    assert r.raw_metrics["beat_rate"] == 1.0
    assert r.raw_metrics["quarters"] == 3


def test_coverage_tiers_by_history_depth():
    assert score("AAPL", _nq([1.05] * 3)).coverage == 0.4
    assert score("AAPL", _nq([1.05] * 4)).coverage == 0.4
    assert score("AAPL", _nq([1.05] * 5)).coverage == 0.6
    assert score("AAPL", _nq([1.05] * 7)).coverage == 0.6
    assert score("AAPL", _nq([1.05] * 8)).coverage == 0.8
    assert score("AAPL", _nq([1.05] * 20)).coverage == 0.8  # window capped at 8


def test_winsorized_surprise_cannot_dominate():
    # +400% special item clipped to +50%; recency weight on that print.
    r = score("AAPL", _nq([5.0, 1.02, 1.02]))
    assert r.available is True
    # est floor 1.0; surprise 5.0 -> winsorized 0.5; weighted over 1..3.
    assert r.raw_metrics["avg_bias"] == pytest.approx((1 * 0.5 + 2 * 0.02 + 3 * 0.02) / 6, abs=1e-4)
    assert r.score == 45.0  # +30 + +15


def test_old_quarters_drop_out_of_the_window():
    r = score("AAPL", _nq([1.05] * 12))
    assert r.raw_metrics["quarters"] == 8
    assert r.coverage == 0.8


def test_median_gate_blocks_one_sided_headline():
    """A positive recency-mean with a negative median is mixed, not bullish."""
    r = score("AAPL", _history([(1.30, 1.00), (0.98, 1.00), (0.97, 1.00)]))
    assert r.available is True
    # recency mean > 0 (newest +30% weighted) but median < 0.
    assert r.raw_metrics["avg_bias"] > 0
    assert r.raw_metrics["median_surprise"] < 0
    assert r.score == 0.0
    assert any("mixed" in x for x in r.reasons)


def test_trend_improving_with_consecutive_beats_adds_points():
    # older 4 quarters +3%, recent 4 quarters +8% (8 consecutive beats).
    r = score("AAPL", _nq([1.03] * 4 + [1.08] * 4))
    assert r.score == 55.0  # +30 + +15 + +10
    assert any("improving" in x for x in r.reasons)
    assert r.coverage == 0.8


def test_trend_improving_requires_consecutive_beats():
    """A single recent +11% outlier after misses is NOT 'accelerating'."""
    # prior: 3 misses (-1%) then 1 mild beat; recent: 3 mild beats +1 one +11%.
    rows = _history([(0.99, 1.00)] * 3 + [(1.01, 1.00)] * 3 + [(1.11, 1.00)])
    r = score("AAPL", rows)
    # Only the final print clears +1% -> consec < 4 -> no improving bonus.
    assert not any("improving" in x for x in r.reasons)


def test_recent_decline_with_positive_median_is_mixed():
    """Strong older beats then 4 mild misses: median still positive, beat rate
    4/8 -> neither bullish nor bearish -> mixed (0) with a declining note."""
    r = score("AAPL", _nq([1.10] * 4 + [0.99] * 4))
    assert r.available is True
    assert r.score == 0.0
    assert any("mixed" in x for x in r.reasons)
    assert any("declining" in x for x in r.reasons)


def test_declining_trend_with_bear_median_penalises():
    """Recent misses large enough to drag the median below 0 AND beat rate low:
    bear side fires and the decline adds the -15 penalty."""
    # 3 mild beats then 5 misses: beat 3/8 (<=3/8), median negative, recency avg
    # negative -> bear; declining recent vs earlier adds -15.
    rows = _history([(1.03, 1.00)] * 3 + [(0.90, 1.00)] * 5)
    r = score("AAPL", rows)
    assert r.available is True
    assert r.score < 0
    assert any("declining" in x for x in r.reasons)


def test_recency_is_chronological_by_report_date():
    # Supplied newest-first; the scorer sorts by date so result is identical.
    rev = [{"actual_eps": 1.08, "street_mean_est": 1.0, "report_date": f"2026-Q{i}"}
           for i in range(4, 0, -1)]
    rev += [{"actual_eps": 1.03, "street_mean_est": 1.0, "report_date": f"2025-Q{i}"}
            for i in range(4, 0, -1)]
    r = score("AAPL", rev)
    assert r.score == 55.0
    assert any("improving" in x for x in r.reasons)


def test_stock_earnings_row_shape_works():
    """The /stock/{t}/earnings print shape drives R2 identically."""
    r = score("AAPL", _stock_rows([1.10, 1.15, 1.20]))
    assert r.available is True
    assert r.score == 45.0


def test_stock_rows_drop_annual():
    r = score("AAPL", _stock_rows([1.10, 1.15, 1.20, 5.0, 6.0])[:3]
              + [{"report_type": "annual", "fiscal_date_ending": "2025-12-31",
                  "reported_eps": "2.0", "estimated_eps": "1.0"}])
    assert r.raw_metrics["quarters"] == 3  # annual row dropped


# ── #196 E-1: ordering (E10) and numeric zero EPS (E18) ─────────────────────

def _dated_rows(pairs_newest_first, dates_newest_first):
    return [
        {"report_type": "quarterly", "fiscal_date_ending": d, "reported_eps": a, "estimated_eps": e}
        for (a, e), d in zip(pairs_newest_first, dates_newest_first)
    ]


_D8 = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30",
       "2025-03-31", "2024-12-31", "2024-09-30", "2024-06-30"]


def test_e10_one_undated_row_does_not_flip_the_series():
    # Newest-first UW series: recent beats, old misses. An undated row used to turn
    # the "all rows dated" sort off, so the OLD misses were read as the newest.
    pairs = [(1.2, 1.0)] * 4 + [(0.8, 1.0)] * 4
    rows = _dated_rows(pairs, _D8)
    full = score("X", rows)
    rows_with_undated = rows + [{"report_type": "quarterly", "reported_eps": 0.5, "estimated_eps": 1.0}]
    partial = score("X", rows_with_undated)
    assert partial.score == full.score
    assert partial.raw_metrics["quarters"] == full.raw_metrics["quarters"]


def test_e10_dated_rows_sort_ascending_regardless_of_input_order():
    pairs = [(1.2, 1.0)] * 4 + [(0.8, 1.0)] * 4
    rows = _dated_rows(pairs, _D8)
    assert score("X", rows).score == score("X", list(reversed(rows))).score


def test_e18_numeric_zero_eps_is_a_real_print():
    # A reported EPS of exactly 0.00 against an estimate of 1.00 is a -100% miss
    # (winsorized), not a missing row.
    rows = [{"actual_eps": 0.0, "street_mean_est": 1.0}] * 3
    r = score("X", rows)
    assert r.available is True
    assert r.raw_metrics["quarters"] == 3
    assert r.score < 0


def test_e18_zero_in_the_first_alias_does_not_fall_through():
    rows = [{"actual_eps": 0.0, "reported_eps": 5.0, "street_mean_est": 1.0}] * 3
    assert score("X", rows).score < 0


def test_e10_undated_rows_are_dropped_not_counted():
    rows = _dated_rows([(1.2, 1.0)] * 6, _D8[:6])
    undated = {"report_type": "quarterly", "reported_eps": 0.5, "estimated_eps": 1.0}
    assert score("X", rows + [undated]).raw_metrics["quarters"] == 6
