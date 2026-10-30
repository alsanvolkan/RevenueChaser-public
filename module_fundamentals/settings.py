"""The one resolver for the Fundamentals inputs a user can change (PR 2 of the A+B universe plan).

Before PR 2 the 7-A prefilter's floors were fixed in ``config``; now they are settings like Swing's (#384 SW-S4),
with the same defaults ($5, $250K), together with the board's type filter (``OPERATING_ONLY``). A default is read
from ``module_fundamentals/config.py`` at call time and never copied into the store; only a value a user saved lives
in ``board_settings``, and *Reset* deletes it. A store that cannot be read is the defaults, never an exception.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from RevenueChaser.core import board_settings

from . import config

BOARD = "module_fundamentals"


@dataclass(frozen=True)
class Effective:
    """The inputs one run screens with (settings override, else config default)."""

    min_price: float
    min_dollar_volume: float
    operating_only: bool
    override_values: tuple = ()           # ((KEY, stored value), ...): what the ledger's model version hashes

    def prefilter_cfg(self) -> SimpleNamespace:
        """The ``cfg`` ``core.screen_common.prefilter`` reads (it knows only these two names)."""
        return SimpleNamespace(MIN_PRICE=self.min_price, MIN_DOLLAR_VOLUME=self.min_dollar_volume)


def _stored() -> dict:
    try:
        return board_settings.values(BOARD)
    except Exception:  # noqa: BLE001 - a store read that fails is the defaults, never a failed scan
        return {}


def effective() -> Effective:
    vals = _stored()
    v = vals.get("values") or {}
    over = vals.get("overridden") or ()
    return Effective(
        min_price=float(v.get("MIN_PRICE", config.MIN_PRICE)),
        min_dollar_volume=float(v.get("MIN_DOLLAR_VOLUME", config.MIN_DOLLAR_VOLUME)),
        operating_only=bool(int(v.get("OPERATING_ONLY", config.OPERATING_ONLY))),
        override_values=tuple((k, v[k]) for k in over if k in v),
    )


def stored_overrides() -> dict:
    """Only the keys a user overrode (never a default): what the forecast ledger's model version hashes."""
    return dict(effective().override_values)
