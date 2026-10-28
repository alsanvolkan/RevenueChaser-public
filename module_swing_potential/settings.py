"""The one resolver for every Swing threshold a user can change (#384 SW-S4).

Every gate, the ranker and the execution levels take their numbers from an ``Effective`` and never read
``config.*`` for these knobs themselves, so "Settings override, else config default" lives here once. A default
is read from ``module_swing_potential/config.py`` at call time and is never copied into the store; only a value
a user saved lives in ``board_settings``, and *Reset* deletes it.

A store that cannot be read (a locked file, a pre-#40 store) is the defaults, never an exception: the board
still scans.

One Fetch scores with one read (Fable review of Swing, 28 Oct 2026, finding 2): ``run_scan`` resolves the settings
once and pins them for the run (``pinned``), so the prefilter, the gates, the stored rows and the payload agree even
when a value is saved mid-Fetch; the payload keeps them (``Effective.payload``), and the card and the forecast
ledger read them back from there (``from_payload``) rather than from the store at render or freeze time.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
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
    overridden: tuple = ()
    overrides: tuple = ()          # (config name, stored value) of each override, as read: the ledger hashes these

    def prefilter_cfg(self) -> SimpleNamespace:
        """The ``cfg`` ``core.screen_common.prefilter`` reads (it knows only these two names)."""
        return SimpleNamespace(MIN_PRICE=self.min_price, MIN_DOLLAR_VOLUME=self.min_dollar_volume)

    def payload(self) -> dict:
        """This run's settings, for the board payload / the Settings card."""
        return {
            "min_price": self.min_price, "min_dollar_volume": self.min_dollar_volume,
            "rs_min": self.rs_min, "vol_confirm": self.vol_confirm,
            "breakout_high_pct": self.breakout_high_pct, "move_min": self.move_min,
            "adr_min": self.adr_min, "w_prox": self.w_prox, "w_cons": self.w_cons,
            "stop_atr": self.stop_atr, "target_r": self.target_r,
            "custom": self.custom, "overridden": list(self.overridden), "overrides": dict(self.overrides),
        }


_FIELDS = ("min_price", "min_dollar_volume", "rs_min", "vol_confirm", "breakout_high_pct", "move_min", "adr_min",
           "w_prox", "w_cons", "stop_atr", "target_r")
_RUN: ContextVar[Effective | None] = ContextVar("swing_run_settings", default=None)


@contextmanager
def pinned(e: Effective):
    """``effective()`` and ``stored_overrides()`` answer ``e`` inside the block: one Fetch, one read of the store."""
    token = _RUN.set(e)
    try:
        yield e
    finally:
        _RUN.reset(token)


def from_payload(settings: dict | None) -> Effective | None:
    """The ``Effective`` a payload's ``settings`` recorded, or ``None`` for a payload from before it kept them."""
    if not isinstance(settings, dict) or any(settings.get(f) is None for f in _FIELDS):
        return None
    overrides = settings.get("overrides") or {}
    return Effective(**{f: float(settings[f]) for f in _FIELDS}, custom=bool(settings.get("custom")),
                     overridden=tuple(settings.get("overridden") or ()), overrides=tuple(overrides.items()))


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
    """Resolve the thresholds for one run, from the store overlaid on the config defaults (the pinned run's, inside
    ``pinned``)."""
    run = _RUN.get()
    if run is not None:
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
        overridden=tuple(vals.get("overridden") or ()),
        overrides=tuple((k, v[k]) for k in vals.get("overridden") or () if k in v),
    )


def stored_overrides() -> dict:
    """Only the keys a user overrode (never a default): what the forecast ledger's model version hashes."""
    run = _RUN.get()
    if run is not None:
        return dict(run.overrides)
    vals = _stored()
    v = vals.get("values") or {}
    return {k: v[k] for k in vals.get("overridden") or () if k in v}
