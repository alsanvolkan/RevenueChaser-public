"""Orkestrasyon: ticker -> Quick Remarks + Fin Model engine'leri -> rapor."""
from __future__ import annotations

import re
from pathlib import Path

from RevenueChaser.core.reporting_common import write_artifact

from .config import OUTPUT_DIR, PEER_K
from .engines import (
    comps_engine,
    credit_capacity_engine,
    dcf_engine,
    quick_remarks_engine,
    sensitivity_engine,
    three_statement_engine,
)
from . import history as fin_history
from .peers import select_peers
from .reporting.html_builder import build_report_html
from RevenueChaser.core import vendor_data
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.core.providers import yfinance as yfinance_client


# An upper-cased symbol such as BRK.B. The ticker becomes a file name (CLI report), so a path piece
# must never get through (#173 D5, #175).
_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,9}")


class _NoCachedData(Exception):
    """No cached payload in .store for the ticker (store-only render)."""

    def __init__(self, ticker: str):
        super().__init__(f"no cached financials for {ticker}")
        self.ticker = ticker


def run(ticker: str, modules: list[str] | tuple[str, ...] = ("dcf",),
        output: str = "html", use_cache: bool = True, no_fetch: bool = False,
        assumptions: dict | None = None, peers: list[str] | None = None,
        report_path: Path | str | None = None, run_id: str | None = None) -> dict:
    """Uçtan uca Fin Research çalıştırır; rapor yolunu ve sonuçları döndürür.

    ``no_fetch=True`` — store-only (model-only) render: reads the cached payload
    from ``.store/raw.duckdb`` (ignoring TTL) and never touches the network.
    When no cached payload exists it raises ``_NoCachedData`` so the caller can
    fall back to an empty state / live fetch.

    The report goes to ``report_path`` when given (the dashboard's single ``financials_latest.html``).
    Without it, ``output="html"`` writes the per-ticker file ``<OUTPUT_DIR>/<TICKER>_report.html``:
    that is the CLI's behaviour and nothing else writes per-ticker files (#173 D5/M18).

    A Fetch stores what it fetched from Yahoo (the ticker's statements and ``.info``, the comps peers' ``.info``)
    in ``vendor_financials`` / ``vendor_valuation`` (#297), stamped ``run_id`` (the job's id; a fresh one without
    it). A cache hit fetched nothing and stores nothing.
    """
    ticker = ticker.upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise ValueError(f"bad ticker: {ticker!r}")
    if no_fetch:
        # FIN-5 (#385): a store-only render's model inputs come from the store, with zero network. The statements
        # are the rows its last Fetch stored (``vendor_financials``); the valuation is the raw cached payload's
        # ``.info`` when it has one — ``vendor_valuation`` is written only for a closed session, so an intraday
        # Fetch leaves its own ``.info`` in the cache and no row behind it — and the stored row otherwise. With
        # neither half present anywhere there is no report to render and the caller gets ``_NoCachedData``, never
        # a live fetch from this path.
        financials = vendor_data.merge_for_refresh(vendor_data.read_financials(ticker),
                                                  cache_manager.load(ticker, ignore_ttl=True))
        if financials is None:
            raise _NoCachedData(ticker)
    vendor = None if no_fetch else vendor_data.Capture()

    def _fetch(t: str) -> dict:
        fetched = yfinance_client.get_financials(t)
        vendor.add_yfinance(t, fetched)
        return fetched

    def _peer_info(t: str) -> dict:
        info = comps_engine._default_fetch(t)
        vendor.add_valuation(t, info)
        return info

    if not no_fetch:
        financials = cache_manager.get_or_fetch(ticker, _fetch, use_cache)

    # Always stamp the data date — when the raw yfinance payload was fetched into
    # .store. The report renders it in the header so the viewer knows how fresh
    # the numbers are, whether this was a live Fetch or a store-only Refresh.
    try:
        from RevenueChaser.core import rawstore
        row = rawstore.get("ticker_payload_cache", ticker)
        if row and row.get("fetched_at_iso"):
            financials["_fetched_at_iso"] = row["fetched_at_iso"]
            financials["fetched_at_iso"] = row["fetched_at_iso"]
    except Exception:  # pragma: no cover - a missing stamp must not break a report
        pass

    warnings: list[str] = []
    results: dict = {"ticker": ticker, "financials": financials, "modules": {}, "warnings": warnings}

    # Quick Remarks — her zaman üstte (ticker_scores'dan beslenir, yfinance fallback). FIN-5: it is handed the
    # payload this run already has, so it scores from it instead of fetching the same financials a second time.
    results["quick_remarks"] = quick_remarks_engine.run_quick_remarks(ticker, no_fetch=no_fetch,
                                                                     financials=financials)

    # R5 (#88): the sector is the spine's (DCF bands, comps peers and the report header all read it);
    # the yfinance sector no longer feeds GICS. A name the spine lacks is Unclassified.
    from RevenueChaser.core import universe as _spine
    sector = _spine.gics_of(ticker, _spine.spine_gics([ticker])).sector
    financials["sector"] = sector
    for m in modules:
        m = m.strip().lower()
        if m in ("dcf", "dcf_engine"):
            results["modules"]["dcf"] = dcf_engine.run_dcf(financials, assumptions)
        elif m in ("3stmt", "three_statement", "3statement"):
            results["modules"]["three_statement"] = three_statement_engine.run_three_statement(financials, assumptions)
        elif m in ("comps", "comps_engine"):
            if no_fetch:
                # FIN-4 (#385): a Refresh fetches nothing, so it renders the comps its last stored Fetch computed,
                # labelled with that run's fetch date. Nothing stored yet is an empty state on the board, never an
                # error and never a live fetch, so the reader keeps its promise of no network.
                stored = fin_history.last_report(ticker)
                if fin_history.has_comps(stored):
                    results["modules"]["comps"] = {**stored["modules"]["comps"], "as_of": stored.get("as_of"),
                                                   "stored_run_id": stored.get("run_id")}
                else:
                    results["modules"]["comps"] = {
                        "not_stored": True,
                        "note": f"No stored comps for {ticker} yet — the Refresh shows its last stored Fetch and "
                                f"fetches nothing; run the Fetch button to compute them.",
                    }
            else:
                if peers:
                    used_peers = list(peers)
                else:
                    # FIN-3: the peers are the spine's (industry at the nearest market cap, then sector), never
                    # a static list. A short ladder is reported, not silently padded.
                    used_peers = select_peers(ticker, _spine.classification_universe_rows())
                    if len(used_peers) < PEER_K:
                        warnings.append(f"comps: {len(used_peers)} of {PEER_K} peers — the spine industry and "
                                        f"sector have no more names")
                results["modules"]["comps"] = comps_engine.run_comps(financials, used_peers, fetch_info=_peer_info)
        elif m in ("credit", "credit_capacity"):
            results["modules"]["credit"] = credit_capacity_engine.run_credit_capacity(financials, assumptions)
        elif m in ("sensitivity", "sensitivity_engine"):
            results["modules"]["sensitivity"] = sensitivity_engine.run_sensitivity(financials, assumptions)
        else:
            warnings.append(f"unknown module skipped: {m}")

    # Engine warnings/errors (deduplicated, order preserved)
    seen: set[str] = set()
    for name, res in results["modules"].items():
        if isinstance(res, dict):
            for w in res.get("warnings", []):
                if w not in seen:
                    seen.add(w)
                    warnings.append(w)
            err = res.get("error")
            if err:
                msg = f"{name}: {err}"
                if msg not in seen:
                    seen.add(msg)
                    warnings.append(msg)

    paths: dict = {}
    if output in ("html", "both"):
        html = build_report_html(results)
        path = Path(report_path) if report_path else OUTPUT_DIR / f"{ticker}_report.html"
        # OUTPUT_DIR is created at import time only; if the (regenerable)
        # .cache tree was cleared while the server runs, recreate it here like
        # every other writer in the repo does. A bare write_text made a store-only
        # refresh die with FileNotFoundError and silently fall back to the network.
        path.parent.mkdir(parents=True, exist_ok=True)
        write_artifact(path, html)
        paths["html"] = str(path)

    # After the report (FIN-7): a render or write that raises stores no vendor rows, so no run_id lands in
    # vendor_* without its board file. A cache hit (e.g. a same-day re-Fetch) stores no
    # new statements; the comps peers' quotes are still fetched and stored.
    if vendor is not None:
        from RevenueChaser.core import ledger
        vendor.flush(run_id=run_id or ledger.new_run_id(), label="Financials")

    return {"paths": paths, "results": results}
