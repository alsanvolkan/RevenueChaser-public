"""Board inputs — what each board reads, its knobs, and its own funnel.

Store-only: a card is built from the board's last rendered payload JSON in
``.cache`` and never recomputes the board (zero network). The funnel shown is
the one the board itself emitted (``core.funnel`` format), so the Settings card
and the board cannot disagree. See ``docs/data_health.md``.
"""
from __future__ import annotations

import json

from RevenueChaser.core import board_settings
from RevenueChaser.core import funnel as funnel_mod
from RevenueChaser.core.universe import DEFAULT_MC_MIN
from RevenueChaser.module_comovement import config as comov_config
from RevenueChaser.module_comovement import settings as comov_settings
from RevenueChaser.module_swing_potential import config as swing_config
from RevenueChaser.module_swing_potential import settings as swing_settings


def _comovement_params() -> list[str]:
    """Plain-label knobs, read from ``module_comovement.config`` at call time."""
    c = comov_config
    eff = comov_settings.effective()
    bench = c.BENCHMARKS.get(c.DEFAULT_BENCHMARK) or "off"
    return [
        f"Window {eff.window} sessions",
        f"Min observations {eff.min_obs}",
        f"Out-of-sample holdout {eff.oos_holdout} sessions",
        f"Market-cap floor ${DEFAULT_MC_MIN / 1e6:,.0f}M",
        f"Default benchmark {bench}",
        f"Default horizon {c.DEFAULT_HORIZON} day",
        f"Price history {c.OHLC_PERIOD}",
        f"Winsorize at {eff.winsor_k} sigma",
    ]


_WINDOW_LABELS = {21: "1m", 42: "2m", 126: "6m", 189: "9m", 252: "1y", 378: "1.5y"}
_INPUT_KEYS = ("WINDOW", "MIN_OBS", "OOS_HOLDOUT")
_FINE_KEYS = ("WINSOR_K", "MIN_PERIODS", "BENCH_STALE_SESSIONS")


def _range_text(f: dict) -> str:
    """The allowed range, as the Settings card prints it next to the field."""
    key, lo, hi = f["key"], f["lo"], f["hi"]
    if key == "WINDOW":
        return ", ".join(_WINDOW_LABELS.get(c, str(c)) for c in f["choices"])
    if key == "MIN_OBS":
        return f"{lo:g}–WINDOW"
    if key == "MIN_PERIODS":
        return f"{lo:g}–MIN_OBS"
    return f"{lo:g}–{hi:g}"


def _comovement_settings() -> dict:
    """Editable inputs for the card: effective value, default, override flag, range."""
    rows = {f["key"]: f for f in board_settings.describe(comov_settings.BOARD)}
    for f in rows.values():
        f["range"] = _range_text(f)
        if f["choices"] is not None:
            f["choices"] = [[c, f"{_WINDOW_LABELS.get(c, c)} ({c})"] for c in f["choices"]]
    holdout = int(rows["OOS_HOLDOUT"]["value"])
    return {
        "board": comov_settings.BOARD,
        "custom": any(f["overridden"] for f in rows.values()),
        "inputs": [rows[k] for k in _INPUT_KEYS],
        "fine": [rows[k] for k in _FINE_KEYS],
        "derived": {"key": "OOS_MIN_OBS",
                    "label": "Held-out observations needed (derived)",
                    "value": comov_settings.oos_min_obs_for(holdout)},
    }


def _plain_range(f: dict) -> str:
    """``lo–hi`` as the card prints it (every Swing key has both ends)."""
    return f"{f['lo']:g}–{f['hi']:g}"


_SWING_INPUT_KEYS = ("MIN_PRICE", "MIN_DOLLAR_VOLUME", "RS_MIN")
_SWING_FINE_KEYS = ("VOL_CONFIRM", "BREAKOUT_HIGH_PCT", "MOVE_MIN", "ADR_MIN",
                    "W_PROX", "W_CONS", "STOP_ATR", "TARGET_R")


