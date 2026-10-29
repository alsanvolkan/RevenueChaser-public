"""Normalize UW ``/option-contracts`` rows into a per-(strike, call/put) chain.

The Iron Butterfly panel prices its four legs off the NBBO mid of each contract.
``option_symbol`` (e.g. ``SPXW260902P07630000``) encodes root + YYMMDD expiry +
P/C + 8-digit strike (3 implied decimals). When a strike has no NBBO quotes, a
Black-Scholes mid is modeled from the contract's own IV (r = 0) and flagged in
``price_source`` so modeled prices are never mistaken for live quotes.
"""
from __future__ import annotations

import math
from datetime import date as _date
from datetime import datetime

from RevenueChaser.core.calendar import NY, SESSION_CLOSE
from RevenueChaser.core.options import parse_option_symbol

from .config import to_float

__all__ = ["parse_option_symbol", "black_scholes_mid", "normalize_option_chain"]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_mid(spot: float, strike: float, years: float, iv: float, cp: str) -> float | None:
    """Theoretical mid (r = 0) for a European option; None when inputs invalid."""
    if iv is None or iv <= 0.0 or years <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return None
    if not (0.0 < iv < 5.0):
        return None
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    if cp == "C":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


YEAR_SECONDS = 365.0 * 86400.0
SESSION_SECONDS = 6.5 * 3600.0     # 09:30-16:00 New York
MIN_SECONDS_LEFT = 5 * 60.0        # a fetch at or after the close still leaves a sliver, not zero


def _years_left(expiry: str, dte: int | None, snapshot_time: str | None) -> float:
    """Time to expiry in years for the model price.

    A same-day contract (review N2) runs to 16:00 New York on its expiry: the time left after the fetch, floored at
    five minutes. Without a fetch time it gets one full session, the most it can have; a 0DTE contract used to get a
    whole calendar day and was priced like the next day's contract. Later expiries count calendar days as before.
    shortcut: early-close days (13:00) are treated as 16:00 closes, like core.calendar.session_close; upgrade when
    the calendar carries early closes.
    """
    if dte is not None and dte > 0:
        return dte / 365.0
    seconds = SESSION_SECONDS
    if snapshot_time:
        try:
            fetched = datetime.fromisoformat(str(snapshot_time).replace("Z", "+00:00"))
            close = datetime.combine(_date.fromisoformat(expiry), SESSION_CLOSE, tzinfo=NY)
            if fetched.tzinfo is not None:
                seconds = min(SESSION_SECONDS, (close - fetched).total_seconds())
        except ValueError:
            seconds = SESSION_SECONDS
    return max(seconds, MIN_SECONDS_LEFT) / YEAR_SECONDS


def normalize_option_chain(payload: dict | None, spot: float | None = None,
                           asof_date: str | None = None, snapshot_time: str | None = None) -> list[dict]:
    """Per-(strike, type) rows: bid/ask/mid/IV/Δ/Γ/Θ/ν + OI/volume.

    ``mid`` prefers an explicit mid, else ``(bid + ask) / 2``; when neither
    exists it is modeled from ``iv`` via Black-Scholes (``price_source`` =
    "modeled"). ``asof_date`` lets the DTE be derived for the model price; ``snapshot_time`` (the fetch's
    ``api_snapshot_time``) sets how much of a same-day contract's session is left.
    """
    if not payload:
        return []
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = data.get("data") or []
    rows = []
    for r in data:
        sym = parse_option_symbol(r.get("option_symbol") or "")
        if sym is None:
            continue
        bid = to_float(r.get("nbbo_bid"))
        ask = to_float(r.get("nbbo_ask"))
        mid = to_float(r.get("mid"))
        price_source = "quote"
        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        iv = to_float(r.get("implied_volatility"))
        if mid is None and iv is not None and spot is not None:
            dte = None
            if asof_date:
                try:
                    dte = (_date.fromisoformat(sym["expiry"]) - _date.fromisoformat(asof_date)).days
                except ValueError:
                    dte = None
            mid = black_scholes_mid(spot, sym["strike"], _years_left(sym["expiry"], dte, snapshot_time),
                                    iv, sym["type"])
            if mid is not None:
                price_source = "modeled"
        if mid is not None:
            mid = round(mid, 2)
        rows.append({
            "strike": sym["strike"],
            "type": sym["type"],
            "expiry": sym["expiry"],
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "price_source": price_source,
            "iv": iv,
            "delta": to_float(r.get("delta")),
            "gamma": to_float(r.get("gamma")),
            "theta": to_float(r.get("theta")),
            "vega": to_float(r.get("vega")),
            "open_interest": to_float(r.get("open_interest")),
            "volume": to_float(r.get("volume")),
        })
    return rows
