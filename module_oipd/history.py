"""Implied Probability (OIPD) board -> result history (#382, A3; avo's card 6: every fitted distribution and the chain
it used, kept per run).

``record`` is a step of an OIPD Fetch (``app/runners.run_oipd``), after the board file is written. It hands
``core/history.record_run`` two row sets for the run's ticker:

- ``oipd_chain_history``: the stored chain the fit used (G9: the options store's ``option_eod`` of the name's newest
  session), numbered by ``row_no``.
- ``oipd_fit_history``: one row per fitted expiry: the fit's inputs and statistics, its ATM greeks, the SVI
  parameters, and the curve and smile the board draws (compressed).

The mapping lives here, in the module, not in ``core/``. A Refresh (``run_oipd_cached`` and ``no_fetch``) never
calls it (06 rule 9).

OIPD is a run board: every run keeps its rows, and a degraded run is kept too, marked 'partial' with the reason in
``quality_note`` (design C rule E). The reason is run-level: the name is not on the option watch list, the store has
no chain for it yet, or no expiry could be fitted. An expiry that is too thin to fit is a board warning, not a
reason.
"""
from __future__ import annotations

import json
from collections import Counter

from RevenueChaser.core import history, ledger
from RevenueChaser.module_oipd import config

BOARD = "module_oipd"
CHAIN, FITS = "oipd_chain_history", "oipd_fit_history"
# Bump when the fit changes in a way the config hash does not see (``opd_math``'s SVI or density formulas, the
# forward inference), not a setting.
# 2: G9, the chain is the options store's (``option_eod``), not a UW pull; the paging settings are gone.
SELECTION_VERSION = "2"
_QUANTILES = ("p05", "p10", "p25", "median", "p75", "p90", "p95")
_SPOT_PROBS = ("prob_below_spot", "prob_above_spot", "p_up10", "p_down10")
# The density verdict the board shows per expiry (#172 D2: opd_math.density_check over the density window).
_DENSITY = ("g_min_window", "clipped_pct", "min_var", "density_flag", "density_note", "density_g_min_shown")


def effective_config() -> dict:
    """The settings the stored chain is picked and the surface fitted with."""
    return {
        "RISK_FREE_RATE": config.RISK_FREE_RATE, "MIN_DTE": config.MIN_DTE, "MAX_DTE": config.MAX_DTE,
        "MAX_EXPIRIES": config.MAX_EXPIRIES, "MIN_CONTRACTS": config.MIN_CONTRACTS,
        "G_FLOOR": config.G_FLOOR,
        "CLIP_FLAG_PCT": config.CLIP_FLAG_PCT, "PENALTY_WEIGHT": config.PENALTY_WEIGHT,
    }


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


def _plain(value):
    """JSON-safe copy: dates and other non-JSON values become strings."""
    return json.loads(json.dumps(value, default=str))


def fit_rows(payload: dict, capture: dict) -> list[dict]:
    """One ``oipd_fit_history`` row per fitted expiry of the payload."""
    per_expiry = Counter(r.get("expiry") for r in capture.get("chain") or [])
    svi = capture.get("svi") or {}
    out = []
    for e in payload.get("expiries") or []:
        quantiles = e.get("quantiles") or {}
        out.append({
            "ticker": payload.get("ticker"), "expiry": e["date"], "as_of": payload.get("asof"),
            "spot": payload.get("spot"), "risk_free": payload.get("risk_free"),
            "horizon_days": payload.get("horizon_days"), "n_contracts": per_expiry.get(e["date"], 0),
            "dte": e.get("dte"), "t_years": e.get("T"), "forward": e.get("forward"), "n_strikes": e.get("n"),
            "atm_iv": e.get("atm_iv"), "g_min": e.get("g_min"), **{d: e.get(d) for d in _DENSITY},
            "rmse": e.get("rmse"), "mean": e.get("mean"),
            **{q: quantiles.get(q) for q in _QUANTILES}, **{p: e.get(p) for p in _SPOT_PROBS},
            "greeks": _plain(e.get("greeks")), "svi": svi.get(e["date"]),
            "curve": _plain(e.get("curve")), "smile": _plain(e.get("smile")),
        })
    return out


def _problems(payload: dict, uw_health: dict | None) -> list[str]:
    """Why this run's fit is not whole (empty when it is)."""
    return ([payload["error"]] if payload.get("error") else []) + history.uw_problems(uw_health)


def record(payload: dict, capture: dict, *, run_id: str, run_at: float, uw_health: dict | None = None):
    """Record a finished OIPD Fetch: a header for every Fetch, with its chain and fit rows; a degraded run is
    partial. ``capture`` is the dict ``engine.run`` filled. Returns rows written per table."""
    capture = capture or {}
    meta = capture.get("meta") or {}
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(),
        rows={CHAIN: capture.get("chain") or [], FITS: fit_rows(payload, capture)}, params=effective_config(),
        context={"ticker": payload.get("ticker"), "as_of": payload.get("asof"), "spot": payload.get("spot"),
                 "fetched_at": meta.get("fetched_at"), "n_contracts": payload.get("n_contracts"),
                 "expiries_fitted": len(payload.get("expiries") or []), "chain_status": capture.get("status"),
                 "source": payload.get("source")},
        warnings=list(payload.get("warnings") or []) or None,
        partial="; ".join(_problems(payload, uw_health)) or None, provider_health=uw_health)
