"""Financials board -> result history (#385, A3 first writer; avo's card 12: the Financials numbers kept per run).

``record`` is a step of a Financials Fetch (``app/runners.run_quick``), after the board file is written. It hands
``core/history.record_run`` one ``financials_report_history`` row for the report's ticker: the sector, the modules
that ran, the comps peers, the assumptions each model used, when the data was fetched, the warnings, Quick Remarks
and every module's numbers (``modules``, compressed). The mapping lives here, in the module, not in ``core/``. A
Refresh (``run_quick_cached``) never calls it (06 rule 9).

``last_report`` is the other side of that row (FIN-4, #385): the Refresh's way back to what its last Fetch computed.
It opens the history file read-only and writes nothing, and a store with no recorded run reads as ``None``.

Financials is a run board: every run keeps its row, and a degraded run is kept too, marked 'partial' with the reason
in ``quality_note`` (design C rule E). The reasons, all run-level: a module that could not run (its ``error``), a
report the board did not get (the copy to ``financials_latest.html`` failed), or a run that returned no report.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from RevenueChaser.core import history, ledger, rawstore
from RevenueChaser.module_financials import config
from RevenueChaser.module_financials.engines import credit_capacity_engine as credit, sensitivity_engine as sens

BOARD, TABLE = "module_financials", "financials_report_history"
# Bump when a model changes in a way the config hash does not see (a formula, not a default).
SELECTION_VERSION = "1"


def effective_config() -> dict:
    """The defaults every model falls back to when the caller passes no assumptions (the dashboard passes none),
    plus the credit thresholds and the sensitivity axes, which shape the stored numbers too."""
    return {
        "RISK_FREE_RATE": config.RISK_FREE_RATE, "EQUITY_RISK_PREMIUM": config.EQUITY_RISK_PREMIUM,
        "DEFAULT_BETA": config.DEFAULT_BETA, "DEFAULT_TAX_RATE": config.DEFAULT_TAX_RATE,
        "DEFAULT_COST_OF_DEBT": config.DEFAULT_COST_OF_DEBT, "TERMINAL_GROWTH": config.TERMINAL_GROWTH,
        "DEFAULT_EXIT_MULTIPLE": config.DEFAULT_EXIT_MULTIPLE, "PROJECTION_YEARS": config.PROJECTION_YEARS,
        "DEFAULT_REVENUE_GROWTH": list(config.DEFAULT_REVENUE_GROWTH),
        "DEFAULT_EBITDA_MARGIN": config.DEFAULT_EBITDA_MARGIN, "DEFAULT_CAPEX_PCT_REV": config.DEFAULT_CAPEX_PCT_REV,
        "DEFAULT_DNWC_PCT_REV": config.DEFAULT_DNWC_PCT_REV, "PEER_K": config.PEER_K,
        "SECTOR_DEFAULTS": config.load_sector_defaults(),
        "DEBT_EBITDA_SAFE": credit.DEBT_EBITDA_SAFE, "DEBT_EBITDA_STRESSED": credit.DEBT_EBITDA_STRESSED,
        "INTEREST_COVERAGE_SAFE": credit.INTEREST_COVERAGE_SAFE,
        "INTEREST_COVERAGE_STRESSED": credit.INTEREST_COVERAGE_STRESSED,
        "WACC_DELTAS": list(sens.WACC_DELTAS), "GROWTH_DELTAS": list(sens.GROWTH_DELTAS),
    }


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


def _plain(value):
    """JSON-safe copy: dates and other non-JSON values become strings."""
    return json.loads(json.dumps(value, default=str))


def _row(ticker: str, results: dict) -> dict:
    financials = results.get("financials") or {}
    modules = results.get("modules") or {}
    comps = modules.get("comps") or {}
    return {
        "ticker": ticker, "sector": financials.get("sector"), "modules_run": list(modules),
        "peers": [p.get("ticker") for p in comps.get("peers") or []],
        "assumptions": _plain({m: r["assumptions"] for m, r in modules.items()
                               if isinstance(r, dict) and r.get("assumptions")}),
        "data_fetched_at": financials.get("fetched_at_iso"), "warnings": _plain(results.get("warnings") or []),
        "quick_remarks": _plain(results.get("quick_remarks")), "modules": _plain(modules),
    }


def _problems(results: dict, report_written: bool) -> list[str]:
    """Why this run's report is not whole (empty when it is)."""
    out = [f"{m}: {r['error']}" for m, r in (results.get("modules") or {}).items()
           if isinstance(r, dict) and r.get("error")]
    if not report_written:
        out.append("the report file was not written, so the board does not show this run")
    return out


def record(results: dict, *, ticker: str, run_id: str, run_at: float, modules, report_written: bool = True):
    """Record a finished Financials Fetch: a header for every Fetch, with its report row; a degraded run is partial.

    ``results`` is ``module_financials.run.run(...)["results"]``; ``modules`` the modules the Fetch asked for.
    Returns rows written."""
    rows = [_row(ticker, results)] if results else []
    problems = _problems(results, report_written)
    note = "; ".join(["the run returned no report", *problems]) if not rows else "; ".join(problems) or None
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(), rows={TABLE: rows},
        params={"modules": list(modules), "defaults": effective_config()},
        context={"ticker": ticker, "sector": rows[0]["sector"] if rows else None}, partial=note)


def _as_of(data_fetched_at, run_at) -> str | None:
    """The date the run's data was fetched: the payload's stamp, else the run's own date (UTC)."""
    text = str(data_fetched_at or "").strip()
    if len(text) >= 10:
        try:
            return datetime.fromisoformat(text).date().isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(float(run_at), timezone.utc).date().isoformat() if run_at else None


def last_report(ticker: str) -> dict | None:
    """The newest recorded Fetch's report for ``ticker``, decoded: ``modules`` (comps included), ``warnings``,
    ``quick_remarks``, ``sector``, ``run_id`` and ``as_of`` (the date its data was fetched).

    Read-only: ``rawstore.query`` opens the history file read-only and writes nothing; a store with no recorded run
    (no history file at all) reads as ``None``. This is how a Refresh renders the comps its last Fetch computed,
    without a network call (FIN-4, #385)."""
    rows = rawstore.query(
        "SELECT h.run_id, h.ticker, h.sector, h.modules_run, h.peers, h.assumptions, h.data_fetched_at, "
        "h.warnings, h.quick_remarks, h.modules, r.run_at "
        f"FROM {TABLE} h JOIN result_runs r ON r.board = ? AND r.run_id = h.run_id "
        "WHERE h.ticker = ? ORDER BY r.run_at DESC, h.run_id DESC LIMIT 1",
        [BOARD, (ticker or "").strip().upper()], db="history")
    if not rows:
        return None
    report = rawstore._decode_row(TABLE, rows[0])
    report["as_of"] = _as_of(report.get("data_fetched_at"), report.get("run_at"))
    return report
