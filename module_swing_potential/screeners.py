"""Swing screeners — pure, auditable gate evaluations.

Each screener returns a ``GateResult`` dict:

    {"name": str, "passed": bool, "score": "X/Y" or "X/Y of n known", "known": int, "total": int,
     "rules": [{"id", "label", "unit", "passed" (True / False / None = unknown), "actual", "threshold"}, ...]}

``label`` is a plain-language description, ``unit`` tells the frontend how to
format ``actual`` (price / pct / ratio / rating / diff_price / diff_pct /
pct_high), and ``threshold`` is the human-readable comparison.
"""
from __future__ import annotations

from RevenueChaser.core import tristate as tri
from . import config
from . import settings as swing_settings


def _rule(id: str, label: str, unit: str, passed: bool | None, actual, threshold: str) -> dict:
    """``passed`` is True / False / None (unknown: an input is missing, #151); nothing is coerced."""
    return {"id": id, "label": label, "unit": unit, "passed": passed,
            "actual": actual, "threshold": threshold}


def _pct(share: float) -> str:
    """A 0-1 share as the whole percent the card prints (0.98 -> '98', 0.03 -> '3')."""
    return f"{round(share * 100, 4):g}"


def _gate(name: str, rules: list[dict], min_passes: int | None = None,
          decides: list[str] | None = None) -> dict:
    """Passes when at least ``min_passes`` rules are KNOWN passes (all of them by default).

    An unknown rule is never a pass, so a gate that needs every rule passes only when every rule is known and
    passed (decision 8-A); Qullamaggie (5 of 6) passes on five known passes. ``decides`` (a list of rule ids)
    narrows what decides ``passed`` to just those rules, while ``score`` / ``known`` / ``total`` still cover every
    rule: Minervini shows and counts its RS rule but the seven price rules decide ``passed`` (#65 / #384 SW-S1).
    """
    passed_count, n_known, total = tri.tally(rules)
    min_passes = total if min_passes is None else min_passes
    if decides is None:
        passed = passed_count >= min_passes
    else:
        decided = [r for r in rules if r["id"] in decides]
        passed = bool(decided) and all(r["passed"] is True for r in decided)
    return {
        "name": name,
        "passed": passed,
        "score": tri.score_text(passed_count, n_known, total),
        "known": n_known,
        "total": total,
        "rules": rules,
    }


def _diff(a, b):
    return (a - b) if (a is not None and b is not None) else None


def evaluate_minervini(ind: dict, rs_rating: float | None, eff=None) -> dict:
    """Minervini Trend Template — 8/8 rule-set. ``eff`` (an ``Effective``) else the effective settings now."""
    e = eff or swing_settings.effective()
    g = ind.get
    rules = [
        _rule("price_above_150_200", "Price above 150 & 200-day MA", "price",
              tri.rule((g("price"), g("sma150"), g("sma200")), lambda p, a, b: p > a and p > b),
              g("price"), "above both MAs"),
        _rule("ma150_above_ma200", "150-day MA above 200-day MA", "price",
              tri.rule((g("sma150"), g("sma200")), lambda a, b: a > b),
              g("sma150"), "above 200-day MA"),
        _rule("ma200_rising", "200-day MA rising (1 month)", "diff_price",
              tri.rule((g("sma200"), g("sma200_prev_1m")), lambda a, b: a > b),
              _diff(g("sma200"), g("sma200_prev_1m")), "rising vs 1mo ago"),
        _rule("ma50_leading", "50-day MA above 150-day MA", "price",
              tri.rule((g("sma50"), g("sma150")), lambda a, b: a > b),
              g("sma50"), "above 150-day MA"),
        _rule("price_above_50", "Price above 50-day MA", "price",
              tri.rule((g("price"), g("sma50")), lambda p, a: p > a),
              g("price"), "above 50-day MA"),
        _rule("above_52w_low", "At least 25% above 52-week low", "ratio",
              tri.rule((g("above_52w_low_pct"),), lambda v: v >= config.ABOVE_52W_LOW),
              g("above_52w_low_pct"), f"≥ {config.ABOVE_52W_LOW:g}× the low"),
        _rule("within_52w_high", "Within 25% of 52-week high", "pct_high",
              tri.rule((g("within_52w_high_pct"),), lambda v: v >= config.WITHIN_52W_HIGH),
              g("within_52w_high_pct"), f"≥ {_pct(config.WITHIN_52W_HIGH)}% of the high"),
        _rule("rs_ge_70", f"RS Rating at least {e.rs_min:g}", "rating",
              tri.rule((rs_rating,), lambda v: v >= e.rs_min),
              rs_rating, f"≥ {e.rs_min:g}"),
    ]
    # #65: RS is for ranking, not a hard filter — it stays shown and counted, but the seven price rules decide.
    return _gate("Minervini Trend", rules, decides=[r["id"] for r in rules if r["id"] != "rs_ge_70"])


