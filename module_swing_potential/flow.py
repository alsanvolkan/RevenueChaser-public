"""Swing options flow — manual-only by default (constitutional bypass).

UW flow is manual-review only. ``poll_flow`` is provided for an explicit manual
trigger; it is NOT called by the automated pipeline when ``MANUAL_FLOW_ONLY`` is
True (the default). Returns a small, JSON-safe summary.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def poll_flow(uw, ticker: str) -> dict:
    """Manual flow summary for one ticker (flow-recent + spot-exposures)."""
    if uw is None or not getattr(uw, "active", False):
        return {"status": "not_scanned"}
    try:
        flow = uw.flow_recent(ticker) or []
        inst_net = None
        spot = None
        if flow:
            agg = uw.aggregate_flow(ticker, flow)
            inst_net = round(agg.get("inst_net", 0.0), 2)
        try:
            expo = uw.spot_exposures_by_strike(ticker) or []
            spot = {"strikes": len(expo)}
        except Exception as exc:  # pragma: no cover - optional endpoint
            logger.debug("spot_exposures %s: %s", ticker, exc)
        return {"status": "scanned", "inst_net": inst_net, "spot_strikes": spot["strikes"] if spot else None}
    except Exception as exc:  # pragma: no cover - network
        logger.debug("poll_flow %s: %s", ticker, exc)
        return {"status": "error"}
