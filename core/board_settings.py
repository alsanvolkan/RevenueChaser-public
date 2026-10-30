"""Per-board editable settings (issue #40) — one registry, one store table.

``board_settings(board, key, value, updated_at)`` in ``raw.duckdb`` holds only the
values a user **overrode**. A default is never copied into the table: it is read
from the board's config module at call time, so changing a default in config (or
pressing *Reset*, which deletes the rows) is always enough to move a board back.

``SPEC`` names every editable key of a board with its kind, its config default
and its bounds. Cross-rules (``MIN_PERIODS <= MIN_OBS``, window + holdout fits the
stored history, ...) are a callable per board so they see the values as they
would be *after* a save. ``validate`` returns one message per broken rule, each
naming that rule; nothing is written until the whole change set is valid.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import rawstore

TABLE = "board_settings"
# DuckDB commits a DELETE and an upsert of the same row side by side (no conflict is raised), and each write's
# change capture reads its own snapshot, so a Save and a Reset that overlap would leave board_settings_changes
# out of step with the table (#306 review S1). save() and reset() are the only writers and the dashboard the only
# process that writes the store, so an in-process lock serializes them.
_WRITE_LOCK = threading.Lock()


class SettingsError(ValueError):
    """A change set that broke one or more rules (``.messages`` names each)."""

    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages))
        self.messages = list(messages)


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str                                   # "int" | "float"
    default: Callable[[], float]                # read from config at call time
    lo: float | None = None
    hi: float | None = None
    choices: Callable[[], tuple] | None = None  # closed set (overrides lo/hi)
    label: str = ""


def _comov_cfg():
    from RevenueChaser.module_comovement import config
    return config


def stored_sessions() -> int:
    """Trading sessions of SPY that Co-movement reads (read-only): D01 over its 2y span since the #379 D01 switch
    (``module_comovement.ohlc.sessions_held``); 504 when the store has none (a fresh checkout) or cannot be read."""
    try:
        from RevenueChaser.module_comovement import ohlc
        n = ohlc.sessions_held("SPY")
    except Exception:  # noqa: BLE001 - table absent on a fresh store
        n = 0
    return n if n > 0 else 504


def _comov_rules(v: dict) -> list[str]:
    """Cross-rules for Co-movement, over the values as they would be saved."""
    cfg = _comov_cfg()
    out: list[str] = []
    window = int(v["WINDOW"])
    # Below SHORT_WINDOW the run scales MIN_OBS / MIN_PERIODS itself and skips
    # the lagged test, so the stored values only need to be sane on their own.
    cap = max(window, cfg.SHORT_WINDOW)
    if not v["MIN_OBS"] <= cap:
        out.append(f"MIN_OBS must be between {cfg.MIN_OBS_LO} and WINDOW ({cap}); "
                   f"got {int(v['MIN_OBS'])}")
    if not v["MIN_PERIODS"] <= v["MIN_OBS"]:
        out.append(f"MIN_PERIODS must be between {cfg.MIN_PERIODS_LO} and MIN_OBS "
                   f"({int(v['MIN_OBS'])}); got {int(v['MIN_PERIODS'])}")
    if window >= cfg.SHORT_WINDOW:
        avail = stored_sessions()
        if window + v["OOS_HOLDOUT"] > avail:
            out.append(f"WINDOW + OOS_HOLDOUT must not exceed the {avail} stored "
                       f"sessions; got {window} + {int(v['OOS_HOLDOUT'])}")
    return out


def _comov_specs() -> dict[str, Setting]:
    cfg = _comov_cfg()
    return {
        "WINDOW": Setting("WINDOW", "int", lambda: _comov_cfg().WINDOW,
                          choices=lambda: tuple(_comov_cfg().WINDOW_OPTIONS),
                          label="Default window (sessions)"),
        "MIN_OBS": Setting("MIN_OBS", "int", lambda: _comov_cfg().MIN_OBS,
                           lo=cfg.MIN_OBS_LO, label="Minimum observations"),
        "OOS_HOLDOUT": Setting("OOS_HOLDOUT", "int", lambda: _comov_cfg().OOS_HOLDOUT,
                               lo=cfg.OOS_HOLDOUT_LO, hi=cfg.OOS_HOLDOUT_HI,
                               label="Out-of-sample holdout (sessions)"),
        "WINSOR_K": Setting("WINSOR_K", "float", lambda: _comov_cfg().WINSOR_K,
                            lo=cfg.WINSOR_K_LO, hi=cfg.WINSOR_K_HI,
                            label="Winsorize at (sigma)"),
        "MIN_PERIODS": Setting("MIN_PERIODS", "int", lambda: _comov_cfg().MIN_PERIODS,
                               lo=cfg.MIN_PERIODS_LO, label="Pairwise minimum overlap"),
        "BENCH_STALE_SESSIONS": Setting(
            "BENCH_STALE_SESSIONS", "int", lambda: _comov_cfg().BENCH_STALE_SESSIONS,
            lo=cfg.BENCH_STALE_LO, hi=cfg.BENCH_STALE_HI, label="Benchmark lag (sessions)"),
        "OPERATING_ONLY": _operating_only(lambda: _comov_cfg().OPERATING_ONLY),
    }


TYPE_FILTER_CHOICES = (0, 1)


def _operating_only(default: Callable[[], int]) -> Setting:
    """A board's type filter (PR 2): 1 screens operating companies only, 0 every type of Tiers A and B."""
    return Setting("OPERATING_ONLY", "int", default, choices=lambda: TYPE_FILTER_CHOICES,
                   label="Security types: 1 = operating companies only, 0 = every type (funds, SPACs, ...)")


