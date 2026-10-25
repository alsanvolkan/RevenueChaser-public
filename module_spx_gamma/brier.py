"""Brier scorecard — honest audit of past scenario probabilities.

On each report we record the scenario probabilities for the asof date. When a
later session closes, we can score that date: Brier = sum((p - o)^2) over the
3 scenario classes (0-2 scale, lower = better). A running average is compared
to the equal-weight reference (2/3) so uncalibrated probabilities are flagged,
not hidden, and reported as a Brier Skill Score: BSS = 1 - average / reference
(0 = no better than 1/3 each, 1 = perfect, negative = worse).
"""
from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path

from . import safe_json
from .config import BRIER_FILENAME, DATA_ROOT

logger = logging.getLogger(__name__)

BRIER_FILE = DATA_ROOT / BRIER_FILENAME
CLASSES = ["Consolidation", "Upside", "Downside"]
# Uniform 1/3 forecast against a one-hot outcome: (1 - 1/3)^2 + 2 * (1/3)^2 = 2/3.
EQUAL_WEIGHT_REFERENCE = (1 - 1 / len(CLASSES)) ** 2 + (len(CLASSES) - 1) * (1 / len(CLASSES)) ** 2
PROB_SUM_TOLERANCE = 0.01


def skill_score(average: float | None) -> float | None:
    """Brier Skill Score against the equal-weight reference; None when nothing is scored."""
    if average is None:
        return None
    return 1.0 - average / EQUAL_WEIGHT_REFERENCE


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


def _default_path() -> Path:
    return BRIER_FILE


def _empty() -> dict:
    return {"records": [], "average": None, "n": 0}


def load_scores(path: Path | None = None) -> dict:
    """Scores for display: a corrupt file reads as empty but is left untouched."""
    p = path or _default_path()
    try:
        data = safe_json.load_json(p, _empty())
    except (safe_json.CorruptJsonError, OSError) as exc:
        logger.error("spx brier unreadable, shown as empty: %s", exc)
        return _empty()
    return data if isinstance(data, dict) else _empty()


def _load_for_update(p: Path) -> dict:
    """Scores for a write: a corrupt file is moved aside and the write refused (S-1)."""
    data = safe_json.load_for_update(p, _empty())
    if not isinstance(data, dict):
        safe_json.quarantine(p)
        raise safe_json.CorruptJsonError(p, f"expected an object, got {type(data).__name__}")
    return data


def record_probs(asof_date: str, probs: dict[str, float], path: Path | None = None,
                 target_date: str | None = None, bands: dict | None = None) -> dict:
    """Append a probability record for an asof date (idempotent per date).

    A record that is already scored is kept as it is: its outcome and Brier were computed from
    its own probabilities, so replacing them would break the audit (SPX-4).
    ``target_date`` (the session whose close scores it) and ``bands`` (the numeric class edges) are
    kept on the record when given (S3b, #38), so a later Fetch can score it without the parsed day.
    """
    p = path or _default_path()
    clean = _validated_probs(probs)
    data = _load_for_update(p)
    for r in data.get("records", []):
        if r.get("date") == asof_date and (r.get("outcome") is not None or r.get("brier") is not None):
            logger.info("spx brier record for %s is scored; kept", asof_date)
            return data
    records = [r for r in data.get("records", []) if r.get("date") != asof_date]
    records.append({
        "date": asof_date,
        "scenario_probs": clean,
        "outcome": None,
        "brier": None,
    })
    if target_date is not None or bands is not None:
        records[-1]["target_date"] = target_date
        records[-1]["bands"] = None if bands is None else {k: list(v) for k, v in bands.items()}
    records.sort(key=lambda r: r["date"])
    data = {"records": records, "average": data.get("average"), "n": data.get("n", 0)}
    safe_json.write_json_atomic(p, data)
    return data


def score_date(asof_date: str, realized: str, path: Path | None = None) -> dict:
    """Score the record for ``asof_date`` with the realized scenario class."""
    if realized not in CLASSES:
        raise ValueError(f"realized scenario must be one of {CLASSES}, got {realized!r}")
    p = path or _default_path()
    data = _load_for_update(p)
    for r in data.get("records", []):
        if r.get("date") != asof_date:
            continue
        r["brier"] = brier(r.get("scenario_probs") or {}, realized)
        r["outcome"] = realized
        break
    scored = [r for r in data.get("records", []) if r.get("brier") is not None]
    if scored:
        avg = sum(r["brier"] for r in scored) / len(scored)
        data["average"] = avg
        data["n"] = len(scored)
    else:
        data["average"] = None
        data["n"] = 0
    safe_json.write_json_atomic(p, data)
    return data


def scorecard(path: Path | None = None) -> dict:
    """Summary for the dashboard: average, n, and calibration verdict."""
    data = load_scores(path=path)
    records = data.get("records", [])
    avg = data.get("average")
    n = data.get("n", 0)
    if avg is None:
        verdict = "No scored weeks yet — scorecard is in training mode."
        calibrated = None
    elif avg <= EQUAL_WEIGHT_REFERENCE:
        verdict = (
            f"Average Brier {avg:.4f} is at or below the equal-weight reference "
            f"({EQUAL_WEIGHT_REFERENCE:.4f}), skill score {skill_score(avg):+.2f} — "
            "this probability layer is calibrated."
        )
        calibrated = True
    else:
        verdict = (
            f"Average Brier {avg:.4f} is ABOVE the equal-weight reference "
            f"({EQUAL_WEIGHT_REFERENCE:.4f}), skill score {skill_score(avg):+.2f} — "
            "this probability layer is NOT yet calibrated "
            "and has not measured better than an uninformed reference. Recorded for audit only."
        )
        calibrated = False
    return {
        "records": records,
        "scored_count": n,
        "average": avg,
        "equal_weight_reference": EQUAL_WEIGHT_REFERENCE,
        "skill_score": skill_score(avg),
        "verdict": verdict,
        "calibrated": calibrated,
        "recorded_count": len(records),
    }
