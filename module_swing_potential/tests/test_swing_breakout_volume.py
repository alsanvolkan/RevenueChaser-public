"""Weinstein's volume confirmation reads the breakout window, not the last bar alone (Fable review of Swing,
28 Oct 2026, finding 3).

The ratio was the last session's volume over a 20-day average that included that same session: a breakout on 4x
volume yesterday that held today on normal volume read "failed", the pass count moved with the weekday of the scan,
and a 5x day read about 4.2x because the spike sat in its own denominator. It is now the best volume of the last
``VOL_WINDOW`` sessions over the average of the 20 sessions before that window.
"""
from __future__ import annotations

import pandas as pd
import pytest

from RevenueChaser.module_swing_potential import config, freeze, indicators, screeners


def _bars(spikes: dict[int, float]):
    """300 flat sessions at 1,000,000 shares; ``spikes`` maps a position from the end (-1 = the newest) to a volume."""
    idx = pd.bdate_range("2025-01-01", periods=300)
    close = pd.Series(100.0, index=idx)
    vol = pd.Series(1_000_000.0, index=idx)
    for pos, v in spikes.items():
        vol.iloc[pos] = v
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": vol})


def test_a_breakout_yesterday_that_holds_today_on_normal_volume_still_confirms():
    ind = indicators.compute_indicators(_bars({-2: 4_000_000.0}))
    assert ind["vol_ratio"] == pytest.approx(4.0)
    rule = next(r for r in screeners.evaluate_weinstein(ind)["rules"] if r["id"] == "volume_confirmation")
    assert rule["passed"] is True


def test_the_spike_is_not_in_its_own_average():
    ind = indicators.compute_indicators(_bars({-1: 5_000_000.0}))
    assert ind["vol_ratio"] == pytest.approx(5.0) and ind["vol_20d"] == pytest.approx(1_000_000.0)


def test_a_spike_before_the_window_is_the_average_not_the_breakout():
    ind = indicators.compute_indicators(_bars({-(config.VOL_WINDOW + 1): 21_000_000.0}))
    assert ind["vol_ratio"] == pytest.approx(1.0 / 2.0)             # 20 sessions: 19 x 1M + 21M = 2M a session


def test_the_window_is_part_of_the_ledger_model(monkeypatch):
    before = freeze.model_version()
    monkeypatch.setattr(config, "VOL_WINDOW", 3)
    assert freeze.effective_config()["VOL_WINDOW"] == 3 and freeze.model_version() != before


def test_a_v4_payload_keeps_its_method_toggles_and_says_v5_reads_the_window():
    from RevenueChaser.module_swing_potential.pipeline import build_payload
    from RevenueChaser.module_swing_potential.report import render_swing_dashboard
    p = build_payload([], run_stats={})
    p["is_fixture"] = False
    p["selection_version"] = "4"
    html = render_swing_dashboard(p)
    assert 'data-method="weinstein"' in html
    assert "Scored with v4; v5 reads Weinstein&#x27;s volume as the best of the last 5 sessions" in html
    assert 'id="old-version"' not in render_swing_dashboard({**p, "selection_version": config.SELECTION_VERSION})
