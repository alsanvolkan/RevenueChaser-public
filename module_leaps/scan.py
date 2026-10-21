"""LEAPS accumulation scan (live UW + store-only), owned by the LEAPS module.

Relocated from the former ``module_testing.forwards_testing.forward_collector``
when the forward-test subsystem was removed. The universe is the watchlist;
``scan_leaps`` fetches from Unusual Whales, ``scan_leaps_cached`` reads only
``.store/raw.duckdb`` (no network).
"""
import json
import logging
from datetime import date

from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.core import rawstore
from RevenueChaser.module_leaps import engine as leaps

logger = logging.getLogger(__name__)


def _leaps_tickers() -> set:
    """Watchlist tickers — the LEAPS scan universe."""
    return {t for t in (rawstore.load_watchlist() or set()) if t}


def _sec13f_names() -> dict:
    """Ticker -> name from the shared 13F security meta — the Refresh's fallback name source."""
    try:
        from RevenueChaser.module_sec_13f.engine import load_sec13f_meta
        return {t: (m.get("name") or "") for t, m in load_sec13f_meta().items()}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"cached 13F name lookup failed: {e}")
        return {}


def _history_names() -> dict:
    """Ticker -> name from the last recorded LEAPS session snapshot (K2 readers, ``core.history``, #377 L5).

    The Fetch resolves names through the UW screener and records one ``leaps_footprint_history`` row per watchlist
    ticker, so a ticker the 13F meta does not carry keeps its name on Refresh. Empty when nothing is recorded, so
    the caller falls back to the 13F meta; a read never creates the history file.
    """
    from RevenueChaser.core import history, rawstore
    from RevenueChaser.module_leaps import history as leaps_history
    try:
        with rawstore.brief_wait():
            snaps = history.sessions(leaps_history.BOARD, 1)
            if not snaps:
                return {}
            rows = rawstore.query("SELECT ticker, name FROM leaps_footprint_history WHERE run_id = ?",
                                  [snaps[0]["run_id"]], db="history")
        return {r["ticker"]: r["name"] for r in rows if r["name"]}   # a blank name never hides the 13F one
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"cached history name lookup failed: {e}")
        return {}


def _screener_names(uw, tickers: list) -> dict:
    """The screener's ticker rows, through the real client's outcome-carrying ``screener_batch_result`` (#377 L5).

    A test double that offers only ``screener_batch`` still works."""
    read = getattr(uw, "screener_batch_result", None)
    if read is not None:
        return read(tickers).data or {}
    return uw.screener_batch(tickers) or {}


def _history_baselines(tickers, today: date, warnings: list | None = None) -> dict:
    """The recorded baselines of the whole watchlist in one batched read (O8/O9, #377 L3).

    A history read never fails a Fetch or a Refresh: a locked or unreadable file gives no baselines (growth None)
    and one warning, so the board can say a Quiet label there is missing history rather than a flat footprint.
    """
    from RevenueChaser.module_leaps import history as leaps_history
    try:
        return leaps_history.baselines(tickers, today)
    except Exception as e:  # noqa: BLE001 - a missing baseline, never a failed run
        logger.warning(f"LEAPS recorded baselines not read: {e}")
        if warnings is not None:
            warnings.append(f"The recorded session baselines could not be read ({type(e).__name__}: {e}), so no "
                            "growth is shown: a Quiet label here is missing history, not a flat footprint.")
        return {}


def rederive(snap: dict, oi, base_1d: dict | None, base_q: dict | None, ask_share=None) -> dict:
    """Classify a stored snapshot again from its own OI and the recorded baselines (a copy; nothing is written).

    This is the Refresh's D4 re-derivation (#377 L3): the same classifier a Fetch runs, at the recorded session's
    own date, so the far-dated cut and the quarter baseline are the ones that session saw.
    """
    return leaps.classify(dict(snap), base_1d, base_q, cur_oi=oi, ask_share=ask_share)


def _rederive_from_history(snap: dict, base: dict) -> dict:
    """Re-label a stored snapshot from its recorded session row (D4, #377 L3).

    Only when the newest recorded session is the session the cache holds. A newer intraday Fetch lives only in
    ``leaps_cache.latest`` and has no recorded row of its own, so its stored label stands.
    """
    from RevenueChaser.module_leaps import history as leaps_history
    newest = (base or {}).get("newest")
    if not newest or str(newest.get("as_of")) != str(snap.get("as_of")):
        return snap
    own = leaps_history.pick((base or {}).get("rows") or [], newest["as_of"])
    return rederive(snap, newest.get("oi_per_expiry"), own.get("1d"), own.get("q"),
                    ask_share=newest.get("ask_share"))


