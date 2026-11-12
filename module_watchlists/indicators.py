"""The watch-list columns a daily bar series can answer: performance, risk and the technical ratings (#786).

avo, 12 Nov 2026, on TradingView's Tech / MA / Os Rating: "turetemez misin?" — yes, and this is where. The method
is TradingView's published one: fifteen moving averages vote, eleven oscillators vote, each vote is buy (+1),
sell (-1) or neutral (0), a rating is the mean of its votes, and the technical rating is the mean of the two.
The bands are TradingView's:

    <= -0.5 strong sell · <= -0.1 sell · < 0.1 neutral · < 0.5 buy · >= 0.5 strong buy

**Our numbers will not match TradingView's exactly**, and avo said that is fine ("sorun degil", 12 Nov 15:30): the
bars come from our own prices store and a different session, and TradingView smooths some of these on its own
data. What is promised here is the method, not the digits.

Everything in this module is a pure function over a bar series — no store, no network, no clock — so it is read the
same way in a test and on the board. A series too short for an indicator gives ``None``, never a guess: a name
that listed last month must show "—" for its 5-year performance, not a number computed from six weeks.

A ``Bar`` is one complete session, oldest first, with ``close`` always set and ``high`` / ``low`` / ``volume``
set where the source has them.
"""
from __future__ import annotations

import math
from typing import NamedTuple, Sequence

# ── the bands ────────────────────────────────────────────────────────────────

STRONG_SELL, SELL, NEUTRAL, BUY, STRONG_BUY = "strong_sell", "sell", "neutral", "buy", "strong_buy"
#: The lower edge of each label, as TradingView draws them.
BANDS = ((0.5, STRONG_BUY), (0.1, BUY), (-0.1, NEUTRAL), (-0.5, SELL))

#: Sessions in a trading week, month, quarter and year — the windows the Performance tab uses. A year is 252
#: sessions, the usual count of US trading days.
WINDOWS = {"1W": 5, "1M": 21, "3M": 63, "1Y": 252, "5Y": 1260}

MA_PERIODS = (10, 20, 30, 50, 100, 200)


class Bar(NamedTuple):
    close: float
    high: float | None = None
    low: float | None = None
    volume: float | None = None


def label(score: float | None) -> str | None:
    """The word for a rating between -1 and 1."""
    if score is None:
        return None
    for edge, name in BANDS:
        if score >= edge:
            return name
    return STRONG_SELL


# ── small helpers ────────────────────────────────────────────────────────────

def _closes(bars: Sequence[Bar]) -> list[float]:
    return [b.close for b in bars]


def _vote(value: float | None, reference: float | None) -> int | None:
    """A moving average votes buy when the price is above it and sell when below."""
    if value is None or reference is None:
        return None
    if math.isclose(reference, value):
        return 0
    return 1 if reference > value else -1


