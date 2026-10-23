"""Co-movement board -> result history (#379 D2; avo's card D2 "Kolon + header").

A Co-movement run already stays in raw.duckdb: ``comov_runs`` and its node, member and lag rows, one dated run per
``as_of``, never pruned. History keeps no second copy of them (card D2, option a). Instead each ``comov_runs`` row
carries the Fetch's ``run_id`` and its ``session_date``, and ``record`` writes ONE header-only ``result_runs`` row per
Fetch that names every run the Fetch stored or served: its own window plus the precompute windows (#95).

``record`` is the last step of a Co-movement Fetch (``app/runners.run_comovement``, after ``engine.precompute``). It is
never called from ``precompute`` itself, from the store-only recompute after a Settings save, or from a Refresh
(06 rule 9). The mapping lives here, in the module, not in ``core/``.

Co-movement is a run board with no history tables of its own (``core.history.tables_of`` returns none), so every
Fetch keeps its header. A Fetch that clustered too few names stores no run; its header is kept as 'partial' with the
reason. A failed precompute is a warning on the header: the Fetch's own run is whole.
"""
from __future__ import annotations

import time
from datetime import datetime

from RevenueChaser.core import history, ledger
from RevenueChaser.module_comovement import config, noise
from RevenueChaser.module_comovement import settings as settings_mod

BOARD = "module_comovement"
# Bump when the clustering changes in a way the effective config does not see (``returns``, ``noise``,
# ``persistence``, ``label``), not a setting. ``config.ENGINE_EPOCH`` is the engine's own marker for the same.
SELECTION_VERSION = "1"


def effective_config() -> dict:
    """What every run of a Fetch is clustered, judged and labelled with, at the Settings defaults."""
    eff = settings_mod.effective()
    return {
        "LINKAGE": config.LINKAGE, "CUT_TIERS": [[n, list(c)] for n, c in config.CUT_TIERS],
        "CATCHALL_SHARE": config.CATCHALL_SHARE, "PERSIST_MIN_JACCARD": config.PERSIST_MIN_JACCARD,
        "LABEL_MIN_N": config.LABEL_MIN_N, "LABEL_Q_MAX": config.LABEL_Q_MAX, "LABEL_MIN_HITS": config.LABEL_MIN_HITS,
        "LABEL_MIN_ENRICH_SHARE": config.LABEL_MIN_ENRICH_SHARE, "LABEL_MIN_LIFT": config.LABEL_MIN_LIFT,
        "FLAT_RUN": config.FLAT_RUN, "FLAT_SHARE": config.FLAT_SHARE, "NULL_PERMS": noise.NULL_PERMS,
        "PRECOMPUTE_WINDOWS": list(config.PRECOMPUTE_WINDOWS), "ENGINE_EPOCH": config.ENGINE_EPOCH,
        "settings": {k: v for k, v in eff.payload().items() if k != "window_options"},
    }


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


def _run_at(payload: dict) -> float:
    """The Fetch run's own time: its ``as_of`` (the ``comov_runs`` key), so the header's session is the run's."""
    try:
        return datetime.fromisoformat(str(payload.get("as_of"))).timestamp()
    except (TypeError, ValueError):
        return time.time()


def record(payload: dict, *, run_id: str, as_ofs: dict[int, str] | None = None,
           precompute: dict[int, str] | None = None, warnings: list[str] | None = None):
    """Record a finished Co-movement Fetch as one header with no rows.

    ``payload`` is the board the Fetch wrote; ``as_ofs`` maps each precompute window to the ``as_of`` it stored or
    served (``engine.precompute``); ``precompute`` is its status per window. The header's ``params`` list every
    ``as_of`` with its window, the Fetch window first. Returns rows written (none)."""
    runs: dict[int, str] = {}
    if payload.get("config_hash"):
        runs[int(payload["window"])] = payload["as_of"]
    for w, a in (as_ofs or {}).items():
        runs.setdefault(int(w), a)
    partial = None
    if not payload.get("config_hash"):
        partial = (f"the Fetch clustered {payload.get('eligible') or 0} names (3 needed), so no run was stored")
    return history.record_run(
        BOARD, run_id=run_id, run_at=_run_at(payload), model_version=model_version(), rows={},
        params={"as_of": list(runs.values()), "windows": list(runs), "config_hash": payload.get("config_hash"),
                "horizon": payload.get("horizon"), "benchmark": payload.get("benchmark"),
                "settings": payload.get("settings")},
        context={"prices_through": payload.get("prices_through"), "window_end": payload.get("window_end"),
                 "eligible": payload.get("eligible"), "universe_total": payload.get("universe_total"),
                 "n_factors": payload.get("n_factors"), "benchmark_status": payload.get("benchmark_status"),
                 "fetch": payload.get("fetch"),
                 "precompute": {str(k): v for k, v in (precompute or {}).items()}},
        funnel=payload.get("funnel") or None, warnings=list(warnings or []) or None, partial=partial,
        provider_health=payload.get("provider_health"))
