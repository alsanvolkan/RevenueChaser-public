"""Orkestrasyon: ticker -> Quick Remarks + Fin Model engine'leri -> rapor."""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path

from RevenueChaser.core.reporting_common import write_artifact

from .config import OUTPUT_DIR, PEER_K, RISK_FREE_MAX_AGE_DAYS
from .engines import (
    comps_engine,
    credit_capacity_engine,
    dcf_engine,
    quick_remarks_engine,
    sensitivity_engine,
    three_statement_engine,
)
from . import history as fin_history
from . import market_inputs
from .peers import select_peers, target_row
from .reporting.html_builder import build_report_html
from RevenueChaser.core import vendor_data
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.core.providers import yfinance as yfinance_client


# An upper-cased symbol such as BRK.B. The ticker becomes a file name (CLI report), so a path piece
# must never get through (#173 D5, #175).
_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,9}")


logger = logging.getLogger(__name__)


def _stored_risk_free() -> dict | None:
    """The newest stored FRED 10-year Treasury yield as a rate with its date, or ``None`` when none is stored or the
    newest is more than ``RISK_FREE_MAX_AGE_DAYS`` old.

    The DCF's risk-free rate was the 4% constant although the store holds the real yield (Financials review S2).
    Read-only and zero network, so a Refresh reads it too; without it the DCF takes its default and says so. The
    yield is ``market_series`` DGS10 (``rcdata``, G6); the old ``macro_series`` row (the Earnings nightly's D27) is
    read only while there is no market file, until the old store is retired (plan PR 21).

    The window is looser than the Earnings Radar's on purpose (the Opus review's N1): that one pairs daily yields
    with returns for a beta, so a series three sessions behind is already wrong there. Here one level is read, a
    ten-year yield moves a few basis points in two weeks, and the fallback is a flat 4%, which is further off than a
    two-week-old yield; the date is shown.
    """
    from RevenueChaser.core import calendar as market_calendar
    from RevenueChaser.rcdata import read, serving
    today = market_calendar.today_ny()
    if serving.live_path("market").exists():
        rows = [r for r in read.market_series(["DGS10"], start=today - timedelta(days=RISK_FREE_MAX_AGE_DAYS))
                if r.get("source") == "fred" and r.get("value") is not None]
        if not rows:
            return None
        newest = max(rows, key=lambda r: r["obs_date"])
        return {"value": float(newest["value"]) / 100, "as_of": newest["obs_date"].isoformat()}
    from RevenueChaser.core import rawstore
    try:
        rows = rawstore.query("SELECT obs_date, value FROM macro_series WHERE series_id = 'DGS10' AND source = 'fred' "
                              "AND value IS NOT NULL ORDER BY obs_date DESC LIMIT 1")
    except Exception as exc:  # a store without the table reads as no yield, never a failed report
        logger.debug("stored DGS10: %s", exc)
        return None
    if not rows:
        return None
    obs = rows[0]["obs_date"]
    obs = obs if isinstance(obs, date) else date.fromisoformat(str(obs)[:10])
    if (today - obs).days > RISK_FREE_MAX_AGE_DAYS:
        return None
    return {"value": float(rows[0]["value"]) / 100, "as_of": obs.isoformat()}


# The statement fields the SEC facts carry (``sec_edgar.CONCEPTS``) that the engines read, by the engines' names. A
# field the facts do not carry (pre-tax income, tax provision, interest expense, current assets and liabilities) is
# left out, so the engines' labelled defaults show (review S1).
_SEC_FIELDS = ("revenue", "cost_of_revenue", "gross_profit", "ebit", "net_income", "diluted_shares", "basic_shares",
               "rd_expense", "operating_cash_flow", "capex", "depreciation", "total_assets", "total_equity", "cash",
               "short_term_investments", "receivables", "inventory", "payables", "total_debt")
