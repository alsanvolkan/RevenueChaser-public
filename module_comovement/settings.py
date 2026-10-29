"""The one resolver for every Co-movement input a user can change (issue #40).

Engine, returns and persistence take the values from an ``Effective`` and never
read ``config.*`` for these knobs themselves, so there is exactly one place where
"Settings override, else config default" and the short-window rules live.

Short windows (``window < config.SHORT_WINDOW``): ``MIN_OBS = MIN_PERIODS =
max(15, window // 2)`` for that run, whatever is stored; horizon is locked to 1;
the out-of-sample persistence test is skipped. Longer windows need at most
``MIN_OBS_MAX_SHARE`` of their sessions (119 of 126), never every one.
"""
from __future__ import annotations

from dataclasses import dataclass

from RevenueChaser.core import board_settings
from RevenueChaser.core import universe as universe_mod

from . import config

BOARD = "module_comovement"


@dataclass(frozen=True)
class Effective:
    window: int
    min_obs: int
    min_periods: int
    oos_holdout: int
    oos_min_obs: int
    winsor_k: float
    bench_stale_sessions: int
    floor: float
    default_window: int
    short_window: bool
    custom: bool
    overridden: tuple = ()

    @property
    def horizon_locked(self) -> bool:
        return self.short_window

    @property
    def oos_enabled(self) -> bool:
        return not self.short_window

    def payload(self) -> dict:
        """The ``settings`` block of the board payload (for the Part B UI)."""
        return {
            "window": self.window, "default_window": self.default_window,
            "window_options": list(config.WINDOW_OPTIONS),
            "min_obs": self.min_obs, "min_periods": self.min_periods,
            "oos_holdout": self.oos_holdout, "oos_min_obs": self.oos_min_obs,
            "winsor_k": self.winsor_k,
            "bench_stale_sessions": self.bench_stale_sessions,
            "floor": self.floor, "short_window": self.short_window,
            "horizon_locked": self.horizon_locked,
            "custom": self.custom, "overridden": list(self.overridden),
        }


def oos_min_obs_for(holdout: int) -> int:
    """Held-out returns a name needs: round(0.63 x holdout) (40 at 63)."""
    return int(round(config.OOS_MIN_SHARE * int(holdout)))


def short_note(window: int) -> str:
    return (f"Short window ({window} sessions): noisy, exploratory. Horizon is "
            "fixed at 1 day and the out-of-sample persistence test is not run.")


def effective(window: int | None = None) -> Effective:
    """Resolve the inputs for one run. ``window=None`` is the Settings default."""
    vals = board_settings.values(BOARD)
    v = vals["values"]
    default_window = int(v["WINDOW"])
    win = default_window if window is None else int(window)
    short = win < config.SHORT_WINDOW
    if short:
        min_obs = min(win, max(config.SHORT_MIN_FLOOR, win // 2))
        min_periods = min_obs
    else:
        min_obs = min(int(v["MIN_OBS"]), int(win * config.MIN_OBS_MAX_SHARE))
        min_periods = min(int(v["MIN_PERIODS"]), min_obs)
    holdout = int(v["OOS_HOLDOUT"])
    return Effective(
        window=win, min_obs=min_obs, min_periods=min_periods,
        oos_holdout=holdout, oos_min_obs=oos_min_obs_for(holdout),
        winsor_k=float(v["WINSOR_K"]),
        bench_stale_sessions=int(v["BENCH_STALE_SESSIONS"]),
        floor=universe_mod.DEFAULT_MC_MIN, default_window=default_window,
        short_window=short, custom=bool(vals["overridden"]),
        overridden=tuple(vals["overridden"]))