# board -> (spec factory, cross-rule). The factory runs at call time so bounds
# come from the board's config, never from a copy made at import.
def _swing_cfg():
    from RevenueChaser.module_swing_potential import config
    return config


def _swing_specs() -> dict[str, Setting]:
    """Swing's editable thresholds (#384 SW-S4): the numeric screen/score knobs of its config.

    Structural windows (``ATR_PERIOD``, ``CONSISTENCY_LOOKBACK``, the OHLC TTL) stay in config: they change
    what the board measures, not which names pass. ``ABOVE_52W_LOW``/``WITHIN_52W_HIGH`` are the Minervini
    template's own numbers and stay config-only too.
    """
    cfg = _swing_cfg()

    def default(name: str):
        return lambda: getattr(_swing_cfg(), name)

    return {
        "MIN_PRICE": Setting("MIN_PRICE", "float", default("MIN_PRICE"),
                             lo=cfg.MIN_PRICE_LO, hi=cfg.MIN_PRICE_HI,
                             label="Minimum price ($)"),
        "MIN_DOLLAR_VOLUME": Setting("MIN_DOLLAR_VOLUME", "float", default("MIN_DOLLAR_VOLUME"),
                                     lo=cfg.MIN_DOLLAR_VOLUME_LO, hi=cfg.MIN_DOLLAR_VOLUME_HI,
                                     label="Minimum daily dollar volume ($)"),
        "RS_MIN": Setting("RS_MIN", "float", default("RS_MIN"),
                          lo=cfg.RS_MIN_LO, hi=cfg.RS_MIN_HI,
                          label="Relative strength rating, ranks only: percentile among the scanned names (0-100)"),
        "VOL_CONFIRM": Setting("VOL_CONFIRM", "float", default("VOL_CONFIRM"),
                               lo=cfg.VOL_CONFIRM_LO, hi=cfg.VOL_CONFIRM_HI,
                               label="Breakout volume vs 20-day average (x)"),
        "BREAKOUT_HIGH_PCT": Setting("BREAKOUT_HIGH_PCT", "float", default("BREAKOUT_HIGH_PCT"),
                                     lo=cfg.BREAKOUT_HIGH_PCT_LO, hi=cfg.BREAKOUT_HIGH_PCT_HI,
                                     label="Overhead breakout (share of the 52-week high)"),
        "MOVE_MIN": Setting("MOVE_MIN", "float", default("MOVE_MIN"),
                            lo=cfg.MOVE_MIN_LO, hi=cfg.MOVE_MIN_HI,
                            label="Preceding move over 1-3 months (share)"),
        "ADR_MIN": Setting("ADR_MIN", "float", default("ADR_MIN"),
                           lo=cfg.ADR_MIN_LO, hi=cfg.ADR_MIN_HI,
                           label="Average daily range (share)"),
        "W_PROX": Setting("W_PROX", "float", default("W_PROX"),
                          lo=cfg.W_PROX_LO, hi=cfg.W_PROX_HI,
                          label="Rank weight: 52-week proximity"),
        "W_CONS": Setting("W_CONS", "float", default("W_CONS"),
                          lo=cfg.W_CONS_LO, hi=cfg.W_CONS_HI,
                          label="Rank weight: trend consistency"),
        "STOP_ATR": Setting("STOP_ATR", "float", default("STOP_ATR"),
                            lo=cfg.STOP_ATR_LO, hi=cfg.STOP_ATR_HI,
                            label="Stop distance (x average true range)"),
        "TARGET_R": Setting("TARGET_R", "float", default("TARGET_R"),
                            lo=cfg.TARGET_R_LO, hi=cfg.TARGET_R_HI,
                            label="Target (x the risk)"),
        "OPERATING_ONLY": _operating_only(default("OPERATING_ONLY")),
    }


