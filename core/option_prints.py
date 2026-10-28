"""The side and the package flag of one option print (a UW ``flow-recent`` row), the one rule both the options data
(``rcdata/build/options/rows.py``, ``option_print.side`` / ``package``) and the Earnings Radar lean
(``core/providers/unusual_whales.py`` ``aggregate_flow``, R5) read.

Side: UW's side tag when the print has one (``tags``, ``docs/openapi.yaml`` "Option Trade"), else its price
against the NBBO: at or above the ask is ``ask``, at or under the bid ``bid``, between ``mid``. None without a tag and
without a quote whose bid is under its ask. Package: the print's OPRA trade condition (``upstream_condition_detail``)
is a multi-leg (``ml*``, ``mesl``), stock-tied (``tl*``, ``tasl``) or older spread, straddle, buy-write or combo code.
"""
from __future__ import annotations

SIDE_TAGS = {"ask_side": "ask", "bid_side": "bid", "mid_side": "mid"}
PACKAGE_CODES = frozenset({"mlet", "mlat", "mlct", "mlft", "mesl", "tlet", "tlat", "tlct", "tlft", "tasl",
                           "sprd", "stdl", "bwrt", "cmbo"})


def _float(value) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


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
    price, bid, ask = (_float(item.get(k)) for k in ("price", "nbbo_bid", "nbbo_ask"))
    if price is None or bid is None or ask is None or not 0 <= bid < ask:
        return None
    return "ask" if price >= ask else "bid" if price <= bid else "mid"


def is_package(item: dict) -> bool | None:
    """True for a package's trade condition, False for any other condition, None when the print has none."""
    code = str(item.get("upstream_condition_detail") or "").strip().lower()
    return None if not code else code in PACKAGE_CODES
