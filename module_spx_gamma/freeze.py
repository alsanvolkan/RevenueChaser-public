"""S3b (#38): SPX Gamma's scenario forecast in the shared forecast ledger, and its Brier score in ``brier.json``.

The first non-screener family of ``forecast_ledger`` (the rules are ``core/ledger.py``'s; the mapping lives here, as
each screener's ``freeze.py`` does). avo's cards (2026-10-25):

- one row per Fetch day: ``source`` 'spx_gamma', ``subject`` 'SPX', ``target_type`` 'scenario_3class', the three
  class probabilities in ``probs`` and the spot in ``shown_price``;
- ``target_date`` is the last session of the forecast day's week, or of the next week when the forecast day is
  already that session (``week_target``): the bands are this week's levels;
- the freeze follows the screener rule (latest complete session, outside 09:30-16:30 ET, first write wins), and
  only a Fetch reaches it; a Refresh neither freezes nor scores (SPX-4);
- ``model_version`` hashes the probabilities and the band offsets;
- the realized class reads the ``^GSPC`` close of ``index_levels_daily`` (D03, store only) on ``target_date``;
- the Brier score stays in ``brier.json`` (``score_due``); a record from before S3b gets its target from the same
  week rule and its bands from its parsed snapshot, and is left unscored without one;
- the records already in ``brier.json`` are not copied into the ledger: the family starts at the first Fetch.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from RevenueChaser.core import calendar, index_levels, ledger

from . import brier, scenarios

logger = logging.getLogger(__name__)

SOURCE = "spx_gamma"
SUBJECT = "SPX"
TARGET_TYPE = "scenario_3class"
LABEL = "SPX scenario"
SELECTION_VERSION = "spx-s3b-1"      # bump by hand when the probabilities or the band offsets change
CLOSE_SYMBOL = index_levels.SYMBOL   # ^GSPC
# A fixed level set: its bands carry every offset of ``scenario_bands`` into the model version.
_REFERENCE_LEVELS = {"current_level": 1000.0, "levels": {
    "regime_threshold": 1000.0, "upper_resistance_band": 1100.0, "lower_support_band": 900.0}}


def _week_last_session(day: date) -> date | None:
    monday = day - timedelta(days=day.weekday())
    sessions = [d for d in (monday + timedelta(days=i) for i in range(5)) if calendar.is_trading_day(d)]
    return sessions[-1] if sessions else None


def week_target(day: date) -> date | None:
    """The last session of ``day``'s week when it is after ``day``, else the next week's; None past the calendar."""
    try:
        for week in range(3):                    # a week without a session is skipped, never a guess
            last = _week_last_session(day + timedelta(weeks=week))
            if last is not None and last > day:
                return last
    except ValueError:                           # outside the holiday table
        return None
    return None


def model_version() -> str:
    config = {
        "probs": {"Consolidation": scenarios.CONSOLIDATION_P, "Upside": scenarios.UP_P, "Downside": scenarios.DOWN_P},
        "bands": scenarios.scenario_bands(_REFERENCE_LEVELS),
        "target": "week_last_session",
    }
    return ledger.model_version(SELECTION_VERSION, config)


def _probs(snapshot: dict) -> dict:
    return {s["name"]: s["p"] for s in scenarios.build_scenarios(snapshot).get("scenarios", [])}


def freeze_scenarios(snapshot: dict, asof_date: str, *, now=None) -> ledger.Outcome:
    """Freeze ``asof_date``'s scenario probabilities (Fetch only). A refusal is a note, never an error."""
    day = date.fromisoformat(asof_date)
    target = week_target(day)
    probs = _probs(snapshot)
    problem = None
    if scenarios.scenario_bands(snapshot) is None or not probs:
        problem = "the scenario set has no bands (a core level is missing)"
    elif target is None:
        problem = f"the market calendar cannot place the week after {day}"
    else:
        try:
            probs = brier._validated_probs(probs)
        except ValueError as exc:
            problem = str(exc)
    return ledger.freeze_dated(
        SOURCE, LABEL, {"subject": SUBJECT, "probs": probs, "shown_price": snapshot.get("current_level")},
        target_type=TARGET_TYPE, forecast_date=day, target_date=target or day + timedelta(days=7),
        model_version=model_version(), now=now, problem=problem)


def record_fields(snapshot: dict, asof_date: str) -> dict:
    """The S3b fields a new ``brier.json`` record carries: its target session and its numeric bands."""
    target = week_target(date.fromisoformat(asof_date))
    return {"target_date": target.isoformat() if target else None, "bands": scenarios.scenario_bands(snapshot)}


def target_close(day: str) -> float | None:
    """The stored ``^GSPC`` close of ``day`` (D03), or None. Store only, no network."""
    rows = index_levels.levels_between(day, day, symbol=CLOSE_SYMBOL)
    return rows[0]["close"] if rows else None


def _parsed_bands(root: Path, day: str) -> dict | None:
    try:
        snap = json.loads((Path(root) / "parsed" / day / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return scenarios.scenario_bands(snap) if isinstance(snap, dict) else None


def score_due(path: Path, root: Path, close_of=target_close) -> int:
    """Score every unscored ``brier.json`` record whose target close is stored; returns how many were scored.

    Reads the file for display first, so a run with nothing due writes nothing. ``score_date`` writes each score.
    """
    if not Path(path).exists():
        return 0
    scored = 0
    for rec in brier.load_scores(path).get("records", []):
        if rec.get("outcome") is not None or rec.get("brier") is not None:
            continue
        day = rec.get("date")
        target = rec.get("target_date")
        if not target:                           # a record from before S3b
            try:
                derived = week_target(date.fromisoformat(day))
            except (TypeError, ValueError):
                continue
            target = derived.isoformat() if derived else None
        bands = rec.get("bands") or _parsed_bands(root, day)
        if not target or not bands:
            continue
        realized = scenarios.realized_class(bands, close_of(target))
        if realized is None:
            continue
        try:
            brier.score_date(day, realized, path=path)
        except ValueError as exc:                # bad stored probabilities: left unscored, said in the log
            logger.warning("spx brier %s not scored: %s", day, exc)
            continue
        scored += 1
    return scored
