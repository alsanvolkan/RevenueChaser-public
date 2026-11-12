"""The watch-list columns computed from daily bars: performance, risk and the three ratings (#786).

Each test either checks a value that can be worked out by hand, or checks the rule the column promises — that a
series too short gives ``None`` rather than a number made from the wrong window.
"""
from __future__ import annotations

import math

import pytest

from RevenueChaser.module_watchlists import indicators as ind
from RevenueChaser.module_watchlists.indicators import Bar


def bars(closes, *, spread: float = 0.0, volume: float | None = 1_000.0):
    """A bar per close; ``spread`` puts a high and a low that far either side of it."""
    return [Bar(close=c,
                high=c * (1 + spread) if spread else c,
                low=c * (1 - spread) if spread else c,
                volume=volume) for c in closes]


def rising(n: int, start: float = 100.0, step: float = 1.0):
    return bars([start + step * i for i in range(n)], spread=0.01)


def falling(n: int, start: float = 300.0, step: float = 1.0):
    return bars([start - step * i for i in range(n)], spread=0.01)


# ── the bands ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("score, word", [
    (1.0, "strong_buy"), (0.5, "strong_buy"), (0.49, "buy"), (0.1, "buy"), (0.09, "neutral"),
    (0.0, "neutral"), (-0.09, "neutral"), (-0.1, "sell"), (-0.49, "sell"), (-0.5, "strong_sell"),
    (-1.0, "strong_sell"),
])
def test_a_score_gets_tradingviews_word(score, word):
    assert ind.label(score) == word


def test_no_score_is_no_word():
    assert ind.label(None) is None


# ── moving averages ──────────────────────────────────────────────────────────

def test_the_simple_average_is_the_mean_of_the_window():
    assert ind.sma([1, 2, 3, 4, 5], 5) == 3
    assert ind.sma([1, 2, 3, 4, 5], 2) == 4.5


def test_the_weighted_average_leans_on_the_newest_value():
    # (1*1 + 2*2 + 3*3) / 6
    assert ind.wma([1, 2, 3], 3) == pytest.approx(14 / 6)


def test_an_average_of_a_flat_series_is_that_value():
    flat = [7.0] * 60
    assert ind.sma(flat, 50) == 7
    assert ind.ema(flat, 50) == pytest.approx(7)
    assert ind.hull(flat, 9) == pytest.approx(7)


@pytest.mark.parametrize("fn, period", [(ind.sma, 200), (ind.ema, 200), (ind.wma, 200), (ind.hull, 9)])
def test_an_average_of_a_series_too_short_is_none(fn, period):
    assert fn([1.0, 2.0], period) is None


def test_the_volume_weighted_average_follows_the_heavier_sessions():
    rows = [Bar(close=10, volume=1), Bar(close=20, volume=3)]
    assert ind.vwma(rows, 2) == pytest.approx((10 * 1 + 20 * 3) / 4)


def test_the_volume_weighted_average_needs_volume_on_every_session():
    assert ind.vwma([Bar(close=10, volume=1), Bar(close=20, volume=None)], 2) is None


def test_the_ichimoku_base_is_the_midpoint_of_the_range():
    rows = [Bar(close=c, high=c + 1, low=c - 1) for c in range(1, 27)]
    assert ind.ichimoku_base(rows, 26) == pytest.approx(((26 + 1) + (1 - 1)) / 2)


# ── oscillators ──────────────────────────────────────────────────────────────

def test_rsi_of_an_unbroken_rise_is_a_hundred_and_of_an_unbroken_fall_is_zero():
    assert ind.rsi([float(i) for i in range(1, 40)]) == 100
    assert ind.rsi([float(i) for i in range(40, 1, -1)]) == 0


def test_rsi_of_a_flat_series_sits_in_the_middle():
    assert ind.rsi([5.0] * 40) == 50


def test_rsi_needs_one_more_session_than_its_period():
    assert ind.rsi([1.0] * 14) is None
    assert ind.rsi([1.0] * 15) is not None


def test_momentum_is_the_change_over_its_period():
    assert ind.momentum([float(i) for i in range(20)], 10) == 10


def test_cci_of_a_flat_series_is_zero():
    assert ind.cci(bars([3.0] * 25, spread=0.01), 20) == 0


def test_cci_is_positive_on_a_rise_and_negative_on_a_fall():
    assert ind.cci(rising(40)) > 0
    assert ind.cci(falling(40)) < 0


def test_the_awesome_oscillator_is_positive_on_a_rise():
    assert ind.awesome(rising(60)) > 0
    assert ind.awesome(falling(60)) < 0


def test_stochastic_is_high_at_the_top_of_the_range_and_low_at_the_bottom():
    assert ind.stochastic(rising(40)) > 85
    assert ind.stochastic(falling(40)) < 20


def test_williams_r_runs_from_minus_hundred_to_zero():
    assert -20 < ind.williams_r(rising(40)) <= 0
    assert -100 <= ind.williams_r(falling(40)) < -80


def test_the_ultimate_oscillator_stays_inside_its_range():
    value = ind.ultimate(rising(60))
    assert 0 <= value <= 100


def test_bull_bear_power_is_positive_when_the_session_sits_above_the_average():
    assert ind.bull_bear_power(rising(40)) > 0
    assert ind.bull_bear_power(falling(40)) < 0


