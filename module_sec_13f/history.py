"""SEC 13F board -> result history (#375, A3; avo's card 11: the 13F board metrics, the shares-out / short / insider
inputs and the CUSIP map as it was, kept per quarter).

``record`` is a step of a 13F Fetch (``app/runners.build_sec13f``), after the board file is written. It hands
``core/history.record_run`` one ``sec13f_metrics_history`` row per board row, built from the same entries the board
renders (``report.board_entries``): the latest snapshot's metrics, the shares and quarter-over-quarter changes per
quarter, the snapshot inputs (``enrich.load_inputs``: ``ownership_snapshot`` and D01, #375 F2) and the CUSIPs
mapped to the ticker (``cusip_ticker``). The mapping lives here, in the module, not in ``core/``. A Refresh
(``run_sec_13f_latest`` alone, the dashboard's self-heal included) never calls it (06 rule 9).

13F is a quarter board: every run keeps its rows, and ``sec13f_metrics_by_quarter`` takes each quarter's last run
without an error. ``quarter`` is the board's latest quarter column, the same for every row of a run. A run whose
names and sectors were not refreshed is 'partial' (design C rule E); the CUSIPs the newest quarter could not name
are a header warning, as on the board.

The numbers and the CUSIP map are the builder's since G9, so a run records what the board showed, not how it was
derived: the derive's own guards and map identity went with the old writers.
"""
from __future__ import annotations

from RevenueChaser.core import history, ledger
from RevenueChaser.module_sec_13f import engine, report

BOARD, TABLE = "module_sec_13f", "sec13f_metrics_history"
# Bump when the metrics change in a way the config hash does not see (``compute_totals``' SQL, ``board_entries``'
# streak / accel / % out formulas, the map's source), not a setting. 2: the name match is retired, the map is
# OpenFIGI's (#207). 3: plus the legacy entries whose issuer prefix OpenFIGI confirms, 9x issues out (#207).
# 4: shares out, insider % and price come from ownership_snapshot and D01, not sec13f_enrichment (#375 F2).
# 5: plus ownership breadth, holders ÷ that quarter's filers, and its change (#375 F4, Chen-Hong-Stein 2002).
# 6: the metrics and the CUSIP map come from the ownership serving store, not the retired derive (G9).
SELECTION_VERSION = "6"
_LAST = ("shares", "value", "holders", "top10_pct", "hhi", "new_holders", "exited_holders", "buy_flow", "sell_flow",
         "breadth", "breadth_chg")
_ENTRY = ("name", "sector", "streak", "accel", "pct_out", "price", "price_delta", "shares_outstanding",
          "float_shares", "short_pct", "insider_pct")


def effective_config() -> dict:
    """The settings the board's quarters are chosen with. The totals' own rules are the builder's (G9), and its
    serving schema version says when they change."""
    from RevenueChaser.rcdata import serving
    return {"START_QUARTER": engine.START_QUARTER,
            "OWNERSHIP_SERVING_SCHEMA": serving.SERVING_SCHEMA_VERSION["ownership"]}


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


def cusips_by_ticker(quarter: str | None) -> dict[str, list[str]]:
    """``{ticker: [cusip, ...]}`` of ``quarter``, from the builder's CUSIP map (``sec13f_cusip_map``, G9)."""
    if not quarter:
        return {}
    from RevenueChaser.rcdata import read as rcread
    out: dict[str, list[str]] = {}
    for r in rcread.cusip_map_13f([quarter]):
        out.setdefault(r["ticker"], []).append(r["cusip"])
    return {t: sorted(c) for t, c in out.items()}


def metric_rows(totals: dict, meta: dict, enrichment: dict | None) -> tuple[str | None, list]:
    """``(quarter, rows)``: one row per board row, in board order; ``quarter`` is the board's latest quarter."""
    quarters, entries = report.board_entries(totals, meta, enrichment)
    quarter = quarters[-1] if quarters else None
    cusips = cusips_by_ticker(quarter)
    rows = []
    for e in entries:
        last = e["last"] or {}
        rows.append({
            "ticker": e["ticker"], "quarter": quarter, **{c: last.get(c) for c in _LAST},
            **{c: e[c] for c in _ENTRY},
            "shares_by_quarter": dict(zip(quarters, e["shares"])),
            "chg_by_quarter": dict(zip(quarters[1:], e["chgs"][1:])),
            "cusips": cusips.get(e["ticker"], []),
        })
    return quarter, rows


def record(shown: dict, *, run_id: str, run_at: float, error: str | None = None, names_note: str | None = None,
           unmatched: str | None = None):
    """Record a 13F Fetch: a header for every Fetch, with the board's rows. ``shown`` holds the ``totals``, ``meta``
    and ``enrichment`` the board was rendered from (``app/runners.run_sec_13f_latest``'s capture). ``names_note`` is
    the board's names-not-refreshed note and ``unmatched`` its CUSIPs-left-out note. Returns rows written."""
    totals = shown.get("totals") or {}
    quarter, rows = metric_rows(totals, shown.get("meta") or {}, shown.get("enrichment"))
    problems = [p for p in (names_note, None if totals else "no 13F ownership totals are stored") if p]
    warnings = [w for w in (unmatched,) if w]
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(), rows={TABLE: rows},
        params=effective_config(),
        context={"quarter": quarter, "tickers": len(rows)},
        warnings=warnings or None, partial="; ".join(problems) or None, error=error)