# shortcut: the newest five fiscal years (Yahoo gave four); read more when a model projects from a longer history.
SEC_YEARS = 5
# What a run on the SEC's statements always takes a default for, because the facts never carry it (no pre-tax
# income, tax provision or interest expense): the Source line names it (Opus review M1; avo, 29 Oct 2026: stay on the
# SEC's statements and say so, rather than letting these send every run to Yahoo's).
# shortcut: named, not filled; upgrade when the layer serves the tax and interest concepts (#271).
NOT_IN_FACTS = ("tax rate", "interest expense")


def _financials_record(rec: dict) -> dict:
    """One SEC annual record (``sec_edgar.extract_annual``'s shape) as the engines read a year: the fields above,
    ``capital_expenditure`` beside ``capex``, ``period_end`` (the revenue's) and EBITDA as operating income plus
    depreciation when the year has both."""
    from RevenueChaser.core import fiscal_year
    from RevenueChaser.core.config import to_float
    end = fiscal_year.period_end(rec)
    out: dict = {"fiscal_year": rec.get("fiscal_year"), "period_end": end.isoformat() if end else None}
    for f in _SEC_FIELDS:
        v = to_float(rec.get(f))
        if v is not None:
            out[f] = v
    if "capex" in out:
        out["capital_expenditure"] = out["capex"]
    if "ebit" in out and "depreciation" in out:
        out["ebitda"] = out["ebit"] + out["depreciation"]
    return out


def _statement_gaps(annual: list[dict], bank: bool = False) -> list[str]:
    """The base-year inputs the engines would run on a labelled default for (``dcf_engine``): revenue, EBITDA,
    depreciation and capex of the newest year with revenue (``fiscal_year.anchor``), and working capital in every
    year, except for a bank (``bank``), whose models never read it (P/E only, Opus review N4)."""
    from RevenueChaser.core import fiscal_year
    from RevenueChaser.core.config import to_float
    from .engines.dcf_engine import _nwc
    i = fiscal_year.anchor(annual)
    latest = annual[i] if i is not None else {}
    gaps = sorted(f for f in ("revenue", "ebitda", "depreciation") if to_float(latest.get(f)) is None)
    if to_float(latest.get("capex")) is None and to_float(latest.get("capital_expenditure")) is None:
        gaps = sorted(gaps + ["capex"])
    if not bank and not any(_nwc(r) is not None for r in annual):
        gaps.append("working capital")
    return gaps


def _statements_choice(sec: list[dict], yahoo: list[dict], bank: bool = False) -> tuple[str, list[str]]:
    """``(used, what the SEC's statements lack)``: which annual statements a run models, ``"sec"`` or ``"yahoo"``.

    The SEC's, unless Yahoo's newest year is newer, or is the same year and gives an input the SEC's lacks (the
    real-data check of 29 Oct 2026: the SEC facts carry no depreciation for MSFT, ORCL, AMD and AVGO, no working
    capital for a filer without an inventory line, and no year after 2020 for an IFRS filer such as TM, so their DCF
    ran on defaults Yahoo did not need). Default chosen, change if avo prefers the SEC's statements always.

    A year is the newest one with revenue (``fiscal_year.anchor``), so a newer year with only a balance sheet is no
    newer year (Opus review S2). With ``"sec"`` the list is every input the SEC's year lacks, which then runs on a
    default and is named on the Source line (Opus review S1); with ``"yahoo"`` it is what Yahoo's gives that the
    SEC's lacks.

    shortcut: a whole-source choice per run, never a field-by-field mix; upgrade when the layer serves depreciation's
    other tags and current assets and liabilities (#271; then the SEC's statements stand alone)."""
    from RevenueChaser.core import fiscal_year
    sec_gaps = _statement_gaps(sec, bank)
    if not yahoo:
        return "sec", sec_gaps

    def year(rows):
        i = fiscal_year.anchor(rows)
        text = str(rows[i].get("fiscal_year") or "")[:4] if i is not None else ""
        return int(text) if text.isdigit() else None

    s, y = year(sec), year(yahoo)
    if s is not None and y is not None and s != y:
        return ("sec", sec_gaps) if s > y else ("yahoo", [f"the fiscal year {y}"])
    lacking = [g for g in sec_gaps if g not in _statement_gaps(yahoo, bank)]
    return ("yahoo", lacking) if lacking else ("sec", sec_gaps)