def scan_leaps_cached(today: date | None = None,
                      warnings: list | None = None) -> tuple[list[dict], list[dict]]:
    """Store-only LEAPS render — no network.

    Mirrors ``scan_leaps`` but reads every input from ``.store/raw.duckdb``
    instead of Unusual Whales:
      - watchlist footprints come from the latest cached snapshot per ticker
        (``leaps_cache``), re-labelled from the recorded sessions when the cache holds the session that was
        recorded (D4, #377 L3);
      - flow-print candidates come from the stored far-dated prints
        (``leaps_flow_prints`` via ``leaps.load_recent_prints``);
      - names come from the last recorded LEAPS session (``leaps_footprint_history``, #377 L5), the 13F meta as
        the fallback.
    """
    d = today or date.today()
    watched_tickers = sorted(_leaps_tickers())
    bases = _history_baselines(watched_tickers, d, warnings)

    try:
        rows = leaps.load_recent_prints(today=today)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"cached flow-print read failed: {e}")
        rows = []
    agg = leaps.aggregate_flow_prints(rows, today)
    candidates = {c["ticker"]: c for c in leaps.score_flow_candidates(agg)}

    # Names from the last recorded Fetch (the resolved UW names), the 13F meta as the fallback (#377 L5).
    name_map = {**_sec13f_names(), **_history_names()}

    watched, unwatched = [], []
    for t in watched_tickers:
        data = leaps.load_cache(t)
        snap = (data or {}).get("latest") if isinstance(data, dict) else None
        if not snap:
            continue
        snap = _rederive_from_history(snap, bases.get(t) or {})
        _merge_radar(snap, candidates.get(t))
        snap.setdefault("name", name_map.get(t, ""))
        watched.append(snap)                       # every pinned name, whatever its label (#377 L2)

    seen = set(watched_tickers)
    for cand in candidates.values():
        sym = cand["ticker"]
        if sym in seen:
            continue
        snap = leaps.radar_snapshot(cand, today)
        snap["name"] = name_map.get(sym, "")
        unwatched.append(snap)
        if len(unwatched) >= leaps.UNWATCHED_CAP:
            break

    return watched, unwatched


def _failed_calls(uw) -> int:
    """How many UW calls of this client have failed so far (0 for a client without ``health``)."""
    try:
        return int((uw.health() or {}).get("failed") or 0)
    except Exception:  # noqa: BLE001 - a test double or a client without health
        return 0


