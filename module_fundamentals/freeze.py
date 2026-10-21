"""Fundamentals board -> forecast ledger rows (#128).

``freeze_payload`` is the last step of a Fundamentals Fetch (``app/runners.run_fundamentals``): it
applies the shared freeze rule (``core.ledger.freeze_board``) to the board just built and appends the
shown candidates to ``forecast_ledger``. The mapping lives here, in the module, not in ``core/``.
"""
from __future__ import annotations

from RevenueChaser.core import ledger
from RevenueChaser.module_fundamentals import config

SOURCE = "fundamentals"
LABEL = "Fundamentals"

# The thresholds the board scores with; ``model_version`` hashes exactly these.
# (Per-board setting overrides, #188, join this dict once the board reads them.)
_HASHED = (
    "MIN_PRICE", "MIN_DOLLAR_VOLUME",
    "RO40_MIN", "GROWTH_MIN", "ROIC_MIN", "GM_MIN", "ND_EBITDA_MAX",
    "DIST_52W_MAX", "RS_6M_MIN", "ABOVE_200D", "DISTORTED_GROWTH", "DISTORTED_REV_MIN", "TAX_RATE",
    "EXCLUDED_SECTORS", "RO40_BARRIER",     # #386 F1 data-health gates
)


def effective_config() -> dict:
    return {name: getattr(config, name) for name in _HASHED}


def model_version() -> str:
    return ledger.model_version(config.SELECTION_VERSION, effective_config())


def picks(candidates: list[dict]) -> list[dict]:
    """The board's shown candidates as ledger picks. ``rank`` is the 1-based position on the board."""
    return [{
        "subject": c["ticker"],
        "rank": position,
        "score": c.get("ro40"),
        "bucket": c.get("bucket"),     # #386 F2: the rules passed ("rule_of_40+quality+momentum"), not a 1-3 count
        "market_cap": c.get("market_cap"),
        "sector": c.get("sector"),
        "shown_price": c.get("price"),
        "stop": None,
        "target": None,
    } for position, c in enumerate(candidates, start=1)]


def freeze_payload(payload: dict, *, run_id: str | None = None, now=None) -> ledger.Outcome:
    """Freeze a finished Fundamentals payload when the rule allows it; the outcome is also a job note.

    The board's price comes from the shared daily-bar cache, so the anchor and the per-candidate
    check are the same as Swing's: SPY and each frozen name must end on one complete session D.
    """
    return ledger.freeze_board(
        SOURCE, LABEL, payload, rows_key="candidates", to_rows=picks, model_version=model_version(),
        run_id=run_id, now=now, anchor=ledger.spy_anchor_problem,
        bar_gaps=ledger.ohlc_gaps)
