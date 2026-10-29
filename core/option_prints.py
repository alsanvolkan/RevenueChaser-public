"""The side and the package flag of one option print (a UW ``flow-recent`` row), the one rule both the options data
(``rcdata/build/options/rows.py``, ``option_print.side`` / ``package``) and the Earnings Radar lean
(``core/providers/unusual_whales.py`` ``aggregate_flow``, R5) read.

Side: UW's side tag (``tags``, ``docs/openapi.yaml`` "Option Trade") when the print has one, else the side of its
NBBO's midpoint the price is on: above the midpoint ``ask``, below it ``bid``, on it ``mid``. None without a tag and
without a quote whose bid is under its ask. **Default chosen, change if avo prefers.** The first real sample
(2026-10-28, SPX, AAPL and NVDA, 300 prints, every one tagged) found this nearer-side rule agreeing with UW's tag on
235 of the 241 prints with a quote, and the strict rule (at or above the ask ``ask``, at or under the bid ``bid``,
anything inside the spread ``mid``) on only 123 of the 300, so the strict rule is not the fallback. The alternatives
are UW's tag only (a print without one has no side) and the strict rule. The 59 prints without a quote were all
``exht``, so for those the tag is the only side. The print's ``price``, ``nbbo_bid`` and ``nbbo_ask`` stay stored, so
another rule can be read from them at any time.

Package: the print's OPRA trade condition (``upstream_condition_detail``) is a multi-leg (``ml*``, ``mesl``),
stock-tied (``tl*``, ``tasl``) or older spread, straddle, buy-write or combo code.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

SIDE_TAGS = {"ask_side": "ask", "bid_side": "bid", "mid_side": "mid"}
PACKAGE_CODES = frozenset({"mlet", "mlat", "mlct", "mlft", "mesl", "tlet", "tlat", "tlct", "tlft", "tasl",
                           "sprd", "stdl", "bwrt", "cmbo"})


def _decimal(value) -> Decimal | None:
    """Exact, so a price on the midpoint of 1.10 and 1.20 is on it (in floats 1.10 + 1.20 is 2.3000000000000003)."""
    try:
        d = None if value in (None, "") else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d is not None and d.is_finite() else None


def _texts(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def print_side(item: dict) -> str | None:
    """``ask``, ``bid`` or ``mid`` (see the module doc), or None."""
    for tag in _texts(item.get("tags")):
        if tag.lower() in SIDE_TAGS:
            return SIDE_TAGS[tag.lower()]
    price, bid, ask = (_decimal(item.get(k)) for k in ("price", "nbbo_bid", "nbbo_ask"))
    if price is None or bid is None or ask is None or not 0 <= bid < ask:
        return None
    twice_price, spread_sum = 2 * price, bid + ask
    return "ask" if twice_price > spread_sum else "bid" if twice_price < spread_sum else "mid"


def is_package(item: dict) -> bool | None:
    """True for a package's trade condition, False for any other condition, None when the print has none."""
    code = str(item.get("upstream_condition_detail") or "").strip().lower()
    return None if not code else code in PACKAGE_CODES
