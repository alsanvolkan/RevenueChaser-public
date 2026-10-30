"""The one resolver for every Swing threshold a user can change (#384 SW-S4).

Every gate, the ranker and the execution levels take their numbers from an ``Effective`` and never read
``config.*`` for these knobs themselves, so "Settings override, else config default" lives here once. A default
is read from ``module_swing_potential/config.py`` at call time and is never copied into the store; only a value
a user saved lives in ``board_settings``, and *Reset* deletes it.

A store that cannot be read (a locked file, a pre-#40 store) is the defaults, never an exception: the board
still scans.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
from types import SimpleNamespace

from RevenueChaser.core import board_settings

from . import config

BOARD = "module_swing_potential"


@dataclass(frozen=True)
class Effective:
    """The thresholds one run scores with (settings override, else config default)."""

    min_price: float
    min_dollar_volume: float
    rs_min: float
    vol_confirm: float
    breakout_high_pct: float
    move_min: float
    adr_min: float
    w_prox: float
    w_cons: float
    stop_atr: float
    target_r: float
    custom: bool
    operating_only: bool = True           # the type filter (PR 2): operating companies only, else every A+B type
    overridden: tuple = ()
    override_values: tuple = ()           # ((KEY, stored value), ...): what the ledger's model version hashes

    def prefilter_cfg(self) -> SimpleNamespace:
        """The ``cfg`` ``core.screen_common.prefilter`` reads (it knows only these two names)."""
        return SimpleNamespace(MIN_PRICE=self.min_price, MIN_DOLLAR_VOLUME=self.min_dollar_volume)

    def payload(self) -> dict:
        """This run's settings, for the board payload (``from_payload`` reads them back)."""
        return {
            "min_price": self.min_price, "min_dollar_volume": self.min_dollar_volume,
            "rs_min": self.rs_min, "vol_confirm": self.vol_confirm,
            "breakout_high_pct": self.breakout_high_pct, "move_min": self.move_min,
            "adr_min": self.adr_min, "w_prox": self.w_prox, "w_cons": self.w_cons,
            "stop_atr": self.stop_atr, "target_r": self.target_r,
            "operating_only": self.operating_only,
            "custom": self.custom, "overridden": list(self.overridden),
            "overrides": dict(self.override_values),
        }


def from_payload(settings: dict | None) -> Effective | None:
    """The ``Effective`` a payload's ``settings`` (``Effective.payload``) recorded, or None for a payload written
    before the run carried its settings (Swing review finding 2, PR 6), or one that does not hold them all."""
    if not isinstance(settings, dict):
        return None
    try:
        numbers = {f.name: float(settings[f.name]) for f in fields(Effective)
                   if f.name not in ("custom", "operating_only", "overridden", "override_values")}
    except (KeyError, TypeError, ValueError):
        return None
    # A payload written before PR 2 carries no type filter: it screened operating companies only.
    return Effective(**numbers, custom=bool(settings.get("custom")),
                     operating_only=bool(settings.get("operating_only", True)),
                     overridden=tuple(settings.get("overridden") or ()),
                     override_values=tuple((settings.get("overrides") or {}).items()))


# The settings one Fetch resolved at its start (``pinned``): every ``effective()`` call inside it returns them, so a
# save during the Fetch cannot leave the prefilter, the payload and the score rows on different thresholds.
_PINNED: ContextVar[Effective | None] = ContextVar("swing_pinned_settings", default=None)


@contextmanager
def pinned(e: Effective):
    """Every ``effective()`` inside the block returns ``e`` (Swing review finding 2, PR 6)."""
    token = _PINNED.set(e)
    try:
        yield e
    finally:
        _PINNED.reset(token)


def _stored() -> dict:
    """``board_settings.values`` for this board, or an empty read when the store cannot answer."""
    try:
        return board_settings.values(BOARD)
    except Exception:  # noqa: BLE001 - a store read that fails is the defaults, never a failed scan
        return {}


def _num(values: dict, key: str, default) -> float:
    """The stored number for ``key``, else ``default`` (a value that is not a number is the default too)."""
    try:
        f = float(values.get(key, default))
    except (TypeError, ValueError):
        return float(default)
    return f if f == f else float(default)


def effective() -> Effective:
    """Resolve the thresholds for one run, from the store overlaid on the config defaults; inside ``pinned``,
    the run's own."""
    if (run := _PINNED.get()) is not None:
        return run
    vals = _stored()
    v = vals.get("values") or {}
    return Effective(
        min_price=_num(v, "MIN_PRICE", config.MIN_PRICE),
        min_dollar_volume=_num(v, "MIN_DOLLAR_VOLUME", config.MIN_DOLLAR_VOLUME),
        rs_min=_num(v, "RS_MIN", config.RS_MIN),
        vol_confirm=_num(v, "VOL_CONFIRM", config.VOL_CONFIRM),
        breakout_high_pct=_num(v, "BREAKOUT_HIGH_PCT", config.BREAKOUT_HIGH_PCT),
        move_min=_num(v, "MOVE_MIN", config.MOVE_MIN),
        adr_min=_num(v, "ADR_MIN", config.ADR_MIN),
        w_prox=_num(v, "W_PROX", config.W_PROX),
        w_cons=_num(v, "W_CONS", config.W_CONS),
        stop_atr=_num(v, "STOP_ATR", config.STOP_ATR),
        target_r=_num(v, "TARGET_R", config.TARGET_R),
        custom=bool(vals.get("overridden")),
        operating_only=bool(int(_num(v, "OPERATING_ONLY", config.OPERATING_ONLY))),
        overridden=tuple(vals.get("overridden") or ()),
        override_values=tuple((k, v[k]) for k in vals.get("overridden") or () if k in v),
    )


def stored_overrides() -> dict:
    """Only the keys a user overrode (never a default): what the forecast ledger's model version hashes."""
    vals = _stored()
    v = vals.get("values") or {}
    return {k: v[k] for k in vals.get("overridden") or () if k in v}
