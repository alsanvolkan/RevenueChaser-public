"""Volatility metrics carried from the uw-earnings-vol-scan skill.

Pure, dependency-free helpers R4 uses to score vol richness without trusting a
single stale screener field:

- ``yang_zhang`` — realized volatility over the trailing ``window`` trading
  days, annualised by sqrt(251) (Juneteenth made the US trading year 251 days).
- ``term_structure_slope`` — per-day ATM-IV slope from the nearest expiration
  out to ``horizon`` calendar days. Backwardation (front IV rich) is the
  calendar-seller's edge and the premium buyer's drag; contango is the reverse.
- ``term_structure_curve`` — normalise UW ``/volatility/term-structure`` rows
  into ``[(dte, iv), ...]``, expired entries dropped, duplicates collapsed.

Calibration note: the IV/RV pair the skill's 1.25 richness threshold assumes is
today's ATM IV vs YZ-RV30 measured over the trailing 30 trading days — see the
skill's "Why IV and RV are compared with no lag" section. R4 consumes the same
construction (YZ RV fed as ``realized_volatility``) so the threshold transfers.
"""
from __future__ import annotations

import math

# Juneteenth became a full NYSE closure in 2022; 251 is the measured trading
# year (see the skill; never "correct" back to 252 for R4's numbers).
TRADING_PERIODS_PER_YEAR = 251


def clean_ohlc(rows, as_of=None) -> list[dict]:
    """Chronological OHLC bars usable by ``yang_zhang``.

    Coerces string/number OHLC to floats and drops bars whose date is missing,
    whose prices are non-positive (logs undefined) or non-numeric, or — when
    ``as_of`` is given — dated on/after ``as_of`` (a still-forming bar must not
    feed realized vol). Returns [] when nothing survives.

    Rows are first reduced to one regular-session bar per date, oldest first
    (UW sends pre-market / regular / after-hours rows per day, sometimes
    newest-first — review X2), so stored or raw rows are both safe here.
    """
    from RevenueChaser.core.providers.ohlc import regular_session_bars

    bars = []
    for c in regular_session_bars(rows):
        if not isinstance(c, dict):
            continue
        d = str(c.get("date") or c.get("datetime") or "")[:10]
        if not d:
            continue  # no date: recency and ordering are unknowable
        if as_of is not None:
            try:
                if d >= as_of.isoformat():
                    continue
            except (TypeError, ValueError):
                continue
        try:
            o = float(c["open"])
            h = float(c["high"])
            l = float(c["low"])
            cl = float(c["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, l, cl) <= 0:
            continue
        bars.append({"open": o, "high": h, "low": l, "close": cl})
    return bars


def yang_zhang(bars, window: int = 30):
    """Annualised Yang-Zhang realized volatility over the last ``window`` returns.

    ``bars`` is a chronological list of dicts with open/high/low/close floats and
    needs ``window + 1`` of them: the extra bar supplies close_{i-1} for the
    first return. Returns None when the inputs cannot produce a usable number,
    which callers must treat as "no realized-vol read" rather than as zero.
    """
    if not bars or len(bars) < window + 1:
        return None
    w = bars[-(window + 1):]

    close_sq = open_sq = rs_sum = 0.0
    for i in range(1, window + 1):
        o, h, l, c = w[i]["open"], w[i]["high"], w[i]["low"], w[i]["close"]
        prev_c = w[i - 1]["close"]
        if min(o, h, l, c, prev_c) <= 0:
            return None
        log_ho = math.log(h / o)
        log_lo = math.log(l / o)
        log_co = math.log(c / o)
        log_oc = math.log(o / prev_c)
        log_cc = math.log(c / prev_c)
        open_sq += log_oc ** 2
        close_sq += log_cc ** 2
        rs_sum += log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)

    n = window
    close_vol = close_sq / (n - 1)
    open_vol = open_sq / (n - 1)
    rs_vol = rs_sum / (n - 1)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    variance = open_vol + k * close_vol + (1 - k) * rs_vol
    if variance <= 0:
        return None
    return math.sqrt(variance * TRADING_PERIODS_PER_YEAR)


def _interp_flat(curve, x):
    """Linear interpolation with flat extrapolation outside the endpoints."""
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return curve[-1][1]


def term_structure_curve(rows) -> list[tuple]:
    """Normalise UW term-structure rows -> [(dte, iv), ...] sorted ascending.

    Expired entries (dte <= 0) are dropped and duplicate DTEs keep the first,
    mirroring the skill so interpolation is unambiguous. Returns [] when fewer
    than two usable points survive.
    """
    curve = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            dte = int(float(r.get("dte")))
        except (TypeError, ValueError):
            continue
        if dte <= 0:
            continue
        try:
            iv = float(r.get("volatility"))
        except (TypeError, ValueError):
            continue
        if iv <= 0:
            continue
        curve.append((dte, iv))
    curve.sort(key=lambda p: p[0])
    curve = [p for i, p in enumerate(curve) if i == 0 or p[0] != curve[i - 1][0]]
    return curve if len(curve) >= 2 else []


def term_structure_slope(curve, horizon: int = 45):
    """Per-day ATM-IV slope from the nearest expiration out to ``horizon`` DTE.

    Returns None instead of extrapolating when the curve cannot span near ->
    horizon (nearest expiry at/past horizon, or nothing reaches horizon), or when
    fewer than two points exist. That is a deliberate "no slope" for R4, never an
    invented curve.
    """
    if not curve or len(curve) < 2:
        return None
    nearest_dte = curve[0][0]
    if nearest_dte >= horizon:
        return None
    if curve[-1][0] < horizon:
        return None
    iv_far = _interp_flat(curve, horizon)
    iv_near = curve[0][1]
    return (iv_far - iv_near) / (horizon - nearest_dte)
