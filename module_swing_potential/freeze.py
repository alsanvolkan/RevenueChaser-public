"""Swing board -> forecast ledger rows (#128).

``freeze_payload`` is the last step of a Swing Fetch (``app/runners.run_swing``): it applies the
shared freeze rule (``core.ledger.freeze_board``) to the board just built and appends the shown
candidates to ``forecast_ledger``. The mapping lives here, in the module, not in ``core/``.
"""
from __future__ import annotations

from RevenueChaser.core import ledger
from RevenueChaser.module_swing_potential import config
from RevenueChaser.module_swing_potential import settings as swing_settings

SOURCE = "swing"
LABEL = "Swing"

# The thresholds the board scores with. ``model_version`` hashes exactly these; ``OHLC_TTL_DAYS``
# and the options-flow flag change how fresh or how annotated the board is, not which picks it shows.
# A settings override (#384 SW-S4) replaces its key here, so a saved value versions the ledger rows it produced.
_HASHED = (
    "MIN_PRICE", "MIN_DOLLAR_VOLUME",
    "RS_MIN", "ABOVE_52W_LOW", "WITHIN_52W_HIGH", "VOL_CONFIRM", "VOL_WINDOW", "VOL_AVG_DAYS", "BREAKOUT_HIGH_PCT",
    "MOVE_MIN",
    "ADR_MIN", "W_12_1", "W_PROX", "W_CONS", "CONSISTENCY_LOOKBACK", "STOP_ATR", "TARGET_R",
    "ATR_PERIOD", "Q_WEIGHTS", "Q_RULE_TOTALS",
)


def effective_config(payload: dict | None = None) -> dict:
    """Config's thresholds with only a user's stored overrides on top (#384 SW-S4).

    The overrides are applied to config's own values rather than read as the effective settings, so an
    unconfigured board hashes exactly the dict it always did (``4+ab881cb0``). A store read that fails is
    no overrides, never an exception: the ledger still names a model. Given a ``payload`` that carries its
    run's settings, the overrides are that run's, not the store's now (Swing review finding 2, PR 6).
    """
    cfg = {name: getattr(config, name) for name in _HASHED}
    run = swing_settings.from_payload((payload or {}).get("settings"))
    overrides = dict(run.override_values) if run is not None else swing_settings.stored_overrides()
    for name, value in overrides.items():
        if name in cfg:
            cfg[name] = value
    return cfg


def model_version(payload: dict | None = None) -> str:
    return ledger.model_version(config.SELECTION_VERSION, effective_config(payload))


def picks(candidates: list[dict]) -> list[dict]:
    """The board's shown candidates as ledger picks. ``rank`` is the 1-based position on the board."""
    return [{
        "subject": c["ticker"],
        "rank": position,
        "score": c.get("quality"),
        "bucket": c.get("bucket"),                                                      # methods passed (#384 SW-S1)
        "market_cap": c.get("market_cap"),
        "sector": c.get("sector"),
        "shown_price": c.get("price"),
        "stop": c.get("stop"),
        "target": c.get("target"),
    } for position, c in enumerate(candidates, start=1)]


def freeze_payload(payload: dict, *, run_id: str | None = None, now=None) -> ledger.Outcome:
    """Freeze a finished Swing payload when the rule allows it; the outcome is also a job note.

    SPY's bar must be one complete session D (D01's, else the cache's: ``ledger.spy_anchor_problem``), and so must
    each frozen candidate's own last cached bar: a halted name must not carry an old price under D.
    """
    return ledger.freeze_board(
        SOURCE, LABEL, payload, rows_key="candidates", to_rows=picks, model_version=model_version(payload),
        run_id=run_id, now=now, anchor=ledger.spy_anchor_problem,
        bar_gaps=ledger.ohlc_gaps)