def test_the_macd_histogram_is_positive_while_the_rise_is_quickening():
    assert ind.macd_histogram([100 * 1.01 ** i for i in range(80)]) > 0


@pytest.mark.parametrize("fn", [ind.cci, ind.awesome, ind.stochastic, ind.williams_r, ind.ultimate,
                                ind.bull_bear_power])
def test_an_oscillator_without_highs_and_lows_is_none(fn):
    assert fn([Bar(close=float(i)) for i in range(60)]) is None


# ── the ratings ──────────────────────────────────────────────────────────────

def test_the_moving_averages_vote_buy_on_a_long_rise():
    votes = ind.ma_votes(rising(400))
    assert len(votes) == 15
    assert -1 not in votes
    # Hull MA has no lag on a straight line, so it sits on the price and abstains; every other average votes buy.
    assert votes.count(1) >= 14


def test_the_moving_averages_vote_sell_on_a_long_fall():
    votes = ind.ma_votes(falling(400, start=600.0))
    assert 1 not in votes
    assert votes.count(-1) >= 14


def test_an_average_the_series_is_too_short_for_does_not_vote():
    assert len(ind.ma_votes(rising(40))) < 15


def test_a_long_rise_rates_a_strong_buy_and_a_long_fall_a_strong_sell():
    up = ind.ratings(rising(400))
    assert up["ma"] >= 0.9
    assert up["ma_label"] == "strong_buy"
    assert up["tech"] > 0
    down = ind.ratings(falling(400, start=600.0))
    assert down["ma"] <= -0.9
    assert down["ma_label"] == "strong_sell"
    assert down["tech"] < 0


def test_the_technical_rating_is_the_mean_of_the_other_two():
    out = ind.ratings(rising(400))
    assert out["tech"] == pytest.approx((out["ma"] + out["os"]) / 2)


def test_a_rating_of_an_empty_series_is_none_not_a_guess():
    out = ind.ratings([])
    assert out == {"ma": None, "ma_label": None, "os": None, "os_label": None,
                   "tech": None, "tech_label": None}


def test_the_oscillators_never_cast_more_than_eleven_votes():
    assert len(ind.os_votes(rising(400))) == 11


def test_the_trend_strength_oscillator_never_votes_a_direction():
    # ADX is counted but always neutral, so an all-buy series cannot score a flat 1 on the oscillators.
    assert ind.ratings(rising(400))["os"] < 1


# ── performance ──────────────────────────────────────────────────────────────

def test_performance_is_the_percent_change_over_the_window():
    rows = bars([100.0] * 10 + [110.0])
    assert ind.performance(rows, 5) == pytest.approx(10.0)


def test_performance_over_a_window_longer_than_the_series_is_none():
    assert ind.performance(bars([1.0] * 10), 252) is None
    assert ind.performance(bars([1.0] * 10), 0) is None


def test_all_time_performance_runs_from_the_first_stored_session():
    assert ind.performance_all_time(bars([50.0, 75.0, 100.0])) == pytest.approx(100.0)
    assert ind.performance_all_time(bars([50.0])) is None


def test_the_windows_are_the_session_counts_the_tab_shows():
    assert ind.WINDOWS == {"1W": 5, "1M": 21, "3M": 63, "1Y": 252, "5Y": 1260}


# ── risk ─────────────────────────────────────────────────────────────────────

def test_volatility_is_the_session_range_as_a_percent_of_its_low():
    rows = [Bar(close=105, high=110, low=100)]
    assert ind.volatility(rows, 1) == pytest.approx(10.0)


def test_volatility_over_several_sessions_is_their_average_range():
    rows = [Bar(close=105, high=110, low=100), Bar(close=102, high=104, low=100)]
    assert ind.volatility(rows, 2) == pytest.approx((10.0 + 4.0) / 2)


def test_volatility_needs_a_high_and_a_low_on_every_session_of_the_window():
    assert ind.volatility([Bar(close=105, high=110, low=100), Bar(close=102)], 2) is None


def test_a_name_that_moves_with_the_market_has_a_beta_of_one():
    market = bars([100 * 1.01 ** i for i in range(300)])
    assert ind.beta(market, market, 250) == pytest.approx(1.0)


def test_a_name_that_moves_twice_as_hard_has_a_beta_of_two():
    market_closes = [100.0]
    mine_closes = [100.0]
    for i in range(300):
        step = 0.01 if i % 2 else -0.005
        market_closes.append(market_closes[-1] * (1 + step))
        mine_closes.append(mine_closes[-1] * (1 + 2 * step))
    assert ind.beta(bars(mine_closes), bars(market_closes), 250) == pytest.approx(2.0, rel=0.02)


def test_beta_against_a_market_that_never_moves_is_none():
    assert ind.beta(bars([100 + i for i in range(300)]), bars([100.0] * 300), 250) is None


def test_beta_over_a_window_longer_than_the_series_is_none():
    assert ind.beta(bars([1.0] * 10), bars([1.0] * 10), 1260) is None


def test_a_session_at_zero_never_makes_an_infinite_return():
    values = ind.returns(bars([10.0, 0.0, 5.0]))
    assert all(math.isfinite(v) for v in values)