def _mean(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> float | None:
    """Seeded with the first ``period`` values' mean, as every charting package does."""
    if period <= 0 or len(values) < period:
        return None
    k = 2 / (period + 1)
    out = sum(values[:period]) / period
    for value in values[period:]:
        out = value * k + out * (1 - k)
    return out


def wma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    weights = range(1, period + 1)
    return sum(v * w for v, w in zip(window, weights)) / sum(weights)


def hull(values: Sequence[float], period: int = 9) -> float | None:
    """Hull MA: ``WMA(2*WMA(n/2) - WMA(n), sqrt(n))``."""
    half, root = period // 2, max(1, int(round(math.sqrt(period))))
    if len(values) < period + root:
        return None
    raw = []
    for end in range(len(values) - root + 1, len(values) + 1):
        window = values[:end]
        fast, slow = wma(window, half), wma(window, period)
        if fast is None or slow is None:
            return None
        raw.append(2 * fast - slow)
    return wma(raw, root)


def vwma(bars: Sequence[Bar], period: int = 20) -> float | None:
    if len(bars) < period:
        return None
    window = bars[-period:]
    if any(b.volume is None for b in window):
        return None
    total = sum(b.volume for b in window)
    if total <= 0:
        return None
    return sum(b.close * b.volume for b in window) / total


def ichimoku_base(bars: Sequence[Bar], period: int = 26) -> float | None:
    """The Kijun-sen: the midpoint of the last ``period`` sessions' range."""
    if len(bars) < period:
        return None
    window = bars[-period:]
    highs = [b.high for b in window if b.high is not None]
    lows = [b.low for b in window if b.low is not None]
    if len(highs) < period or len(lows) < period:
        return None
    return (max(highs) + min(lows)) / 2


# ── oscillators ──────────────────────────────────────────────────────────────

def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(values) < period + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(values[1:period + 1], values[:period]):
        change = a - b
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    gain, loss = gains / period, losses / period
    for a, b in zip(values[period + 1:], values[period:]):
        change = a - b
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def momentum(values: Sequence[float], period: int = 10) -> float | None:
    if len(values) < period + 1:
        return None
    return values[-1] - values[-1 - period]


def cci(bars: Sequence[Bar], period: int = 20) -> float | None:
    if len(bars) < period or any(b.high is None or b.low is None for b in bars[-period:]):
        return None
    typical = [(b.high + b.low + b.close) / 3 for b in bars[-period:]]
    mean = sum(typical) / period
    deviation = sum(abs(t - mean) for t in typical) / period
    if deviation == 0:
        return 0.0
    return (typical[-1] - mean) / (0.015 * deviation)


def awesome(bars: Sequence[Bar]) -> float | None:
    """AO: SMA 5 minus SMA 34 of the session midpoints."""
    if len(bars) < 34 or any(b.high is None or b.low is None for b in bars[-34:]):
        return None
    mids = [(b.high + b.low) / 2 for b in bars]
    fast, slow = sma(mids, 5), sma(mids, 34)
    return None if fast is None or slow is None else fast - slow


def stochastic(bars: Sequence[Bar], period: int = 14, smooth: int = 3) -> float | None:
    """%K, smoothed over ``smooth`` sessions."""
    if len(bars) < period + smooth - 1 or any(b.high is None or b.low is None for b in bars[-(period + smooth):]):
        return None
    raw = []
    for end in range(len(bars) - smooth + 1, len(bars) + 1):
        window = bars[end - period:end]
        high, low = max(b.high for b in window), min(b.low for b in window)
        raw.append(50.0 if high == low else (window[-1].close - low) / (high - low) * 100)
    return sum(raw) / len(raw)


def stoch_rsi(values: Sequence[float], period: int = 14) -> float | None:
    """The stochastic of the RSI series."""
    if len(values) < period * 2 + 1:
        return None
    series = [rsi(values[:end], period) for end in range(len(values) - period + 1, len(values) + 1)]
    if any(r is None for r in series):
        return None
    high, low = max(series), min(series)
    return 50.0 if high == low else (series[-1] - low) / (high - low) * 100


def williams_r(bars: Sequence[Bar], period: int = 14) -> float | None:
    if len(bars) < period or any(b.high is None or b.low is None for b in bars[-period:]):
        return None
    window = bars[-period:]
    high, low = max(b.high for b in window), min(b.low for b in window)
    if high == low:
        return -50.0
    return (high - window[-1].close) / (high - low) * -100


def macd_histogram(values: Sequence[float]) -> float | None:
    """The MACD line (EMA 12 - EMA 26) against its EMA 9 signal; the vote is on their difference."""
    if len(values) < 26 + 9:
        return None
    line = []
    for end in range(len(values) - 9 + 1, len(values) + 1):
        fast, slow = ema(values[:end], 12), ema(values[:end], 26)
        if fast is None or slow is None:
            return None
        line.append(fast - slow)
    signal = ema(line, 9) if len(line) >= 9 else sum(line) / len(line)
    return None if signal is None else line[-1] - signal


def bull_bear_power(bars: Sequence[Bar], period: int = 13) -> float | None:
    """Bull power plus bear power against the EMA — positive when the session's range sits above it."""
    if len(bars) < period or bars[-1].high is None or bars[-1].low is None:
        return None
    base = ema(_closes(bars), period)
    if base is None:
        return None
    return (bars[-1].high - base) + (bars[-1].low - base)


def ultimate(bars: Sequence[Bar], short: int = 7, middle: int = 14, long: int = 28) -> float | None:
    if len(bars) < long + 1 or any(b.high is None or b.low is None for b in bars[-(long + 1):]):
        return None
    buying, ranges = [], []
    for previous, bar in zip(bars[-(long + 1):-1], bars[-long:]):
        true_low = min(bar.low, previous.close)
        true_high = max(bar.high, previous.close)
        buying.append(bar.close - true_low)
        ranges.append(true_high - true_low)

    def average(n: int) -> float | None:
        total = sum(ranges[-n:])
        return None if total == 0 else sum(buying[-n:]) / total

    a, b, c = average(short), average(middle), average(long)
    if a is None or b is None or c is None:
        return None
    return 100 * (4 * a + 2 * b + c) / 7


# ── the three ratings ────────────────────────────────────────────────────────

def ma_votes(bars: Sequence[Bar]) -> list[int]:
    """The fifteen moving-average votes TradingView counts: SMA and EMA over six periods, the Ichimoku base
    line, VWMA 20 and Hull MA 9. An average the series is too short for simply does not vote."""
    closes = _closes(bars)
    price = closes[-1] if closes else None
    values = [f(closes, period) for period in MA_PERIODS for f in (sma, ema)]
    values += [ichimoku_base(bars), vwma(bars, 20), hull(closes, 9)]
    return [v for v in (_vote(value, price) for value in values) if v is not None]


def os_votes(bars: Sequence[Bar]) -> list[int]:
    """The eleven oscillator votes, each on its own rule. An oscillator the series is too short for does not
    vote. The rules are TradingView's: an overbought reading alone is not a sell — it must also be turning."""
    closes = _closes(bars)
    votes: list[int] = []

    def add(value: float | None, buy: bool, sell: bool) -> None:
        if value is not None:
            votes.append(1 if buy else -1 if sell else 0)

    r = rsi(closes)
    add(r, r is not None and r < 30, r is not None and r > 70)

    k = stochastic(bars)
    add(k, k is not None and k < 20, k is not None and k > 80)

    c = cci(bars)
    add(c, c is not None and c < -100, c is not None and c > 100)

    a = awesome(bars)
    add(a, a is not None and a > 0, a is not None and a < 0)

    m = momentum(closes)
    add(m, m is not None and m > 0, m is not None and m < 0)

    h = macd_histogram(closes)
    add(h, h is not None and h > 0, h is not None and h < 0)

    sr = stoch_rsi(closes)
    add(sr, sr is not None and sr < 20, sr is not None and sr > 80)

    w = williams_r(bars)
    add(w, w is not None and w < -80, w is not None and w > -20)

    bb = bull_bear_power(bars)
    add(bb, bb is not None and bb > 0, bb is not None and bb < 0)

    u = ultimate(bars)
    add(u, u is not None and u > 70, u is not None and u < 30)

    # ADX is counted as the eleventh: it rates trend strength, never direction, so it never votes buy or sell.
    if len(bars) >= 28:
        votes.append(0)
    return votes


def ratings(bars: Sequence[Bar]) -> dict[str, float | str | None]:
    """The three ratings as the Technicals tab shows them: a score between -1 and 1 and its word."""
    ma, os = _mean(ma_votes(bars)), _mean(os_votes(bars))
    tech = None if ma is None and os is None else _mean_of(ma, os)
    return {
        "ma": ma, "ma_label": label(ma),
        "os": os, "os_label": label(os),
        "tech": tech, "tech_label": label(tech),
    }


def _mean_of(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


# ── performance and risk ─────────────────────────────────────────────────────

def performance(bars: Sequence[Bar], sessions: int) -> float | None:
    """The percent change over the last ``sessions`` complete sessions. ``None`` when the series is shorter —
    a name that listed last month has no 5-year number, and must not be given one."""
    if sessions <= 0 or len(bars) < sessions + 1:
        return None
    start, end = bars[-1 - sessions].close, bars[-1].close
    return None if start == 0 else (end - start) / start * 100


def performance_all_time(bars: Sequence[Bar]) -> float | None:
    if len(bars) < 2 or bars[0].close == 0:
        return None
    return (bars[-1].close - bars[0].close) / bars[0].close * 100


def performance_ytd(bars: Sequence[Bar], sessions_this_year: int) -> float | None:
    return performance(bars, sessions_this_year)


def volatility(bars: Sequence[Bar], sessions: int = 1) -> float | None:
    """The average session range as a percent of its low, over the last ``sessions`` sessions — TradingView's
    Volatility 1D / 1W / 1M. A session with no high or low does not count."""
    if sessions <= 0 or len(bars) < sessions:
        return None
    ranges = [(b.high - b.low) / b.low * 100
              for b in bars[-sessions:] if b.high is not None and b.low is not None and b.low > 0]
    return sum(ranges) / len(ranges) if len(ranges) == sessions else None


def returns(bars: Sequence[Bar]) -> list[float]:
    """Session-to-session simple returns; a session at or below zero is skipped rather than made infinite."""
    out = []
    for previous, bar in zip(bars[:-1], bars[1:]):
        if previous.close > 0:
            out.append((bar.close - previous.close) / previous.close)
    return out


def beta(bars: Sequence[Bar], market: Sequence[Bar], sessions: int) -> float | None:
    """The slope of the name's returns against the market's over the last ``sessions`` sessions. Both series must
    reach that far and be aligned session for session by the caller."""
    if sessions <= 1:
        return None
    mine, theirs = returns(bars)[-sessions:], returns(market)[-sessions:]
    if len(mine) < sessions or len(theirs) < sessions:
        return None
    mean_mine = sum(mine) / sessions
    mean_theirs = sum(theirs) / sessions
    variance = sum((t - mean_theirs) ** 2 for t in theirs)
    if variance == 0:
        return None
    covariance = sum((m - mean_mine) * (t - mean_theirs) for m, t in zip(mine, theirs))
    return covariance / variance