def _swing_params() -> list[str]:
    """Plain-label knobs, read from the effective settings at call time (#384 SW-S4)."""
    e = swing_settings.effective()
    return [
        f"Minimum price ${e.min_price:g}",
        f"Minimum dollar volume ${e.min_dollar_volume:,.0f} a day",
        f"RS rating floor {e.rs_min:g}",
        f"Breakout volume {e.vol_confirm:g}× the 20-day average",
        f"Overhead breakout at {round(e.breakout_high_pct * 100, 2):g}% of the 52-week high",
        f"Preceding move {round(e.move_min * 100, 2):g}% over 1–3 months",
        f"Average daily range {round(e.adr_min * 100, 2):g}%",
        f"Stop {e.stop_atr:g}× ATR · target {e.target_r:g}× the risk",
        f"Rank weights: proximity {e.w_prox:g}, consistency {e.w_cons:g}",
    ]


def _swing_settings() -> dict:
    """Editable thresholds for the Swing card: effective value, default, override flag, range."""
    rows = {f["key"]: f for f in board_settings.describe(swing_settings.BOARD)}
    for f in rows.values():
        f["range"] = _plain_range(f)
    return {
        "board": swing_settings.BOARD,
        "custom": any(f["overridden"] for f in rows.values()),
        "inputs": [rows[k] for k in _SWING_INPUT_KEYS],
        "fine": [rows[k] for k in _SWING_FINE_KEYS],
    }


BOARD_INPUTS: list[dict] = [
    {
        "key": "module_comovement",
        "label": "Co-movement",
        "tables": ["equity_universe", "swing_ohlc"],
        "price_basis": "dividend-adjusted closes (yfinance auto_adjust); correct for returns",
        "params": _comovement_params,
        "payload_path": lambda: comov_config.COMOVEMENT_JSON,
        "settings": _comovement_settings,
    },
    {
        "key": "module_swing_potential",
        "label": "Swing",
        "tables": ["equity_universe", "price_bars_daily"],
        "price_basis": "Daily Bars D01: levels nominal, returns adjusted",
        "params": _swing_params,
        "payload_path": lambda: swing_config.SWING_JSON,
        "settings": _swing_settings,
    },
]


def _path_of(entry: dict):
    p = entry["payload_path"]
    return p() if callable(p) else p


def _card(entry: dict) -> dict:
    card = {
        "key": entry["key"], "label": entry["label"], "tables": list(entry["tables"]),
        "price_basis": entry["price_basis"], "params": entry["params"](),
        "as_of": None, "window": {}, "funnel_steps": [], "state": "ok", "error": None,
    }
    if entry.get("settings"):
        card["settings"] = entry["settings"]()
    path = _path_of(entry)
    if not path.exists():
        card["state"] = "not_rendered"
        return card
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload JSON is not an object")
    except Exception as exc:  # noqa: BLE001 - a bad artifact is shown, never raised
        card["state"] = "error"
        card["error"] = f"{type(exc).__name__}: {exc}"
        return card
    card["as_of"] = payload.get("as_of") or payload.get("generated_at")
    card["window"] = {k: payload.get(k) for k in
                      ("window_start", "oos_start", "window_end", "prices_through")}
    steps = funnel_mod.from_dict(payload.get("funnel_steps"))
    if not steps:
        card["state"] = "no_funnel"
        return card
    card["funnel_steps"] = steps
    problems = funnel_mod.validate(steps)
    if problems:
        card["state"] = "invalid"
        card["error"] = "; ".join(problems)
    return card


def board_cards() -> list[dict]:
    """One card per registered board. Never raises."""
    cards = []
    for entry in BOARD_INPUTS:
        try:
            cards.append(_card(entry))
        except Exception as exc:  # noqa: BLE001
            cards.append({"key": entry.get("key"), "label": entry.get("label"),
                          "tables": [], "price_basis": "", "params": [], "as_of": None,
                          "window": {}, "funnel_steps": [], "state": "error",
                          "error": f"{type(exc).__name__}: {exc}"})
    return cards
