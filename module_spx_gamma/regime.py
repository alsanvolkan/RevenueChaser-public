"""The report's "five conditions that change the regime" (§2) and its Net Delta weekly trend (§15).

Both come from what the board already holds — the snapshot's own levels and the GEX history file — so neither adds
a provider call. Pinned to the same report as ``scenarios.py`` (``scenarios.REPORT_AS_OF``).

§2 is a watch list, not a forecast: each row is a threshold that, if it is crossed, invalidates the frame the rest
of the page describes. The report writes the conditions as "if X then Y", with every level taken from its own
tables, and so does this.

§15 is the weekly trend of the summed call and put delta across every expiry — the board stores it per day as
``net_dex`` in the GEX history, and the Friday closes are the report's own rows. Measured against the 30 Oct
report: its table gives 2026-10-23 +123,30 M$ and 2026-10-30 +125,97 M$, and the history holds 123,299,826.10 and
125,970,898.49 for those days, so the stored field is the report's measure.
"""
from __future__ import annotations

from .relations import _fmt_usd        # the dollar form the greek panels already print
from .scenarios import NEGATIVE_GAMMA, POSITIVE_GAMMA, scenario_bands

#: The report's own threshold for "Net DEX falls meaningfully": a quarter off today's reading (30 Oct §2 gives
#: +727,10 bn with a -181,78 bn move to 545,33 bn, which is exactly 25% and 75% of it).
DEX_DROP_SHARE = 0.25
#: How many weekly closes the trend shows when it has them.
TREND_WEEKS = 5


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def net_delta_trend(history: dict | None, asof=None, weeks: int = TREND_WEEKS) -> dict:
    """The last ``weeks`` Friday closes of ``net_dex``, oldest first, with what is missing stated.

    Only days before ``asof`` count, as the positioning deltas do, so a day's own row never appears in its own
    trend. ``have`` is how many of ``weeks`` the history could supply: the board keeps one row per rendered day
    and UW does not serve old days, so the table fills forward one Friday a week rather than being backfillable.
    """
    rows = []
    for day in sorted((history or {}).keys()):
        if asof is not None and str(day) >= str(asof):
            continue
        record = history[day] or {}
        if not record.get("is_friday_close"):
            continue
        value = _finite(record.get("net_dex"))
        if value is not None:
            rows.append({"date": day, "net_delta": value})
    rows = rows[-weeks:]
    first, last = (rows[0]["net_delta"], rows[-1]["net_delta"]) if rows else (None, None)
    return {
        "rows": rows,
        "have": len(rows),
        "want": weeks,
        # The report reads the window's direction from its ends, not from a fit.
        "direction": None if len(rows) < 2 else ("down" if last < first else "up" if last > first else "flat"),
    }


def change_conditions(snapshot: dict, history: dict | None = None, asof=None) -> list[dict]:
    """The thresholds that would end the frame the page describes, as the report's §2 writes them.

    Each row is ``{"trigger", "then"}``. A condition whose level the snapshot does not carry is left out rather
    than printed with a blank, so the list never implies a threshold the board cannot see.
    """
    levels = snapshot.get("levels") or {}
    spot = _finite(snapshot.get("current_level"))
    flip = _finite(levels.get("regime_threshold"))
    put_wall = _finite(levels.get("lower_support_band"))
    call_wall = _finite(levels.get("upper_resistance_band"))
    bands = scenario_bands(snapshot)
    regime = (bands or {}).get("regime")
    out: list[dict] = []

    if spot is not None and flip is not None:
        if regime == NEGATIVE_GAMMA:
            out.append({"trigger": f"A daily close ABOVE {flip:,.0f} (the gamma flip)",
                        "then": "back to positive gamma — acceleration is dampened, the upside frame opens"})
            target = f", {put_wall:,.0f} the next support" if put_wall is not None else ""
            out.append({"trigger": f"The close stays BELOW {flip:,.0f}",
                        "then": f"negative gamma continues, so a move keeps being amplified{target}"})
        elif regime == POSITIVE_GAMMA:
            out.append({"trigger": f"A daily close BELOW {flip:,.0f} (the gamma flip)",
                        "then": "into negative gamma — dealer hedging starts amplifying the move instead"})
            ceiling = f", {call_wall:,.0f} the wall above" if call_wall is not None else ""
            out.append({"trigger": f"The close stays ABOVE {flip:,.0f}",
                        "then": f"positive gamma continues, so moves keep being dampened{ceiling}"})

    vix = (snapshot.get("vix") or {})
    vix_spot = _finite(vix.get("vix"))
    if vix_spot is not None:
        out.append({"trigger": f"A marked jump from VIX {vix_spot:,.2f} together with term backwardation",
                    "then": "vol-regime stress — a downward move gets bigger than the levels alone suggest"})

    net_dex = _finite(snapshot.get("net_dex"))
    if net_dex is not None and net_dex != 0:
        moved = abs(net_dex) * DEX_DROP_SHARE
        floor = net_dex * (1 - DEX_DROP_SHARE)
        out.append({"trigger": (f"Net DEX falls through its {DEX_DROP_SHARE:.0%} threshold "
                                f"(a {_fmt_usd(-moved)} move, to below {_fmt_usd(floor)})"),
                    "then": "the dealer lean behind the current frame weakens meaningfully"})

    report_pin = snapshot.get("charm_pin_report") or {}       # F2: the report's front-expiry pin and its charm
    charm = _finite(report_pin.get("net_charm"))
    pin = _finite(report_pin.get("strike"))
    if charm is not None and charm != 0:
        way = "from minus to plus" if charm < 0 else "from plus to minus"
        at = f" at {pin:,.0f}" if pin is not None else ""
        out.append({"trigger": f"Charm changes sign ({way})",
                    "then": f"the pin's pull{at} reverses, so the close drifts the other way"})
    return out
