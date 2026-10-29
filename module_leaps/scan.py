"""LEAPS accumulation scan, owned by the LEAPS module, on the options store (G9, build plan PR 19).

The pinned names are the stock watch list. Their footprints and the large-print scan read the options store that
``uw/option_list`` fills every evening for SPX and the option watch list (``rcdata.read``): a pinned name off that
list shows "not on the option watch list" and gets no footprint score, and the print scan runs over the list
names' stored prints only. ``scan_leaps`` (the Fetch) and ``scan_leaps_cached`` (the Refresh) compute the same board
from the same store with no network; only the Fetch fills ``footprints`` for the result history.

The UW pull (``engine.fetch_and_classify``, ``engine.discover_flow_prints``) and the old store's ``leaps_cache`` and
``leaps_flow_prints`` are no longer used by the board; they retire with the old store (G10).
"""
import logging
from collections import defaultdict
from datetime import date, timedelta

from RevenueChaser.core import board_requests
from RevenueChaser.core.config import to_float
from RevenueChaser.module_leaps import engine as leaps

logger = logging.getLogger(__name__)


def _leaps_tickers() -> set:
    """The stock watch list's tickers (ops.sqlite, through ``rcdata.read``; PR 4) — the LEAPS scan universe."""
    from RevenueChaser.rcdata import read
    return {t for t in (read.watched("stock") or set()) if t}


