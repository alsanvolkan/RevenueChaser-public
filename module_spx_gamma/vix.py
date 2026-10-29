"""The VIX index family of the report's VIX layer and its term-structure mode.

^VIX (spot), ^VIX9D, ^VIX3M, ^VIX6M, ^VIX1Y and ^VVIX (vol-of-vol). The closes come from the market store
(``store_reads.vix_family``, Cboe's files in ``market_series``; PR 3): the live yfinance read is gone. The
term-structure mode (contango vs backwardation) is derived from the spot vs 3M relationship, with the 9-day point
read for a front-end inversion (review N7).
"""
from __future__ import annotations

VIX_TICKERS = ["^VIX", "^VIX9D", "^VIX3M", "^VIX6M", "^VIX1Y", "^VVIX"]


def vix_term_mode(values: dict) -> str | None:
    """contango (VIX < VIX3M) vs backwardation (VIX > VIX3M); None when missing.

    A contango whose 9-day point sits above spot VIX is "front backwardation": the next week is priced dearer than
    the month although the 3M curve still slopes up, the first sign of near-term stress (review N7: ^VIX9D was
    fetched and shown but never read). Without a 9D close the reading is the old two-point one.
    """
    vix = values.get("^VIX")
    vix3m = values.get("^VIX3M")
    if vix is None or vix3m is None:
        return None
    if vix < vix3m:
        vix9d = values.get("^VIX9D")
        if vix9d is not None and vix9d > vix:
            return "front backwardation"
        return "contango"
    if vix > vix3m:
        return "backwardation"
    return "flat"
