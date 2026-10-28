"""The VIX index family of the report's VIX layer and its term-structure mode.

^VIX (spot), ^VIX9D, ^VIX3M, ^VIX6M, ^VIX1Y and ^VVIX (vol-of-vol). The closes come from the market store
(``store_reads.vix_family``, Cboe's files in ``market_series``; PR 3): the live yfinance read is gone. The
term-structure mode (contango vs backwardation) is derived from the spot vs 3M relationship.
"""
from __future__ import annotations

VIX_TICKERS = ["^VIX", "^VIX9D", "^VIX3M", "^VIX6M", "^VIX1Y", "^VVIX"]


def vix_term_mode(values: dict) -> str | None:
    """contango (VIX < VIX3M) vs backwardation (VIX > VIX3M); None when missing."""
    vix = values.get("^VIX")
    vix3m = values.get("^VIX3M")
    if vix is None or vix3m is None:
        return None
    if vix < vix3m:
        return "contango"
    if vix > vix3m:
        return "backwardation"
    return "flat"