def _sec_statements(ticker: str) -> tuple[list[dict], dict] | None:
    """``(annual, meta)``: the newest ``SEC_YEARS`` fiscal years from the fundamentals layer (G6, build plan PR 15:
    "statement inputs from the store"), or None off the layer or when it holds no year for ``ticker``. They end on
    the newest year with revenue: a later year the layer serves with only balance-sheet fields is not a base year
    (Opus review S2)."""
    from RevenueChaser.core import fiscal_year, fundamentals_layer
    if not fundamentals_layer.reads_layer():
        return None
    annual, _quarterly, meta = fundamentals_layer.records(ticker)
    i = fiscal_year.anchor(annual)
    if i is None:
        return None
    annual = annual[:i + 1]
    return [_financials_record(r) for r in annual[-SEC_YEARS:]], meta


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

    # R5 (#88): the sector is the spine's (DCF bands, comps peers and the report header all read it);
    # the yfinance sector no longer feeds GICS. A name the spine lacks is Unclassified.
    from RevenueChaser.core import universe as _spine
    from .engines._inputs import PE_ONLY_SECTOR
    sector = _spine.gics_of(ticker, _spine.spine_gics([ticker])).sector

    # G6 follow-up: the price, the market cap, the shares and the beta are the prices store's (G2), each Yahoo's only
    # where the store has none; ``market`` is the Source line's part that says which.
    financials, market = market_inputs.overlay(ticker, financials)

    # G6 (build plan PR 15): on the fundamentals layer the statements the models read are the SEC's; Yahoo's
    # payload keeps the name (and whatever market input the prices store does not give).
    sec = _sec_statements(ticker)
    if sec is not None:
        annual, meta = sec
        used, lacking = _statements_choice(annual, financials.get("annual") or [], bank=sector == PE_ONLY_SECTOR)
        if used == "sec":
            # Delta review nit: a bank's model (P/E only) reads neither a tax rate nor an interest expense.
            if sector != PE_ONLY_SECTOR:
                lacking = lacking + list(NOT_IN_FACTS)
            filed = meta.get("newest_filed")
            financials = {**financials, "annual": annual,
                          "statements_meta": {**meta, "used": "sec", "sec_lacks": lacking},
                          "source": "SEC company facts (statements" + (f", newest filed {filed}" if filed else "")
                                    + ("; not in the facts, so defaults: " + ", ".join(lacking) if lacking else "")
                                    + ") · " + market}
        else:
            financials = {**financials, "statements_meta": {**meta, "used": "yahoo", "sec_lacks": lacking},
                          "source": "Yahoo (statements: the SEC company facts lack " + ", ".join(lacking)
                                    + ") · " + market}
    else:
        financials = {**financials, "source": "Yahoo (statements) · " + market}

    warnings: list[str] = []
    results: dict = {"ticker": ticker, "financials": financials, "modules": {}, "warnings": warnings}

    # Quick Remarks — her zaman üstte (ticker_scores'dan beslenir, yfinance fallback). FIN-5: it is handed the
    # payload this run already has, so it scores from it instead of fetching the same financials a second time.
    results["quick_remarks"] = quick_remarks_engine.run_quick_remarks(ticker, no_fetch=no_fetch,
                                                                     financials=financials)

    financials["sector"] = sector
    financials["risk_free_rate"] = _stored_risk_free()
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
                    # a static list. A short ladder is reported, not silently padded — and a target the spine
                    # does not carry is not a short ladder: it is the reason there is no ladder at all.
                    spine_rows = _spine.classification_universe_rows(
                        operating_only=_spine.BOARD_OPERATING_ONLY["module_financials"])
                    used_peers = select_peers(ticker, spine_rows)
                    if target_row(ticker, spine_rows) is None:
                        warnings.append(f"comps: {ticker} is not in the spine universe (below the market-cap "
                                        f"floor, or an excluded security type), so no peers were picked")
                    elif len(used_peers) < PEER_K:
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
