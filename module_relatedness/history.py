"""Relatedness board -> result history (#388, A3; avo's card 10: the peer scores kept per run).

``record`` is a step of a Relatedness Fetch (``engine._run``, so the dashboard Fetch and the off-peak CLI run
``RC-Relatedness-OffPeak`` both record), after the latest table (``relatedness_scores``) and the run marker are
written. It hands ``core/history.record_run`` the same rows the latest table got (``relatedness_scores_history`` is
its mirror, from the shared ``store.score_rows``) and the board payload (``result_runs.payload``, compressed: the
companies, their profiles and every peer list). The mapping lives here, in the module, not in ``core/``. A Refresh
(``no_fetch``) never calls it (06 rule 9).

Relatedness is a run board: every run keeps its rows, and a degraded run is kept too, marked 'partial' with the
reason in ``quality_note`` (design C rule E). The reasons, all run-level: the run stopped at the DeepSeek peak
boundary (the rest of the pilot waits for the next off-peak run), or it scored no pair, so ``relatedness_scores``
kept its stored rows. Tickers still without a usable profile (no filing text yet) are counted in the context, not a
reason: they are the normal state of the pilot.
"""
from __future__ import annotations

from RevenueChaser.core import history, ledger
from RevenueChaser.core.providers import deepseek
from RevenueChaser.module_relatedness import config, store

BOARD, TABLE = "module_relatedness", "relatedness_scores_history"
# Bump when the similarity changes in a way the config hash does not see (``similarity``'s formula, the percentile),
# not a setting. 2: the C12 percentile and the C14 cost cap change what a run selects.
SELECTION_VERSION = "2"


def effective_config() -> dict:
    """What the profiles are built with and the peers scored with."""
    return {
        "MODEL": deepseek.MODEL, "THINKING": config.THINKING, "REASONING_EFFORT": config.REASONING_EFFORT,
        "PROMPT_VERSION": config.PROMPT_VERSION, "PILOT_N": config.PILOT_N,
        "MAX_INPUT_CHARS": config.MAX_INPUT_CHARS, "PROFILE_FIELDS": list(config.PROFILE_FIELDS),
        "FIELD_WEIGHTS": dict(config.FIELD_WEIGHTS), "TOP_PEERS": config.TOP_PEERS,
    }


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


def _problems(payload: dict, rows: list[dict]) -> list[str]:
    """Why this run is not whole (empty when it is)."""
    out = []
    if payload.get("next_off_peak"):
        out.append(f"the run stopped at the DeepSeek peak boundary; {payload.get('pending', 0)} tickers wait for "
                   f"the next off-peak run ({payload['next_off_peak']})")
    if payload.get("stopped_reason") == "allowance":
        out.append(f"the run stopped at the SuperGrok allowance; {payload.get('pending', 0)} tickers wait for the "
                   f"next run (allowance resets {payload.get('next_reset') or 'unknown'})")
    if payload.get("stopped_reason") in ("auth", "transient", "request", "unusable"):
        out.append(f"the run stopped early ({payload['stopped_reason']}); {payload.get('pending', 0)} tickers wait "
                   "for the next run")
    if not rows:
        out.append("the run scored no pair, so relatedness_scores kept its stored rows")
    return out


def record(payload: dict, *, requested: int, run_id: str, run_at: float):
    """Record a finished Relatedness Fetch: a header for every Fetch, with its score rows and payload; a degraded run
    is partial. ``requested`` is how many tickers the run was asked to profile. Returns rows written."""
    rows = store.score_rows(payload.get("peers") or {})
    companies = payload.get("companies") or []
    context = {"as_of": payload.get("as_of"), "requested": requested, "profiled": payload.get("n"),
               "pending": payload.get("pending"), "no_text": payload.get("no_text"), "stale": sum(1 for c in companies if c.get("stale")),
               "n_pairs": payload.get("n_pairs"), "universe_total": payload.get("universe_total"),
               "next_off_peak": payload.get("next_off_peak")}
    if payload.get("writer"):
        # A Grok Fetch (#388) names its writer; ``params`` / ``model_version`` stay the profile mode's (DeepSeek).
        context["writer"] = payload["writer"]
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(), rows={TABLE: rows},
        params=effective_config(),
        context=context,
        payload=_without_catalog(payload), partial="; ".join(_problems(payload, rows)) or None)


def _without_catalog(payload: dict | None) -> dict | None:
    """The payload as history keeps it: without ``theme_catalog``, the Theme facet's vocabulary, which the board reads
    from the pinned theme run at render time (#383 C1b) and is not part of the Fetch's result."""
    if payload is None:
        return None
    return {k: v for k, v in payload.items() if k != "theme_catalog"}