def scan_leaps(uw: UWClient, today: date | None = None, warnings: list | None = None,
               footprints: list | None = None) -> tuple[list[dict], list[dict]]:
    """Scan LEAPS accumulation for the watchlist universe, plus a market-wide
    far-dated flow-print scan that surfaces new (unwatched) names.

    Discovery uses /api/option-trades (min_dte=183, min_premium=$50k), deduped
    by trade id into the store, scored as min-max(0.4·freq + 0.6·log notional)
    times a tiered ITM/OTM delta concentration multiplier.

    Returns (watched_candidates, unwatched_candidates). Warnings worth showing on the
    board (the print scan hit its cap) are appended to ``warnings`` when it is passed.

    ``footprints``, when passed, gets one entry per watchlist ticker, whatever its label (the result history,
    #377): ``snapshot`` (the merged footprint, None when the fetch raised), ``fetch_status`` ('ok',
    'provider_failed' when one of its UW calls failed, or 'error: ...') and ``inputs`` (the UW answers, the two
    baselines and the flow candidate it was scored on).
    """
    d = today or date.today()
    watched_tickers = sorted(_leaps_tickers())
    logger.info(f"LEAPS scan: {len(watched_tickers)} watchlist tickers.")
    bases = _history_baselines(watched_tickers, d, warnings)      # one batched read for the whole scan (#377 L3)

    try:
        rows, _new = leaps.discover_flow_prints(uw, today=today, warnings=warnings)
    except Exception as e:
        logger.warning(f"flow-print discovery failed: {e}")
        rows = []
    agg = leaps.aggregate_flow_prints(rows, today)
    candidates = {c["ticker"]: c for c in leaps.score_flow_candidates(agg)}

    name_map = {}
    try:
        name_universe = sorted(set(watched_tickers) | set(candidates))
        scr = _screener_names(uw, name_universe)
        name_map = {t: (item.get("full_name") or item.get("name") or "") for t, item in scr.items()}
    except Exception as e:
        logger.warning(f"screener name lookup failed: {e}")

    watched, unwatched = [], []
    for t in watched_tickers:
        inputs, failed_before = {}, _failed_calls(uw)
        cand = candidates.get(t) or {}
        try:
            snap = leaps.fetch_and_classify(t, uw, today=today, inputs=inputs,
                                            near_term_event=bool(cand.get("near_event")),
                                            ask_share=cand.get("ask_share"),
                                            baselines=bases.get(t, {}))
        except Exception as e:
            logger.warning(f"leaps {t}: {e}")
            if footprints is not None:
                footprints.append({"ticker": t, "name": name_map.get(t, ""), "snapshot": None, "inputs": inputs,
                                   "fetch_status": f"error: {type(e).__name__}: {e}"})
            continue
        _merge_radar(snap, candidates.get(t))
        snap.setdefault("name", name_map.get(t, ""))
        if footprints is not None:
            # A counted UW failure, a failed read health() does not count (e.g. 404), or a read that raised.
            failed = (_failed_calls(uw) > failed_before or inputs.get("error")
                      or "failed" in (inputs.get("read_status") or {}).values())
            footprints.append({"ticker": t, "name": snap["name"], "snapshot": dict(snap),
                               "inputs": {**inputs, "flow_candidate": candidates.get(t)},
                               "fetch_status": "provider_failed" if failed else "ok"})
        watched.append(snap)                       # every pinned name, whatever its label (#377 L2)

    seen = set(watched_tickers)
    for cand in candidates.values():
        sym = cand["ticker"]
        if sym in seen:
            continue
        snap = leaps.radar_snapshot(cand, today)
        snap["name"] = name_map.get(sym, "")
        unwatched.append(snap)
        if len(unwatched) >= leaps.UNWATCHED_CAP:
            break

    return watched, unwatched


def _merge_radar(snap: dict, cand: dict) -> dict:
    """Attach flow-print metrics to a per-ticker footprint snapshot."""
    # The footprint score (OI + IV) is the snapshot's own; the flow score comes from the prints.
    snap["footprint_score"] = snap.get("score")
    snap["flow_score"] = int(round(cand["final_score"])) if cand is not None else None
    if cand is None:
        snap.setdefault("frequency", 0)
        snap.setdefault("notional", 0.0)
        snap.setdefault("net_notional", 0.0)
        snap.setdefault("concentration", None)
        snap.setdefault("concentration_mult", 1.0)
        snap.setdefault("ask_share", None)
        snap.setdefault("itm_conc", None)
        snap.setdefault("otm_conc", None)
    else:
        snap["frequency"] = cand["frequency"]
        snap["notional"] = cand["notional"]
        snap["net_notional"] = cand.get("net_notional")
        snap["concentration"] = cand["concentration"]
        snap["concentration_mult"] = cand["concentration_mult"]
        snap["ask_share"] = cand.get("ask_share")
        snap["itm_conc"] = cand.get("itm_conc")
        snap["otm_conc"] = cand.get("otm_conc")
    return snap


def run_leaps(uw: UWClient, write_report: bool = True) -> int:
    """Run the LEAPS accumulation scan and (optionally) write the dashboard."""
    watched, unwatched = scan_leaps(uw)
    logger.info(f"LEAPS scan complete: {len(watched)} watched, {len(unwatched)} unwatched.")
    if write_report:
        from RevenueChaser.module_leaps.report import render_leaps_dashboard
        path = render_leaps_dashboard(watched, unwatched)
        logger.info(f"LEAPS dashboard: {path}")
    return len(watched) + len(unwatched)


def main():
    import argparse
    from RevenueChaser.core.config import UW_API_KEY

    parser = argparse.ArgumentParser(description="RevenueChaser LEAPS accumulation scan")
    parser.add_argument("--no-report", action="store_true", help="scan only; do not write the dashboard")
    args = parser.parse_args()

    if not UW_API_KEY:
        print("UNUSUAL_WHALES_API_KEY not set — add it to .env.")
        return 2
    n = run_leaps(UWClient(), write_report=not args.no_report)
    print(f"LEAPS scan: {n} candidates.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

