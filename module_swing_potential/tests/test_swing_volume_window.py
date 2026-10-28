"""Weinstein's volume confirmation looks at the breakout window, not one day (Swing review finding 3, PR 6).

Before, ``vol_ratio`` was the last session's volume over a 20-day mean that included that session: a breakout on
4x volume the day before the scan, holding near the high on normal volume, read as failed, so the Weinstein count
depended on the weekday of the scan; and a spike inflated its own average (a 5x day read about 4.2x). Now it is the
best of the last five sessions, each against the 20 sessions before it.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_swing_potential import config, indicators
from RevenueChaser.module_swing_potential.tests.test_swing import _ohlcv


def _with_volume(*tail: float):
    df = _ohlcv()
    df["Volume"] = 1_000_000.0
    for i, v in enumerate(reversed(tail), start=1):
        df.iloc[-i, df.columns.get_loc("Volume")] = v
    return df


def test_a_breakout_on_heavy_volume_the_day_before_still_confirms():
    ind = indicators.compute_indicators(_with_volume(4_000_000, 1_000_000))
    assert ind["vol_ratio"] == pytest.approx(4.0) and ind["vol_ratio"] >= config.VOL_CONFIRM


def test_a_spike_is_measured_against_the_days_before_it():
    assert indicators.compute_indicators(_with_volume(5_000_000))["vol_ratio"] == pytest.approx(5.0)


def test_a_spike_older_than_the_window_does_not_count():
    ind = indicators.compute_indicators(_with_volume(4_000_000, *[1_000_000] * 5))
    assert ind["vol_ratio"] < config.VOL_CONFIRM


def test_too_few_bars_for_the_window_is_unknown():
    assert indicators.compute_indicators(_ohlcv(periods=20))["vol_ratio"] is None
