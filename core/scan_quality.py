"""Honest accounting for the per-ticker screeners (Fundamentals, Swing; #205).

A scan used to fall back to a demo fixture when nothing scored, with no reason.
Now each dropped ticker records why, tickers served from a stale OHLC cache are
counted, and an empty scan says so on the board instead of showing fixture data.

#145: a Refresh (``no_fetch``) never reaches the network, so an empty store is its own
reason (``no_universe_stored``) that points at Fetch Universe, and a missing SPY series is
"no regime" (``None``), never a made-up RISK-ON.
"""
from __future__ import annotations

from collections import Counter

DROP_LABELS = {
    "no_edgar_annual": "no EDGAR annual data",
    "no_ohlc": "no cached price bars",
    "no_bars": "no stored daily price bars",
    "stale_bars": "no daily bar of the latest stored session",
    "refetch_failed": "price refetch failed and no cache",
    "short_bars": "fewer than 252 price bars",
    "no_price": "no usable price",
    "compute_error": "unexpected error while scoring",
}


def ohlc_drop_reason(df) -> str:
    """Why a ticker's OHLC frame cannot be scored, or ``""`` when it can."""
    if df is None or df.empty:
        return "refetch_failed" if getattr(df, "attrs", {}).get("fetch_failed") else "no_ohlc"
    if len(df) < 252:
        return "short_bars"
    return ""


def regime_parts(regime: str | None) -> tuple[str, str, str]:
    """``(css class, label, momentum phrase)`` for the board header; ``None`` is "unknown", not RISK-ON."""
    if regime == "RISK-ON":
        return "on", "RISK-ON", "momentum in its favor"
    if regime == "RISK-OFF":
        return "off", "RISK-OFF", "momentum against it"
    return "na", "—", "momentum unknown (no SPY trend)"


def count_drops(drops: list[str]) -> dict[str, int]:
    return dict(Counter(drops))


def no_data_message(reason: str, drops: dict[str, int] | None = None,
                    shortlisted: int = 0) -> str:
    """Plain-text explanation for an empty scan (the board escapes it)."""
    drops = drops or {}
    if reason == "no_universe":
        return "No data: the universe is empty (no stored universe and the screener returned nothing)."
    if reason == "no_universe_stored":
        return ("No stored universe yet. Press Fetch Universe in the toolbar to load it once; "
                "Refresh never goes to the network.")
    if reason == "universe_loading":
        return "Loading universe… the board fills in when the universe job finishes."
    if reason == "no_scan_stored":
        return ("No stored scan yet. Press Fetch to score the stored universe; "
                "Refresh shows the last Fetch and never re-scores.")
    parts = [f"{n} {DROP_LABELS.get(k, k)}" for k, n in sorted(drops.items(), key=lambda kv: -kv[1])]
    failed = sum(drops.values())
    return (f"No data: none of the {shortlisted or failed} shortlisted tickers could be scored"
            + (f" ({'; '.join(parts)})." if parts else "."))


def no_data_hint(reason: str) -> str:
    """What to do next, shown after the message; empty when the message already says it."""
    if reason in ("no_universe_stored", "universe_loading", "no_scan_stored"):
        return ""
    return ("Run Fetch again once the price source is reachable; "
            "the demo fixture is not shown for real runs.")


def no_data_payload(base: dict, reason: str, drops: dict[str, int] | None = None,
                    shortlisted: int = 0) -> dict:
    """``base`` is a build_payload result; mark it as an explicit empty state."""
    out = dict(base)
    out["is_fixture"] = False
    out["no_data"] = True
    out["no_data_reason"] = reason
    out["no_data_message"] = no_data_message(reason, drops, shortlisted)
    return out
