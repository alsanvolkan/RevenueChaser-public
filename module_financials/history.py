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
from datetime import date, datetime, timezone

from RevenueChaser.core import calendar as market_calendar, history, ledger, rawstore
from RevenueChaser.module_financials import config
from RevenueChaser.module_financials.engines import _inputs
from RevenueChaser.module_financials.engines import credit_capacity_engine as credit, sensitivity_engine as sens

BOARD, TABLE = "module_financials", "financials_report_history"
# Bump when a model changes in a way the config hash does not see (a formula, not a default).
SELECTION_VERSION = "2"


def effective_config() -> dict:
    """The defaults every model falls back to when the caller passes no assumptions (the dashboard passes none),
    plus the credit thresholds, the tax band and the sensitivity axes, which shape the stored numbers too."""
    return {
        "RISK_FREE_RATE": config.RISK_FREE_RATE, "EQUITY_RISK_PREMIUM": config.EQUITY_RISK_PREMIUM,
        "DEFAULT_BETA": config.DEFAULT_BETA, "DEFAULT_TAX_RATE": config.DEFAULT_TAX_RATE,
        "DEFAULT_COST_OF_DEBT": config.DEFAULT_COST_OF_DEBT, "TERMINAL_GROWTH": config.TERMINAL_GROWTH,
        "DEFAULT_EXIT_MULTIPLE": config.DEFAULT_EXIT_MULTIPLE, "PROJECTION_YEARS": config.PROJECTION_YEARS,
        "DEFAULT_REVENUE_GROWTH": list(config.DEFAULT_REVENUE_GROWTH),
        "DEFAULT_EBITDA_MARGIN": config.DEFAULT_EBITDA_MARGIN, "DEFAULT_CAPEX_PCT_REV": config.DEFAULT_CAPEX_PCT_REV,
        "DEFAULT_DNWC_PCT_REV": config.DEFAULT_DNWC_PCT_REV, "PEER_K": config.PEER_K,
        "SECTOR_DEFAULTS": config.load_sector_defaults(),
        "MEASURED_TAX_RATE_BAND": list(_inputs.MEASURED_TAX_RATE_BAND),
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


def _ny_date(moment: datetime) -> date:
    """The New York calendar date of ``moment``; a naive one is taken as it reads (it carries no zone to move)."""
    return (moment if moment.tzinfo is None else moment.astimezone(market_calendar.NY)).date()


def _as_of(data_fetched_at, run_at) -> str | None:
    """The date the run's data was fetched: the payload's stamp, else the run's own moment — read in New York.

    Both are instants, and a run after 20:00 ET is already the NEXT day in UTC: read as UTC, a Tuesday-evening
    Fetch was labelled Wednesday, the following calendar day (a day the market may not even have traded). The
    board's "as of" is the market's own day, so the instant is read in ``America/New_York``, through the zone
    ``core/calendar`` owns (never a fixed offset: the US and EU change on different weekends).
    """
    text = str(data_fetched_at or "").strip()
    if len(text) >= 10:
        try:
            return _ny_date(datetime.fromisoformat(text)).isoformat()
        except ValueError:
            pass
    return _ny_date(datetime.fromtimestamp(float(run_at), timezone.utc)).isoformat() if run_at else None


def has_comps(report: dict | None) -> bool:
    """True when a recorded report carries comps a Fetch computed: a ``comps`` module that ran without an error.

    The encodings a run leaves in ``modules``: ``comps_engine.run_comps`` returns ``{"error": ...}`` when the step
    could not run, a Fetch that did not ask for comps has no ``comps`` key at all, and the Refresh's own empty state
    (``not_stored``) is never recorded (``run_quick_cached`` writes no history row)."""
    comps = ((report or {}).get("modules") or {}).get("comps")
    return isinstance(comps, dict) and bool(comps) and not comps.get("error")


def last_report(ticker: str) -> dict | None:
    """The newest recorded Fetch's report for ``ticker`` that has comps, decoded: ``modules`` (comps included),
    ``warnings``, ``quick_remarks``, ``sector``, ``run_id`` and ``as_of`` (the date its data was fetched).

    A run whose comps step errored must not hide the comps an older run did compute (FIN-4, #385), so the rows are
    walked newest first and the first with usable comps wins (:func:`has_comps`); when no stored run has any, the
    newest row is returned as it is, which the board renders as its empty state.

    The walk is bounded and lazy: the SELECT takes the newest 20 runs of this ticker (``LIMIT 20``, one joined
    row per run), and each is decoded only when the walk reaches it, so a ticker with a long run history neither
    scans it all nor decodes rows it never looks at — this runs on the Refresh thread, which holds the DuckDB
    writer.

    Read-only: ``rawstore.query`` opens the history file read-only and writes nothing; a store with no recorded run
    (no history file at all) reads as ``None``. This is how a Refresh renders the comps its last Fetch computed,
    without a network call (FIN-4, #385)."""
    rows = rawstore.query(
        "SELECT h.run_id, h.ticker, h.sector, h.modules_run, h.peers, h.assumptions, h.data_fetched_at, "
        "h.warnings, h.quick_remarks, h.modules, r.run_at "
        f"FROM {TABLE} h JOIN result_runs r ON r.board = ? AND r.run_id = h.run_id "
        "WHERE h.ticker = ? ORDER BY r.run_at DESC, h.run_id DESC LIMIT 20",
        [BOARD, (ticker or "").strip().upper()], db="history")
    if not rows:
        return None
    report = None
    newest = None
    for row in rows:
        decoded = rawstore._decode_row(TABLE, row)
        if newest is None:
            newest = decoded
        if has_comps(decoded):
            report = decoded
            break
    report = report or newest
    report["as_of"] = _as_of(report.get("data_fetched_at"), report.get("run_at"))
    return report
