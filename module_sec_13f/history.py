"""SEC 13F board -> result history (#375, A3; avo's card 11: the 13F board metrics, the shares-out / short / insider
inputs and the CUSIP map as it was, kept per quarter).

``record`` is a step of a 13F Fetch (``app/runners.build_sec13f``), after the board file is written. It hands
``core/history.record_run`` one ``sec13f_metrics_history`` row per board row, built from the same entries the board
renders (``report.board_entries``): the latest snapshot's metrics, the shares and quarter-over-quarter changes per
quarter, the snapshot inputs (``enrich.load_inputs``: ``ownership_snapshot`` and D01, #375 F2) and the CUSIPs
mapped to the ticker (``cusip_ticker``). The mapping lives here, in the module, not in ``core/``. A Refresh
(``run_sec_13f_latest`` alone, the dashboard's self-heal included) never calls it (06 rule 9).

13F is a quarter board: every run keeps its rows, and ``sec13f_metrics_by_quarter`` takes each quarter's last run
without an error. ``quarter`` is the board's latest quarter column, the same for every row of a run. A run whose SEC
stage failed or whose derive was refused records ``error`` (the job fails; the board shows the stored totals). A run
whose names and sectors were not refreshed is 'partial' (design C rule E); the CUSIPs left out of the totals are a
header warning, as on the board, and so is a CUSIP map step that did not finish (#207).

The header's ``context["map"]`` is the identity of the CUSIP map the stored totals were derived with (13F-5, #207):
the mapped CUSIPs, their count by source, a hash of the pairs and the CUSIPs and value left out of the newest
quarter (``engine.map_identity``).
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
# 6: the totals are the ownership store's (G9): the newest complete quarter is the snapshot, a quarter still filing is
# left out of it, Short is FINRA's short interest in shares (no float).
SELECTION_VERSION = "6"
_LAST = ("shares", "value", "holders", "top10_pct", "hhi", "new_holders", "exited_holders", "buy_flow", "sell_flow",
         "breadth", "breadth_chg")
_ENTRY = ("name", "sector", "streak", "accel", "pct_out", "price", "price_delta", "shares_outstanding",
          "float_shares", "short_interest", "days_to_cover", "insider_pct")


def effective_config() -> dict:
    """The settings the totals are derived and the board's quarters chosen with."""
    return {"START_QUARTER": engine.START_QUARTER, "SHRINK_MIN_TICKERS": engine._SHRINK_MIN_TICKERS,
            "SHRINK_RATIO": engine._SHRINK_RATIO, "VALUE_RATIO": engine._VALUE_RATIO,
            "UNMATCHED_RISE": engine._UNMATCHED_RISE, "FALLBACK_STRICT": engine._FALLBACK_STRICT}


def model_version() -> str:
    return ledger.model_version(SELECTION_VERSION, effective_config())


def cusips_by_ticker(cusip_ticker: dict) -> dict[str, list[str]]:
    """``{ticker: [cusip, ...]}`` from the stored CUSIP map, read the way ``compute_totals`` reads it."""
    out: dict[str, list[str]] = {}
    for cusip, info in cusip_ticker.items():
        ticker = engine.ticker_of(info)
        if ticker:
            out.setdefault(ticker, []).append(cusip)
    return {t: sorted(c) for t, c in out.items()}


def metric_rows(totals: dict, meta: dict, enrichment: dict | None, cusip_ticker: dict) -> tuple[str | None, list]:
    """``(quarter, rows)``: one row per board row, in board order; ``quarter`` is the board's snapshot quarter, the
    newest complete one (the newest quarter when none is complete).

    shortcut: ``cusips`` still come from the old stored map (``engine.load_cusip_ticker``, empty since the 27 Oct
    purge); the ownership store serves a CUSIP count per row, not the list. Upgrade when G10 retires the old map."""
    quarters, entries = report.board_entries(totals, meta, enrichment)
    filing = report.filing_quarters(totals)
    done = [q for q in quarters if q not in filing]
    quarter = (done or quarters)[-1] if quarters else None
    cusips = cusips_by_ticker(cusip_ticker)
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
           unmatched: str | None = None, ingested: int = 0, map_identity: dict | None = None,
           map_note: str | None = None):
    """Record a 13F Fetch: a header for every Fetch, with the board's rows. ``shown`` holds the ``totals``, ``meta``
    and ``enrichment`` the board was rendered from (``app/runners.run_sec_13f_latest``'s capture). ``error`` is the
    SEC stage's or the derive's failure, ``names_note`` the board's names-not-refreshed note, ``unmatched`` its
    CUSIPs-left-out note, ``map_identity`` the stored map's identity (``engine.map_identity``) and ``map_note`` the
    map step's note when it did not finish. Returns rows written."""
    totals = shown.get("totals") or {}
    cusip_ticker = engine.load_cusip_ticker()
    quarter, rows = metric_rows(totals, shown.get("meta") or {}, shown.get("enrichment"), cusip_ticker)
    problems = [p for p in (names_note, None if totals else "no 13F ownership totals are stored") if p]
    warnings = [w for w in (unmatched, map_note) if w]
    return history.record_run(
        BOARD, run_id=run_id, run_at=run_at, model_version=model_version(), rows={TABLE: rows},
        params=effective_config(),
        context={"quarter": quarter, "tickers": len(rows), "ingested": ingested,
                 "map": map_identity if map_identity is not None else engine.map_identity(cusip_ticker)},
        warnings=warnings or None, partial="; ".join(problems) or None, error=error)