def _history_names() -> dict:
    """Ticker -> name from the last recorded LEAPS session snapshot (K2 readers, ``core.history``, #377 L5).

    The Fetch records one ``leaps_footprint_history`` row per pinned ticker with its name, so a ticker the spine does
    not carry keeps the name an older Fetch resolved (through the UW screener, before G9). Empty when nothing is
    recorded; a read never creates the history file.
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
        return {r["ticker"]: r["name"] for r in rows if r["name"]}   # a blank name never hides another one
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"cached history name lookup failed: {e}")
        return {}


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
            # The class only: a store error names the file it could not open, a host path (#107); the log has it all.
            warnings.append(f"The recorded session baselines could not be read ({type(e).__name__}), so no "
                            "growth is shown: a Quiet label here is missing history, not a flat footprint.")
        return {}


# The newest screener session counts for this many days (``core/earnings_dates.MAX_AGE_DAYS``, the same rule).
REPORT_DATES_MAX_AGE_DAYS = 4


def _near_event_dates(today: date, warnings: list | None = None) -> dict:
    """``{ticker: report date}`` from the options store's newest screener session (``option_screener_daily``: every
    name reporting in the 28 days after it, DF94, #39).

    A read never fails a scan: no session within ``REPORT_DATES_MAX_AGE_DAYS`` days, or an unreadable store, gives
    no dates and one warning, so the board can say a Quiet label there is missing calendar data, not a near event."""
    from RevenueChaser.rcdata import read
    note = None
    try:
        rows = read.option_screener()
    except Exception as e:  # noqa: BLE001 - a missing date, never a failed run
        logger.warning(f"LEAPS report dates not read: {e}")
        rows, note = [], f"could not be read ({type(e).__name__})"
    sessions = [r["session_date"] for r in rows if r.get("session_date")]
    if sessions and (today - max(sessions)).days > REPORT_DATES_MAX_AGE_DAYS:
        rows = []
    dates = {str(r["ticker"]).upper(): r["report_date"] for r in rows if r.get("ticker") and r.get("report_date")}
    if not dates and note is None:
        note = "has no rows from the last few sessions"
    if note and warnings is not None:
        warnings.append(f"The stored earnings calendar {note}, so no watchlist name was flagged for a near event "
                        "from the calendar: a Quiet label here may be missing calendar data, not a near event.")
    return dates


def _watchlist_near_event(ticker: str, cand: dict, near: dict, today: date) -> bool:
    """A pinned name's near-event flag: its own stored earnings date OR the flow candidate's own flag.

    The flow candidate only exists for a name with a far-dated print in the window, so a pinned name without one
    would never see the event exclusion (DF94). The two sources are OR'd, never ranked: the candidate's flag comes
    off the tape's own ``next_earnings_date`` (``engine.aggregate_flow_prints``) while the stored calendar may be up
    to ``earnings_dates.MAX_AGE_DAYS`` sessions old, so letting the calendar decide alone could take a flag away
    from a candidate whose print says earnings are days out (#99 review). This only ever adds the flag.
    """
    own = ticker in near and 0 <= (near[ticker] - today).days <= leaps.NEAR_EVENT_DAYS
    return own or bool(cand.get("near_event"))


# ── the store reads ──────────────────────────────────────────────────────────────────────────────────────────

IV_RANK_DAYS = 14          # calendar days of option_daily read for the IV-rank slope (its last five sessions)
PRINT_DAYS = 5             # the print window, as ``engine.load_recent_prints``


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)[:10]


def store_inputs(ticker: str) -> dict | None:
    """The footprint's inputs from the options store's newest session of ``ticker``; None when none is stored.

    ``oi_per_expiry`` sums ``option_eod``'s open interest per expiry and side, ``flow_per_expiry`` is
    ``option_expiry``'s ask-side premium (set ``list``), ``iv_rank`` is ``option_daily``'s 1-year IV rank over the
    last ``IV_RANK_DAYS`` days, oldest first: the shapes the UW answers had, so ``engine.build_snapshot`` is unchanged.
    """
    from RevenueChaser.rcdata import read
    rows = read.option_chain(ticker)
    if not rows:
        return None
    session = rows[0]["session_date"]
    oi: dict[str, dict] = defaultdict(lambda: {"call_oi": 0, "put_oi": 0})
    for r in rows:
        side = "call_oi" if r.get("option_type") == "call" else "put_oi"
        oi[_iso(r["expiry"])][side] += int(r.get("open_interest") or 0)
    flow = [{"expiry": _iso(r["expiry"]), "call_premium_ask_side": r.get("call_premium_ask_side"),
             "put_premium_ask_side": r.get("put_premium_ask_side")}
            for r in read.option_expiries(ticker, session, "list")]
    iv = [{"date": _iso(r["session_date"]), "iv_rank_1y": r.get("iv_rank_1y")}
          for r in read.option_daily(ticker, session - timedelta(days=IV_RANK_DAYS), session)]
    return {"session": session, "oi_per_expiry": [{"expiry": e, **v} for e, v in sorted(oi.items())],
            "flow_per_expiry": flow, "iv_rank": iv}


def store_prints(names, today: date, near: dict) -> list[tuple]:
    """The list names' far-dated large prints of the last ``PRINT_DAYS`` days, as ``engine.aggregate_flow_prints``
    reads them: ``(ticker, premium, option_type, delta, report date, ask_vol, bid_vol)``.

    ``option_print`` set ``list`` only (the earnings set is Earnings Radar's), not cancelled, at least
    ``engine.MIN_DTE`` days to expiry on its session and at least ``engine.MIN_PREMIUM``; an index is not a stock.
    The report date is the stored calendar's (a print carries none).
    shortcut: the store keeps the last 50 prints a name and session (UW's flow-recent), so a busy name's far-dated
    large prints can be cut off; upgrade when ``uw/option_list`` also stores each list name's far-dated large prints
    (``/option-trades`` by ticker, ``min_dte`` and ``min_premium``).
    """
    from RevenueChaser.rcdata import read
    since = today - timedelta(days=PRINT_DAYS)
    out = []
    for ticker in names:
        if ticker in leaps.INDEX_TICKERS:
            continue
        sessions = sorted({r["session_date"] for r in read.option_daily(ticker, since, today)})
        report = near.get(ticker)
        for session in sessions:
            for p in read.option_prints(ticker, session, "list"):
                premium = to_float(p.get("premium")) or 0.0
                expiry = p.get("expiry")
                if p.get("canceled") or expiry is None or premium < leaps.MIN_PREMIUM:
                    continue
                if (expiry - session).days < leaps.MIN_DTE or "index" in (p.get("tags") or []):
                    continue
                out.append((ticker, premium, p.get("option_type"), to_float(p.get("delta")),
                            _iso(report) if report else None, p.get("ask_vol"), p.get("bid_vol")))
    return out


def _spine_names(tickers) -> dict:
    """Ticker -> name from the spine (any tier), the first name source; the last recorded session fills the rest."""
    try:
        from RevenueChaser.module_sec_13f import store_reads
        return {t: m.get("name") or "" for t, m in store_reads.meta(sorted(tickers)).items() if m.get("name")}
    except Exception as e:  # noqa: BLE001 - a name is never worth a failed board
        logger.warning(f"LEAPS spine name lookup failed: {e}")
        return {}


def _footprint(ticker: str, listed: set, cand: dict, near: dict, base: dict,
               today: date) -> tuple[dict, dict, str]:
    """``(snapshot, inputs, fetch_status)`` of one pinned name from the store.

    ``fetch_status`` is ``ok``, ``not_on_list`` (a pinned name off the option watch list: the label, no score) or
    ``missing`` (a list name the store has no chain for yet)."""
    from RevenueChaser.module_leaps import history as leaps_history
    if ticker not in listed:
        return ({**leaps.build_snapshot(ticker, [], [], [], today=today), "note": board_requests.NOT_ON_LIST},
                {}, "not_on_list")
    inputs = store_inputs(ticker)
    if inputs is None:
        note = f"no stored option chain yet: it fills with the next {board_requests.OPTION_LIST} run"
        return {**leaps.build_snapshot(ticker, [], [], [], today=today), "note": note}, {}, "missing"
    session = inputs["session"]
    oi, flow, iv = inputs["oi_per_expiry"], inputs["flow_per_expiry"], inputs["iv_rank"]
    snap = leaps.build_snapshot(ticker, oi, flow, iv, near_term_event=_watchlist_near_event(ticker, cand, near,
                                                                                            session),
                                today=session)
    # The baselines are picked at the stored session, so a Refresh the day after compares the same two sessions.
    picked = leaps_history.pick((base or {}).get("rows") or [], session)
    snap = leaps.classify(snap, picked["1d"], picked["q"], cur_oi=oi, ask_share=cand.get("ask_share"))
    return snap, {**inputs, "session": _iso(session), "baseline_1d": picked["1d"], "baseline_q": picked["q"]}, "ok"


def _scan(today: date | None, warnings: list | None, footprints: list | None) -> tuple[list[dict], list[dict]]:
    d = today or date.today()
    pinned = sorted(_leaps_tickers())
    listed = board_requests.option_list_names()
    logger.info(f"LEAPS scan: {len(pinned)} pinned names, {len(listed)} on the option watch list.")
    bases = _history_baselines(pinned, d, warnings)               # one batched read for the whole scan (#377 L3)
    near = _near_event_dates(d, warnings)                         # one batched calendar read for the whole scan (DF94)

    agg = leaps.aggregate_flow_prints(store_prints(listed, d, near), d)
    candidates = {c["ticker"]: c for c in leaps.score_flow_candidates(agg)}
    name_map = {**_history_names(), **_spine_names(set(pinned) | set(candidates))}

    watched, unwatched = [], []
    for t in pinned:
        cand = candidates.get(t) or {}
        snap, inputs, status = _footprint(t, set(listed), cand, near, bases.get(t) or {}, d)
        _merge_radar(snap, candidates.get(t))
        snap.setdefault("name", name_map.get(t, ""))
        if footprints is not None:
            footprints.append({"ticker": t, "name": snap["name"], "snapshot": dict(snap),
                               "inputs": {**inputs, "flow_candidate": candidates.get(t)}, "fetch_status": status})
        watched.append(snap)                       # every pinned name, whatever its label (#377 L2)

    seen = set(pinned)
    for cand in candidates.values():
        sym = cand["ticker"]
        if sym in seen:
            continue
        snap = leaps.radar_snapshot(cand, d)
        snap["name"] = name_map.get(sym, "")
        unwatched.append(snap)
        if len(unwatched) >= leaps.UNWATCHED_CAP:
            break
    return watched, unwatched


def scan_leaps_cached(today: date | None = None,
                      warnings: list | None = None) -> tuple[list[dict], list[dict]]:
    """The Refresh: the same board as the Fetch, from the same store, recording nothing."""
    return _scan(today, warnings, None)


def scan_leaps(uw=None, today: date | None = None, warnings: list | None = None,
               footprints: list | None = None) -> tuple[list[dict], list[dict]]:
    """The Fetch's scan: the pinned names' footprints and the list names' far-dated large prints, from the store.

    ``uw`` is unused (G9: the builder fetches). Returns (watched_candidates, unwatched_candidates); warnings worth
    showing on the board (the stored calendar could not be read) are appended to ``warnings``. ``footprints``, when
    passed, gets one entry per pinned name, whatever its label (the result history, #377): ``snapshot``,
    ``fetch_status`` (``ok``, ``not_on_list`` or ``missing``) and ``inputs`` (the store rows it was built from, the
    two baselines and its flow candidate).
    """
    return _scan(today, warnings, footprints)


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


def run_leaps(uw=None, write_report: bool = True) -> int:
    """Run the LEAPS accumulation scan from the store and (optionally) write the dashboard."""
    watched, unwatched = scan_leaps(uw)
    logger.info(f"LEAPS scan complete: {len(watched)} watched, {len(unwatched)} unwatched.")
    if write_report:
        from RevenueChaser.module_leaps.report import render_leaps_dashboard
        path = render_leaps_dashboard(watched, unwatched)
        logger.info(f"LEAPS dashboard: {path}")
    return len(watched) + len(unwatched)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="RevenueChaser LEAPS accumulation scan (options store)")
    parser.add_argument("--no-report", action="store_true", help="scan only; do not write the dashboard")
    args = parser.parse_args()
    n = run_leaps(write_report=not args.no_report)
    print(f"LEAPS scan: {n} candidates.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
