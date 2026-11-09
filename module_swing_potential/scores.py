"""Persist the Swing scan to the raw store (single writer, ``replace_all``).

- ``swing_scores`` — per-ticker OWN metrics (ranking + execution levels).
- ``swing_gates`` — one typed row per ``(ticker, gate, rule_id)``.

``score_rows``/``gate_rows`` are pure (no DB) so they are testable in isolation;
``persist`` does the full-refresh write.
"""
from __future__ import annotations

from RevenueChaser.core import rawstore

from . import settings as swing_settings
from .ranking import execution_levels, ma_alignment, percentile_ranks, quality_403030
from .screeners import evaluate_direction, evaluate_minervini, evaluate_qullamaggie, evaluate_weinstein


def _f(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _known(gate: dict) -> int:
    return sum(1 for r in gate.get("rules", []) if r.get("passed") is True)


def _evaluated(results: list[dict], eff=None) -> tuple[list[dict], list[dict]]:
    e = eff or swing_settings.effective()
    rs_pct = percentile_ranks([(r["ticker"], r["ind"].get("rs_vs_spy_12m")) for r in results])

    scores: list[dict] = []
    gates: list[dict] = []
    for r in results:
        t = r["ticker"]
        ind = r.get("ind") or {}
        g = {
            "minervini": evaluate_minervini(ind, rs_pct.get(t), eff=e),
            "weinstein": evaluate_weinstein(ind, eff=e),
            "qullamaggie": evaluate_qullamaggie(ind, eff=e),
            "direction": evaluate_direction(ind),
        }
        lv = execution_levels(ind, eff=e)
        scores.append({
            "ticker": t, "multiplier": sum(1 for k, x in g.items() if k != "direction" and x["passed"]),
            "quality": quality_403030(_known(g["minervini"]), _known(g["weinstein"]), _known(g["qullamaggie"])),
            "direction_ok": int(bool(g["direction"]["passed"])),
            "m_pass": int(bool(g["minervini"]["passed"])),
            "w_pass": int(bool(g["weinstein"]["passed"])),
            "q_pass": int(bool(g["qullamaggie"]["passed"])),
            "rs_rating": _f(rs_pct.get(t)), "price": _f(ind.get("price")),
            "change_pct": _f(ind.get("change_pct")), "change_1w": _f(ind.get("change_1w")),
            "change_1m": _f(ind.get("change_1m")), "entry": _f(lv.get("entry")),
            "target": _f(lv.get("target")), "stop": _f(lv.get("stop")),
            "rr": _f(lv.get("rr")), "atr": _f(lv.get("atr")),
            "ma_alignment": ma_alignment(ind),
        })
        for gate_name, gate in g.items():
            for rule in gate.get("rules", []):
                gates.append({
                    "ticker": t, "gate": gate_name, "rule_id": rule["id"],
                    "passed": None if rule["passed"] is None else int(bool(rule["passed"])),
                    "actual": _f(rule.get("actual")),
                    "unit": rule.get("unit"),
                })
    return scores, gates


def score_rows(results: list[dict], eff=None) -> list[dict]:
    return _evaluated(results, eff=eff)[0]


def gate_rows(results: list[dict], eff=None) -> list[dict]:
    return _evaluated(results, eff=eff)[1]


def persist(results: list[dict], sink: dict | None = None, eff=None) -> dict:
    """Full-refresh write of both tables. ``sink`` (a dict) receives the rows just written under ``scores`` and
    ``gates``, so the result history (``history.record``) records exactly what the latest tables got (#278)."""
    srows, grows = _evaluated(results, eff=eff)
    rawstore.replace_all("swing_scores", srows)
    rawstore.replace_all("swing_gates", grows)
    if sink is not None:
        sink.update(scores=srows, gates=grows)
    return {"scores": len(srows), "gates": len(grows)}
