"""Persist the Fundamentals scan to the raw store (single writer, ``replace_all``).

- ``fundamentals_scores`` — per-ticker OWN metrics (no identity/GICS; those live
  on ``equity_universe``).
- ``fundamentals_gates`` — one typed row per ``(ticker, gate, rule_id)``.

Pure transforms (``score_rows`` / ``gate_rows``) are separated from ``persist`` so
they are testable without touching the DB.
"""
from __future__ import annotations

from RevenueChaser.core import rawstore

from .screeners import (
    evaluate_momentum,
    evaluate_quality,
    evaluate_rule_of_40,
    is_distorted,
)


def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _pass_count(score) -> int | None:
    """``"2/3"`` -> 2; anything else -> None."""
    try:
        return int(str(score).split("/")[0])
    except (ValueError, AttributeError):
        return None


def _gates(m: dict) -> dict:
    return {
        "rule_of_40": evaluate_rule_of_40(m),
        "quality": evaluate_quality(m),
        "momentum": evaluate_momentum(m),
    }


def score_rows(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        m = r.get("metrics") or {}
        g = _gates(m)
        rows.append({
            "ticker": r["ticker"], "period": r.get("period"),
            "multiplier": sum(1 for x in g.values() if x["passed"]),
            "distorted": int(bool(is_distorted(m))),
            "quality_pass": _pass_count(g["quality"]["score"]),
            "momentum_pass": _pass_count(g["momentum"]["score"]),
            "ro40": _f(m.get("ro40")),
            "ro40_basis": m.get("ro40_basis"),
            "rev_growth_ttm": _f(m.get("rev_growth_ttm")),
            "fcf_margin": _f(m.get("fcf_margin")),
            "ttm_revenue": _f(m.get("ttm_revenue")),
            "gross_margin": _f(m.get("gross_margin")),
            "roic": _f(m.get("roic")),
            "net_debt_ebitda": _f(m.get("net_debt_ebitda")),
            "pe": _f(m.get("pe")), "pb": _f(m.get("pb")),
            "dist_52w_pct": _f(m.get("dist_52w_pct")),
            "rs_6m": _f(m.get("rs_6m")),
            "above_200d": (None if m.get("above_200d") is None
                           else int(bool(m["above_200d"]))),
            "anchor_period_end": r.get("anchor_period_end"),
            "price": _f(r.get("price")),
            "market_cap": _f(r.get("market_cap")),
        })
    return rows


def gate_rows(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        g = _gates(r.get("metrics") or {})
        for gate_name, gate in g.items():
            for rule in gate.get("rules", []):
                rows.append({
                    "ticker": r["ticker"], "gate": gate_name,
                    "rule_id": rule["id"],
                    "passed": None if rule["passed"] is None else int(bool(rule["passed"])),
                    "actual": _f(rule.get("actual")), "unit": rule.get("unit"),
                })
    return rows


def edgar_versions(results: list[dict]) -> dict[str, int]:
    """How many scored tickers rest on each ``edgar_financials`` extract_version ('none' = the row has none)."""
    out: dict[str, int] = {}
    for r in results:
        key = str(r.get("edgar_extract_version") or "none")
        out[key] = out.get(key, 0) + 1
    return out


def persist(results: list[dict], sink: dict | None = None) -> dict:
    """Full-refresh both tables from one scan. The pipeline never persists an empty scan; an empty
    ``results`` is refused by ``rawstore.replace_all`` (#284) instead of wiping the last scores.

    ``sink`` (a dict) receives the rows just written under ``scores`` and ``gates``, and the EDGAR extract versions
    behind them under ``edgar_versions``, so the result history (``history.record``) records exactly what the latest
    tables got (#278)."""
    srows, grows = score_rows(results), gate_rows(results)
    rawstore.replace_all("fundamentals_scores", srows)
    rawstore.replace_all("fundamentals_gates", grows)
    if sink is not None:
        sink.update(scores=srows, gates=grows, edgar_versions=edgar_versions(results))
    return {"scores": len(srows), "gates": len(grows)}
