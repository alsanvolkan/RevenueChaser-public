"""Board inputs — what each board reads, its knobs, and its own funnel.

Store-only: a card is built from the board's last rendered payload JSON in
``.cache`` and never recomputes the board (zero network). The funnel shown is
the one the board itself emitted (``core.funnel`` format), so the Settings card
and the board cannot disagree. See ``docs/data_health.md``.

``funnel`` on an entry is the payload key the board emits (``funnel_steps``),
or ``no_funnel`` when the payload emits none. The card reads that key through
``core.funnel`` and does not invent steps. A missing artifact is
``not_rendered``. A funnel board whose payload has no steps is ``no_funnel``
(the existing "refresh" note). A ``no_funnel`` board never asks for a refresh
to grow a funnel it does not emit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from RevenueChaser.core import board_settings
from RevenueChaser.core import funnel as funnel_mod
from RevenueChaser.core.config import OUTPUT_DIR
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


# A board whose payload does not emit ``funnel_steps`` (or ``funnel``). The card
# degrades by showing reads and, when a JSON payload exists, its as-of — never
# by inventing steps or telling the user to Refresh for a funnel.
NO_FUNNEL = "no_funnel"


def _no_params() -> list[str]:
    return []


def _json(*parts: str) -> Path:
    """A regenerable payload under ``.cache`` (``RC_CACHE_DIR`` via ``OUTPUT_DIR``)."""
    return OUTPUT_DIR.joinpath(*parts)


def _html(name: str) -> Path:
    return OUTPUT_DIR / f"{name}_latest.html"


def _spx_latest() -> Path:
    """SPX writes ``latest.json`` under its module data root, not ``.cache``."""
    root = Path(os.environ.get("SPX_GAMMA_DATA_ROOT")
                or Path(__file__).resolve().parent.parent / "module_spx_gamma" / "data")
    return root / "latest.json"


def _board(key: str, label: str, tables: list[str], payload_path, *,
           funnel: str = NO_FUNNEL, price_basis: str = "", params=None,
           settings=None) -> dict:
    row = {
        "key": key, "label": label, "tables": list(tables),
        "price_basis": price_basis, "params": params or _no_params,
        "payload_path": payload_path, "funnel": funnel,
    }
    if settings is not None:
        row["settings"] = settings
    return row


# Nav order (``app/dashboard.py`` TABS), minus Settings and Logs. Tables are the
# store tables each board's engine or pipeline actually reads, not the tables it
# writes. ``funnel_steps`` only where the rendered payload emits that list.
BOARD_INPUTS: list[dict] = [
    _board("module_earnings_radar", "Earnings Radar",
           ["earnings_calendar", "estimate_snapshots", "short_interest", "economic_calendar",
            "macro_series", "vol_index_daily", "cftc_cot", "price_bars_daily"],
           lambda: _html("earnings_radar"),
           price_basis="Daily Bars D01 nominal closes for reactions"),
    _board("module_spx_gamma", "SPX Gamma",
           ["index_levels_daily", "vol_index_daily", "cftc_cot"], _spx_latest,
           price_basis="index_levels_daily closes"),
    _board("module_oipd", "Implied Probability",
           ["oipd_chain", "oipd_meta"], lambda: _json("oipd", "oipd_latest.json"),
           price_basis="option chain marks, not equity closes"),
    _board("module_financials", "Financials",
           ["ticker_payload_cache", "vendor_financials", "vendor_valuation", "equity_universe"],
           lambda: _json("financials", "financials_latest.json")),
    _board("module_uw_interaction", "UW Interaction (lab)", [],
           lambda: _json("uw_interaction", "uw_interaction_latest.json")),
    # Rendered in memory (no cached payload). A missing file is ``not_rendered``.
    _board("module_discord", "Discord",
           ["discord_channels", "discord_messages", "discord_export_runs"],
           lambda: _json("discord", "discord_latest.json")),
    _board("module_comovement", "Co-movement",
           ["equity_universe", "swing_ohlc"],
           lambda: comov_config.COMOVEMENT_JSON,
           funnel="funnel_steps",
           price_basis="dividend-adjusted closes (yfinance auto_adjust); correct for returns",
           params=_comovement_params, settings=_comovement_settings),
    _board("module_value_chain", "Value Chain",
           ["chain_filings", "chain_mentions", "chain_meta", "sec_company_tickers",
            "equity_universe"],
           lambda: _json("value_chain", "value_chain_latest.json")),
    _board("module_relatedness", "Relatedness",
           ["relatedness_profiles", "relatedness_scores", "chain_filings",
            "equity_universe", "ticker_themes"],
           lambda: _json("relatedness", "relatedness_latest.json")),
    _board("module_sector_rotation", "Sector Rotation",
           ["rotation_snapshots", "etf_holdings", "equity_universe"],
           lambda: _html("sector_rotation"),
           price_basis="ETF levels stored in rotation snapshots"),
    _board("module_asymmetric_growth", "Asymmetric Growth",
           ["equity_universe", "edgar_financials", "price_bars_daily", "ownership_snapshot"],
           lambda: _json("asymmetric_growth", "asymmetric_growth_latest.json"),
           funnel="funnel_steps",
           price_basis="Daily Bars D01 nominal closes for momentum"),
    _board("module_swing_potential", "Swing",
           ["equity_universe", "price_bars_daily"],
           lambda: swing_config.SWING_JSON,
           funnel="funnel_steps",
           price_basis="Daily Bars D01: levels nominal, returns adjusted",
           params=_swing_params, settings=_swing_settings),
    _board("module_fundamentals", "Fundamentals",
           ["equity_universe", "quote_snapshot", "price_bars_daily", "edgar_financials"],
           lambda: _json("fundamentals", "fundamentals_latest.json"),
           funnel="funnel_steps",
           price_basis="Daily Bars D01: levels nominal, returns adjusted"),
    _board("module_sec_13f", "SEC 13F",
           ["sec13f_holdings", "sec13f_submissions", "sec13f_totals", "equity_universe"],
           lambda: _html("sec_13f")),
    _board("module_leaps", "LEAPS",
           ["leaps_flow_prints", "leaps_cache", "watchlist"],
           lambda: _html("leaps")),
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
    if path is None or not path.exists():
        card["state"] = "not_rendered"
        return card
    funnel_key = entry.get("funnel") or "funnel_steps"
    # An HTML board that emits no funnel has no JSON to parse. Existence is the
    # rendered signal; json.loads on the page would be a false error.
    if funnel_key == NO_FUNNEL and path.suffix.lower() != ".json":
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
    if funnel_key == NO_FUNNEL:
        return card
    steps = funnel_mod.from_dict(payload.get(funnel_key))
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