def _swing_rules(v: dict) -> list[str]:
    """Cross-rules for Swing, over the values as they would be saved.

    ``ranking.composite`` divides by the weights it was given, so any weights still give a number; the one
    thing that must stay true is that momentum (``W_12_1``, 0.50) remains the board's largest tilt, i.e. the
    two editable weights together never outweigh it. The target may sit under the stop distance: a sub-1R
    target is a choice, not an error.
    """
    total = float(v["W_PROX"]) + float(v["W_CONS"])
    if total > 1.0 + 1e-9:
        return [f"W_PROX + W_CONS must not exceed 1 (momentum is the largest tilt); got {total:g}"]
    return []


def _fund_cfg():
    from RevenueChaser.module_fundamentals import config
    return config


def _fund_specs() -> dict[str, Setting]:
    """Fundamentals' editable inputs (PR 2): the 7-A prefilter's two floors, no longer fixed, and the type filter.
    The score thresholds (Rule of 40, Quality, Momentum) stay config-only."""
    cfg = _fund_cfg()

    def default(name: str):
        return lambda: getattr(_fund_cfg(), name)

    return {
        "MIN_PRICE": Setting("MIN_PRICE", "float", default("MIN_PRICE"),
                             lo=cfg.MIN_PRICE_LO, hi=cfg.MIN_PRICE_HI, label="Minimum price ($)"),
        "MIN_DOLLAR_VOLUME": Setting("MIN_DOLLAR_VOLUME", "float", default("MIN_DOLLAR_VOLUME"),
                                     lo=cfg.MIN_DOLLAR_VOLUME_LO, hi=cfg.MIN_DOLLAR_VOLUME_HI,
                                     label="Minimum daily dollar volume ($)"),
        "OPERATING_ONLY": _operating_only(default("OPERATING_ONLY")),
    }


def _no_rules(_v: dict) -> list[str]:
    return []


SPEC: dict[str, tuple[Callable[[], dict[str, Setting]], Callable[[dict], list[str]]]] = {
    "module_comovement": (_comov_specs, _comov_rules),
    "module_fundamentals": (_fund_specs, _no_rules),
    "module_swing_potential": (_swing_specs, _swing_rules),
}


def boards() -> list[str]:
    return sorted(SPEC)


def _specs(board: str) -> dict[str, Setting]:
    if board not in SPEC:
        raise KeyError(f"no settings for board {board!r}")
    return SPEC[board][0]()


