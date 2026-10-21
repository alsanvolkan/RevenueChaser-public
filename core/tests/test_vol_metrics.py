"""core.vol_metrics — Yang-Zhang RV and term-structure slope. Pure, no network.

The Yang-Zhang fixture and the slope curve are the same construction the
uw-earnings-vol-scan skill pins in its selftest, so R4's numbers transfer.
"""
from datetime import date

import pytest

from RevenueChaser.core.vol_metrics import (
    TRADING_PERIODS_PER_YEAR, clean_ohlc, term_structure_curve,
    term_structure_slope, yang_zhang,
)


def _drift_bars(n, drift=0.005, start=100.0):
    """Chronological bars drifting up `drift` per day from `start`."""
    bars = []
    prev = start
    for _ in range(n):
        o = prev
        c = prev * (1 + drift)
        h = max(o, c) * 1.002
        l = min(o, c) * 0.998
        bars.append({"open": o, "high": h, "low": l, "close": c})
        prev = c
    return bars


class TestCleanOhlc:
    def test_string_prices_are_coerced(self):
        rows = [{"date": "2026-07-01", "open": "100", "high": "101",
                 "low": "99", "close": "100.5"}]
        out = clean_ohlc(rows)
        assert out == [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}]

    def test_non_positive_or_garbage_rows_dropped(self):
        rows = [
            {"date": "2026-07-01", "open": "100", "high": "101", "low": "99", "close": "100.5"},
            {"date": "2026-07-02", "open": "0", "high": "1", "low": "0", "close": "1"},
            {"date": "2026-07-03", "open": "n/a", "high": "1", "low": "0", "close": "1"},
            {"open": "100", "high": "101", "low": "99", "close": "100"},  # no date
        ]
        assert len(clean_ohlc(rows)) == 1

    def test_as_of_drops_partial_today_bar(self):
        rows = _drift_bars(40)
        for i, b in enumerate(rows):
            b["date"] = date.fromordinal(date(2026, 1, 1).toordinal() + i).isoformat()
        out = clean_ohlc(rows, as_of=date(2026, 1, 20))
        assert len(out) == 19  # 2026-01-01..19 are strictly before 2026-01-20
        assert out[0]["close"] == rows[0]["close"]
        assert out[-1]["close"] == rows[18]["close"]


class TestYangZhang:
    def test_needs_window_plus_one_bars(self):
        assert yang_zhang(_drift_bars(20), window=30) is None
        assert yang_zhang([], window=30) is None

    def test_deterministic_fixture(self):
        rv = yang_zhang(_drift_bars(31))
        assert rv == pytest.approx(0.08452657074275242, rel=1e-9)

    def test_higher_vol_series_returns_higher_rv(self):
        calm = yang_zhang(_drift_bars(31, drift=0.001))
        wild = yang_zhang(_drift_bars(31, drift=0.005, start=100.0))
        assert wild > calm > 0

    def test_annualisation_factor_is_251(self):
        # A 1% daily close-to-close move with no gaps annualises ~ sqrt(251)*1%.
        # Sanity: factor lives in the module and feeds the result scale.
        assert TRADING_PERIODS_PER_YEAR == 251


class TestTermStructureCurve:
    def test_normalises_and_sorts(self):
        rows = [{"dte": "45", "volatility": "0.425"},
                {"dte": "7", "volatility": "0.60"},
                {"dte": "30", "volatility": "0.425"}]
        assert term_structure_curve(rows) == [(7, 0.6), (30, 0.425), (45, 0.425)]

    def test_drops_expired_dupes_and_garbage(self):
        rows = [{"dte": "7", "volatility": "0.6"},
                {"dte": "7", "volatility": "0.99"},   # dup dte -> first kept
                {"dte": "0", "volatility": "0.5"},    # expired
                {"dte": "-3", "volatility": "0.5"},   # expired
                {"dte": "x", "volatility": "0.5"},
                {"dte": "45", "volatility": "0.4"}]
        assert term_structure_curve(rows) == [(7, 0.6), (45, 0.4)]

    def test_fewer_than_two_points_returns_empty(self):
        assert term_structure_curve([{"dte": "7", "volatility": "0.6"}]) == []
        assert term_structure_curve([]) == []


class TestTermStructureSlope:
    def test_skill_fixture(self):
        curve = [(7, 0.60), (30, 0.425), (45, 0.425)]
        # -0.004605... matches the uw-earnings-vol-scan selftest exactly.
        assert term_structure_slope(curve) == pytest.approx(-0.004605263157894736, rel=1e-9)

    def test_contango_is_positive(self):
        curve = [(7, 0.30), (30, 0.40), (45, 0.45)]
        assert term_structure_slope(curve) > 0

    def test_backwardation_is_negative(self):
        curve = [(7, 0.60), (30, 0.425), (45, 0.40)]
        assert term_structure_slope(curve) < 0

    def test_curve_never_reaches_horizon_is_none(self):
        curve = [(7, 0.6), (30, 0.425)]
        assert term_structure_slope(curve) is None  # furthest 30 < 45

    def test_nearest_expiry_at_horizon_is_none(self):
        curve = [(45, 0.5), (60, 0.4)]
        assert term_structure_slope(curve) is None

    def test_interpolates_far_iv_between_points(self):
        # horizon 45 lies between 30 and 60 -> flat-ish slope from linear interp.
        curve = [(7, 0.5), (30, 0.5), (60, 0.35)]
        s = term_structure_slope(curve)
        assert s is not None and s < 0