def evaluate_weinstein(ind: dict, eff=None) -> dict:
    """Weinstein Stage Analysis — 5/5 rule-set (Stage 2 breakout)."""
    e = eff or swing_settings.effective()
    g = ind.get
    rules = [
        _rule("price_above_30w", "Price above 30-week MA", "price",
              tri.rule((g("price"), g("sma150")), lambda p, a: p > a),
              g("price"), "above 30-week MA"),
        _rule("ma30w_rising", "30-week MA rising", "diff_price",
              tri.rule((g("sma150"), g("sma150_prev_1m")), lambda a, b: a > b),
              _diff(g("sma150"), g("sma150_prev_1m")), "rising vs 1mo ago"),
        _rule("overhead_breakout", "Above overhead resistance (near 52w high)", "pct_high",
              tri.rule((g("within_52w_high_pct"),), lambda v: v >= e.breakout_high_pct),
              g("within_52w_high_pct"), f"≥ {_pct(e.breakout_high_pct)}% of the high"),
        _rule("volume_confirmation", f"Volume at least {e.vol_confirm:g}× average", "ratio",
              tri.rule((g("vol_ratio"),), lambda v: v >= e.vol_confirm),
              g("vol_ratio"), f"≥ {e.vol_confirm:g}× 20-day avg"),
        _rule("rs_3m_gt_spy", "3-month relative strength above SPY", "diff_pct",
              tri.rule((g("rs_vs_spy_3m"),), lambda v: v > 0),
              g("rs_vs_spy_3m"), "beats SPY"),
    ]
    return _gate("Weinstein Stage", rules)


def evaluate_qullamaggie(ind: dict, eff=None) -> dict:
    """Qullamaggie — 5/6 rule-set (breakout momentum)."""
    e = eff or swing_settings.effective()
    g = ind.get
    rules = [
        _rule("large_move", f"Preceding move at least {_pct(e.move_min)}% (1–3 months)", "pct",
              tri.rule((g("move_max_1_3m"),), lambda v: v >= e.move_min),
              g("move_max_1_3m"), f"≥ {_pct(e.move_min)}%"),
        _rule("price_above_ema10", "Price above 10-day EMA", "price",
              tri.rule((g("price"), g("ema10")), lambda p, e_: p > e_),
              g("price"), "above EMA10"),
        _rule("price_above_ema20", "Price above 20-day EMA", "price",
              tri.rule((g("price"), g("ema20")), lambda p, e_: p > e_),
              g("price"), "above EMA20"),
        _rule("ema10_above_ema20", "10-day EMA above 20-day EMA", "price",
              tri.rule((g("ema10"), g("ema20")), lambda a, b: a > b),
              g("ema10"), "above EMA20"),
        _rule("emas_rising", "Short-term EMAs rising", "price",
              tri.rule((g("ema10"), g("ema10_prev"), g("ema20"), g("ema20_prev")),
                       lambda a, ap, b, bp: a > ap and b > bp),
              g("ema10"), "rising vs 5 days ago"),
        _rule("adr_ge_3", f"Average daily range at least {_pct(e.adr_min)}%", "pct",
              tri.rule((g("adr_pct"),), lambda v: v >= e.adr_min),
              g("adr_pct"), f"≥ {_pct(e.adr_min)}%"),
    ]
    return _gate("Qullamaggie", rules, min_passes=5)


def evaluate_direction(ind: dict) -> dict:
    """The #65 direction condition, required on every card (#384 SW-S1).

    Both rules must be KNOWN passes; an unknown input (a missing average or return) is unknown, never a pass.
    """
    g = ind.get
    rules = [
        _rule("sma50_ge_sma200", "50-day MA at or above 200-day MA", "price",
              tri.rule((g("sma50"), g("sma200")), lambda a, b: a >= b),
              g("sma50"), "≥ 200-day MA"),
        _rule("ret_3m_ge_0", "3-month return at least 0", "pct",
              tri.rule((g("ret_3m"),), lambda v: v >= 0),
              g("ret_3m"), "≥ 0%"),
    ]
    return _gate("Direction", rules)