def _coerce(spec: Setting, raw):
    """Number of the spec's kind, or ``None`` when ``raw`` is not one."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if spec.kind == "int":
        return int(f) if f == int(f) else None
    return f


def _stored(board: str) -> dict[str, float]:
    try:
        rows = rawstore.query(f"SELECT key, value FROM {TABLE} WHERE board = ?", [board])
    except Exception:  # noqa: BLE001 - table absent on a fresh / pre-#40 store
        return {}
    return {r["key"]: r["value"] for r in rows if r.get("value") is not None}


def _typed(spec: Setting, value):
    v = _coerce(spec, value)
    return spec.default() if v is None else v


def defaults(board: str) -> dict:
    return {k: s.default() for k, s in _specs(board).items()}


def values(board: str) -> dict:
    """``{"values": effective, "defaults": config defaults, "overridden": [keys]}``."""
    specs = _specs(board)
    stored = _stored(board)
    eff, over = {}, []
    for key, spec in specs.items():
        if key in stored:
            eff[key] = _typed(spec, stored[key])
            over.append(key)
        else:
            eff[key] = spec.default()
    return {"values": eff, "defaults": defaults(board), "overridden": sorted(over)}


def describe(board: str) -> list[dict]:
    """One row per editable key for a settings UI: its effective ``value``, the
    config ``default``, whether it is ``overridden``, its kind, bounds and (for a
    closed set) ``choices``. Read-only; the bounds are the ones ``validate`` uses."""
    vals = values(board)
    out = []
    for key, spec in _specs(board).items():
        out.append({
            "key": key, "label": spec.label or key, "kind": spec.kind,
            "value": vals["values"][key], "default": vals["defaults"][key],
            "overridden": key in vals["overridden"],
            "lo": spec.lo, "hi": spec.hi,
            "choices": list(spec.choices()) if spec.choices is not None else None,
        })
    return out


def get(board: str, key: str):
    """The override for ``key`` when one is stored, else the config default."""
    specs = _specs(board)
    if key not in specs:
        raise KeyError(f"unknown setting {key!r} for board {board!r}")
    stored = _stored(board)
    if key in stored:
        return _typed(specs[key], stored[key])
    return specs[key].default()


def is_custom(board: str) -> bool:
    return bool(values(board)["overridden"])


def _plain_number(v: float) -> str:
    """A bound or a value as plain digits: ``:g`` would print 50000000 as '5e+07'."""
    return f"{v:.0f}" if float(v) == int(v) else f"{v:g}"


def _range_text(spec: Setting) -> str:
    if spec.choices is not None:
        return "one of " + ", ".join(str(c) for c in spec.choices())
    if spec.lo is not None and spec.hi is not None:
        return f"between {_plain_number(spec.lo)} and {_plain_number(spec.hi)}"
    if spec.lo is not None:
        return f"at least {_plain_number(spec.lo)}"
    return "a number"


def validate(board: str, changes: dict) -> list[str]:
    """Messages for every rule ``changes`` would break; ``[]`` when valid.

    Per-key bounds first; cross-rules run over the merged values (stored +
    changes) only when every changed key is individually valid.
    """
    specs = _specs(board)
    if not isinstance(changes, dict) or not changes:
        return ["no changes given"]
    msgs: list[str] = []
    clean: dict = {}
    for key, raw in changes.items():
        spec = specs.get(key)
        if spec is None:
            msgs.append(f"unknown setting {key!r}; editable: {', '.join(sorted(specs))}")
            continue
        v = _coerce(spec, raw)
        if v is None:
            kind = "a whole number" if spec.kind == "int" else "a number"
            msgs.append(f"{key} must be {kind} {_range_text(spec)}; got {raw!r}")
            continue
        if spec.choices is not None:
            if v not in spec.choices():
                msgs.append(f"{key} must be {_range_text(spec)}; got {v}")
                continue
        else:
            if spec.lo is not None and v < spec.lo or spec.hi is not None and v > spec.hi:
                msgs.append(f"{key} must be {_range_text(spec)}; got {_plain_number(v)}")
                continue
        clean[key] = v
    if msgs:
        return msgs
    merged = values(board)["values"]
    merged.update(clean)
    return SPEC[board][1](merged)


def save(board: str, changes: dict) -> dict:
    """Validate then store ``changes`` (a value equal to its default drops the
    override instead). Raises ``SettingsError``; returns ``values(board)``.
    One transaction (#306): the overrides and the drops land together or not at all."""
    msgs = validate(board, changes)
    if msgs:
        raise SettingsError(msgs)
    specs = _specs(board)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, drop = [], []
    for key, raw in changes.items():
        v = _coerce(specs[key], raw)
        if v == specs[key].default():
            drop.append(key)
        else:
            rows.append({"board": board, "key": key, "value": float(v), "updated_at": now})

    def _write(tx):
        if rows:
            tx.write_rows(TABLE, rows)
        if drop:
            marks = ", ".join("?" * len(drop))
            tx.execute(f"DELETE FROM {TABLE} WHERE board = ? AND key IN ({marks})", [board, *drop])

    if rows or drop:
        with _WRITE_LOCK:
            rawstore.atomic(_write)
    return values(board)


def reset(board: str, keys: list[str] | None = None) -> dict:
    """Delete the override rows (all of the board's, or just ``keys``), so every
    default is read from config again. Returns ``values(board)``.

    One statement, so the Reset is all-or-nothing and the store records it as one
    change: an 'absent' row per removed key in ``board_settings_changes`` (#306)."""
    specs = _specs(board)
    targets = list(specs) if keys is None else [k for k in keys if k in specs]
    if targets:
        marks = ", ".join("?" * len(targets))
        with _WRITE_LOCK:
            rawstore.execute(f"DELETE FROM {TABLE} WHERE board = ? AND key IN ({marks})", [board, *targets])
    return values(board)
