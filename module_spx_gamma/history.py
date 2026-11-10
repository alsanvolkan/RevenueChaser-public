"""SPX Gamma board -> result history (SPX Gamma review 9 Nov 2026, PR 3; #427 item 4).

``record`` is the last step of an SPX Gamma Fetch (``app/runners.run_spxgamma``), after the board file and the
report are written. It writes one ``result_runs`` header through ``core/history.record_run`` (the whole snapshot as
its compressed payload) and one ``spx_gamma_levels_history`` row: the levels, the prediction and the three scenario
probabilities the board showed. Never called from a Refresh or the stored-day job (06 rule 9, F2).

SPX Gamma is a session board: the first complete run of a session keeps its row. A run is partial, so it keeps its
header only, when Unusual Whales calls failed, or when the day it built is not the session it ran in (a Fetch of an
older day, or a fallback to the previous session): that day's levels must not stand for another session.
"""
from __future__ import annotations

import json

from RevenueChaser.core import calendar, history, ledger

from .scenarios import build_scenarios

BOARD, TABLE = "module_spx_gamma", "spx_gamma_levels_history"
# Bump when the levels, the prediction or the scenarios change in a way the stored row would not show.
SELECTION_VERSION = "1"


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, {"methodology_version": "1.0", "table": TABLE})


def levels_row(snapshot: dict, asof_date: str) -> dict:
    """The one row a Fetch keeps: what the board's header, levels panel and scenario table showed."""
    levels = snapshot.get("levels") or {}
    pred = snapshot.get("prediction") or {}
    charm = snapshot.get("charm_qualification") or {}
    probs = {s.get("name"): s.get("p") for s in build_scenarios(snapshot).get("scenarios") or []}
    return {
        "asof_date": asof_date, "spot": snapshot.get("current_level"), "regime": snapshot.get("regime"),
        "net_gex": snapshot.get("net_gamma_total"), "gamma_flip": levels.get("regime_threshold"),
        "call_wall": levels.get("upper_resistance_band"), "put_wall": levels.get("lower_support_band"),
        "pin_magnet": pred.get("pin_magnet"), "pin_flag": pred.get("pin_flag"),
        "charm_label": charm.get("label"), "charm_strike": charm.get("strike"),
        "expected_move": (snapshot.get("expected_move") or {}).get("points"),
        "s_dir": pred.get("s_dir"), "bias": pred.get("bias"),
        "p_consolidation": probs.get("Consolidation"), "p_upside": probs.get("Upside"),
        "p_downside": probs.get("Downside"),
    }


def _problems(asof_date: str, run_at: float, uw_health: dict | None) -> list[str]:
    out = history.uw_problems(uw_health)
    try:
        session = calendar.session_for(run_at).session_date
    except ValueError:
        return out                                  # record_run marks a run with no session itself
    if str(session) != asof_date:
        out.append(f"the Fetch built {asof_date}, not the session {session} it ran in")
    return out


def record(out: dict, *, run_id: str, run_at: float, uw_health: dict | None = None,
           warnings: list | None = None):
    """Record a finished SPX Gamma Fetch. ``out`` is what ``run.run`` returned (``snapshot`` and ``latest``).
    Returns rows written per table."""
    snapshot, latest = out["snapshot"], out.get("latest") or {}
    asof_date = latest.get("asof_date") or snapshot.get("asof_date")
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(),
        rows={TABLE: [levels_row(snapshot, asof_date)]},
        params={"asof_date": asof_date, "requested_date": latest.get("requested_date")},
        context={"api_snapshot_time": latest.get("api_snapshot_time"), "input_sources": latest.get("input_sources"),
                 "candle_source": latest.get("candle_source")},
        payload=json.loads(json.dumps(snapshot, default=str)),   # dates and numpy values as plain JSON
        warnings=list(warnings) if warnings else None,
        partial="; ".join(_problems(asof_date, run_at, uw_health)) or None, provider_health=uw_health)
