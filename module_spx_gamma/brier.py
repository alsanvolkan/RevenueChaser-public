"""Brier scorecard — honest audit of past scenario probabilities.

On each report we record the scenario probabilities for the asof date. When a
later session closes, we can score that date: Brier = sum((p - o)^2) over the
3 scenario classes (0-2 scale, lower = better). A running average is shown
against the equal-weight reference (2/3) as a Brier Skill Score: BSS = 1 - average /
reference (0 = no better than 1/3 each, 1 = perfect, negative = worse).

The verdict is decided against the base rate instead (review N6): the Brier of always
forecasting the outcome frequencies the scored days actually had. Beating 1/3 each is
easy when one outcome dominates (constant probabilities do it), so it proves nothing.
Each record is one report day, so the scorecard counts days.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

from . import store
from .scenarios import REPORT_AS_OF, realized_class

logger = logging.getLogger(__name__)

CLASSES = ["Consolidation", "Upside", "Downside"]
# Uniform 1/3 forecast against a one-hot outcome: (1 - 1/3)^2 + 2 * (1/3)^2 = 2/3.
EQUAL_WEIGHT_REFERENCE = (1 - 1 / len(CLASSES)) ** 2 + (len(CLASSES) - 1) * (1 / len(CLASSES)) ** 2
PROB_SUM_TOLERANCE = 0.01


def current_rule() -> str:
    """The scenario rule a record has to carry to count: the report ``scenarios.py`` implements."""
    return REPORT_AS_OF


def scored_under_current_rule(records: list[dict]) -> list[dict]:
    """The scored records of the current rule.

    A record is only comparable with records built from the same bands. The 30 Oct report moved every band edge
    and every probability (avo, 3 Nov 2026: the scorecard restarts from zero), so records written under an earlier
    rule keep their outcome and Brier for the audit but no longer count towards the average. A record with no
    ``rule`` at all predates the stamp and is therefore not of this rule.
    """
    return [r for r in records if r.get("brier") is not None and r.get("rule") == current_rule()]


def skill_score(average: float | None) -> float | None:
    """Brier Skill Score against the equal-weight reference; None when nothing is scored."""
    if average is None:
        return None
    return 1.0 - average / EQUAL_WEIGHT_REFERENCE


def base_rate_reference(records: list[dict]) -> float | None:
    """Brier of forecasting the scored days' own outcome frequencies: 1 - sum(f_c^2); None when nothing is scored.

    shortcut: the frequencies are in-sample (the same days the average covers), which flatters the reference a
    little; upgrade to a trailing out-of-sample base rate once a few hundred days are scored."""
    outcomes = [r.get("outcome") for r in scored_under_current_rule(records) if r.get("outcome") in CLASSES]
    if not outcomes:
        return None
    return 1.0 - sum((outcomes.count(c) / len(outcomes)) ** 2 for c in CLASSES)


def _validated_probs(probs: dict) -> dict[str, float]:
    """The three class probabilities, or ValueError: all present, finite, in [0, 1], summing to 1."""
    out: dict[str, float] = {}
    for c in CLASSES:
        v = probs.get(c)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0.0 <= v <= 1.0:
            raise ValueError(f"scenario probability for {c!r} must be a number in [0, 1], got {v!r}")
        out[c] = float(v)
    total = sum(out.values())
    if abs(total - 1.0) > PROB_SUM_TOLERANCE:
        raise ValueError(f"scenario probabilities must sum to 1, got {total:.4f}")
    return out


def brier(probs: dict, realized: str) -> float:
    """Multi-class Brier score: sum of ``(p - o)^2`` over CLASSES (0-2, lower is better).

    ``probs`` goes through ``_validated_probs`` and ``realized`` must be one of
    CLASSES; anything else is a ValueError rather than a silently lenient score.
    """
    if realized not in CLASSES:
        raise ValueError(f"realized scenario must be one of {CLASSES}, got {realized!r}")
    clean = _validated_probs(probs)
    return sum((clean[c] - (1.0 if c == realized else 0.0)) ** 2 for c in CLASSES)


def load_scores() -> dict:
    """``{"records", "average", "n"}`` from the board file; average and n are computed, not stored."""
    data = {"records": store.brier_records()}
    _refresh_average(data)
    return data


def target_friday(asof_date: str) -> str:
    """The Friday whose official close resolves a record made on ``asof_date``.

    The 30 Oct report (p.19) resolves a week's scenario set on the NEXT Friday's close: a Friday's record
    resolves a week later, any other day's on the coming Friday.
    """
    d = date.fromisoformat(asof_date)
    ahead = (4 - d.weekday()) % 7 or 7
    return (d + timedelta(days=ahead)).isoformat()


def _clean_bands(bands: dict | None) -> dict[str, list[float]] | None:
    if not bands:
        return None
    out: dict[str, list[float]] = {}
    for key in ("consolidation", "upside", "downside"):
        lo_hi = bands.get(key)
        if not lo_hi or len(lo_hi) != 2:
            return None
        if key == "upside" and lo_hi[1] is None:
            # an open upside (the pin at or below the flip): scoring reads only the consolidation band
            try:
                lo = float(lo_hi[0])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(lo):
                return None
            out[key] = [lo, None]
            continue
        try:
            lo, hi = float(lo_hi[0]), float(lo_hi[1])
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(lo) and math.isfinite(hi)):
            return None
        out[key] = [lo, hi]
    return out


def record_probs(asof_date: str, probs: dict[str, float], bands: dict | None = None) -> dict:
    """Append a probability record for an asof date (idempotent per date).

    ``bands`` are the scenario bands the probabilities were given for; a record without them cannot be
    scored later, because the outcome class depends on them. A record that is already scored is kept as
    it is: its outcome and Brier were computed from its own probabilities, so replacing them would break
    the audit (SPX-4).
    """
    clean = _validated_probs(probs)
    data = load_scores()
    for r in data.get("records", []):
        if r.get("date") == asof_date and (r.get("outcome") is not None or r.get("brier") is not None):
            logger.info("spx brier record for %s is scored; kept", asof_date)
            return data
    store.put_brier([{
        "date": asof_date,
        "scenario_probs": clean,
        "outcome": None,
        "brier": None,
        "rule": current_rule(),
        "target": target_friday(asof_date),
        "bands": _clean_bands(bands),
    }])
    return load_scores()


def _resolving_close(asof_date: str, target: str, closes: dict[str, float]) -> tuple[str, float] | None:
    """The close that resolves a record, or None while its week is still open.

    The target Friday's close when it exists. When that Friday was a market holiday and a later session has
    closed, the week is over and its last close (after the asof date) resolves it.
    """
    if target in closes:
        return target, float(closes[target])
    if not any(d > target for d in closes):
        return None
    week = sorted(d for d in closes if asof_date < d < target)
    if not week:
        return None
    return week[-1], float(closes[week[-1]])


def _refresh_average(data: dict) -> None:
    scored = scored_under_current_rule(data.get("records", []))
    if scored:
        data["average"] = sum(r["brier"] for r in scored) / len(scored)
        data["n"] = len(scored)
    else:
        data["average"] = None
        data["n"] = 0


def score_pending(closes: dict[str, float]) -> dict:
    """Score every unscored record whose target week has closed, from SPX daily closes ``{date: close}``.

    A record keeps the outcome it got first; a record without bands is left unscored.
    """
    data = load_scores()
    scored_now: list[str] = []
    changed: list[dict] = []
    for r in data.get("records", []):
        if r.get("outcome") is not None or r.get("brier") is not None:
            continue
        bands = r.get("bands")
        asof = r.get("date")
        if not bands or not asof:
            continue
        target = r.get("target") or target_friday(asof)
        hit = _resolving_close(asof, target, closes)
        if hit is None:
            continue
        on, close = hit
        outcome = realized_class({k: tuple(v) for k, v in bands.items()}, close)
        if outcome not in CLASSES:
            continue
        r["brier"] = brier(r.get("scenario_probs") or {}, outcome)
        r["outcome"] = outcome
        r["resolved_on"] = on
        r["resolved_close"] = close
        scored_now.append(asof)
        changed.append(r)
    if scored_now:
        store.put_brier(changed)
        _refresh_average(data)
    return {"scored": scored_now, "data": data}


def score_date(asof_date: str, realized: str) -> dict:
    """Score the unscored record for ``asof_date`` with the realized scenario class (a scored one is kept)."""
    if realized not in CLASSES:
        raise ValueError(f"realized scenario must be one of {CLASSES}, got {realized!r}")
    data = load_scores()
    for r in data.get("records", []):
        if r.get("date") != asof_date:
            continue
        r["brier"] = brier(r.get("scenario_probs") or {}, realized)
        r["outcome"] = realized
        store.put_brier([r])
        break
    return load_scores()


def scorecard() -> dict:
    """Summary for the dashboard: average, n, both references and the verdict against the base rate."""
    data = load_scores()
    records = data.get("records", [])
    avg = data.get("average")
    n = data.get("n", 0)
    base = base_rate_reference(records) if avg is not None else None
    base_skill = (1.0 - avg / base) if (base is not None and base > 0) else None
    if avg is None:
        verdict = "No scored days yet — scorecard is in training mode."
        calibrated = None
    elif base_skill is None:
        verdict = (
            f"Average Brier {avg:.4f} over {n} scored day(s), all with one outcome, so there is no base rate to "
            f"beat yet (equal-weight reference {EQUAL_WEIGHT_REFERENCE:.4f}, skill {skill_score(avg):+.2f}). "
            "Recorded for audit only."
        )
        calibrated = None
    elif avg < base:
        verdict = (
            f"Average Brier {avg:.4f} is below the base-rate reference ({base:.4f}, always forecasting the "
            f"observed outcome frequencies), base-rate skill {base_skill:+.2f} — "
            "this probability layer adds information over the base rate."
        )
        calibrated = True
    else:
        verdict = (
            f"Average Brier {avg:.4f} does NOT beat the base-rate reference ({base:.4f}, always forecasting the "
            f"observed outcome frequencies), base-rate skill {base_skill:+.2f} — "
            "this probability layer is NOT yet calibrated "
            f"(equal-weight reference {EQUAL_WEIGHT_REFERENCE:.4f}, skill {skill_score(avg):+.2f}, is no test: "
            "constant probabilities beat it whenever one outcome dominates). Recorded for audit only."
        )
        calibrated = False
    counted = [float(r["brier"]) for r in scored_under_current_rule(records)]
    threshold = diff = discriminates = None
    if len(counted) >= 2 and avg is not None:
        mean = sum(counted) / len(counted)
        sd = math.sqrt(sum((x - mean) ** 2 for x in counted) / (len(counted) - 1))
        threshold = 2.0 * sd / math.sqrt(len(counted))
        diff = mean - EQUAL_WEIGHT_REFERENCE
        discriminates = abs(diff) > threshold
        verdict += (f" Difference from 1/3 each: {diff:+.4f}; the report's discrimination threshold (2 standard "
                    f"errors) is {threshold:.4f}, so the forecasts "
                    f"{'do' if discriminates else 'do NOT yet'} discriminate.")
    older = [r for r in records if r.get("brier") is not None and r.get("rule") != current_rule()]
    if older:
        verdict += (f" {len(older)} scored day(s) of an earlier scenario rule are kept for the audit but not "
                    f"counted: their bands and probabilities were different numbers.")
    return {
        "records": records,
        "rule": current_rule(),
        "retired_scored_count": len(older),
        "scored_count": n,
        "average": avg,
        "equal_weight_reference": EQUAL_WEIGHT_REFERENCE,
        "skill_score": skill_score(avg),
        "base_rate_reference": base,
        "base_rate_skill_score": base_skill,
        "verdict": verdict,
        "calibrated": calibrated,
        "recorded_count": len(records),
        "discrimination_threshold": threshold,
        "difference_vs_equal_weight": diff,
        "discriminates": discriminates,
    }
