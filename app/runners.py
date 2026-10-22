"""Runners — thin wrappers that run modules and write stable ``latest.html`` files.

These are the glue between the dashboard server and the existing engines/exporters.
Each runner returns the Path to the generated file (or None if nothing was produced).
"""
import functools
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.core.config import OUTPUT_DIR
from RevenueChaser.core.gics_map import UNCLASSIFIED, is_unclassified, sector_or_unclassified
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.core import job_control, job_lock, ledger
from .jobs import JobMessage, run_id

logger = logging.getLogger(__name__)


def _freeze_run_id() -> str:
    """``run_id`` for a forecast-ledger freeze: the running job's id, else a fresh one (#128)."""
    return run_id()


def _write_output(module: str, content: str, suffix: str, ext: str = "html") -> Path:
    """Write a stable ``{module}_latest.{ext}`` regenerable file under ``.cache/``.

    - HTML dashboards live under ``.cache/{module}_latest.{ext}``.
    - JSON lives under ``.cache/{module}/{module}_latest.json``.
    """
    if ext == "json":
        stable = OUTPUT_DIR / module / f"{module}_latest.json"
    else:
        stable = OUTPUT_DIR / f"{module}_latest.{ext}"
    stable.parent.mkdir(parents=True, exist_ok=True)
    from RevenueChaser.core.reporting_common import write_artifact
    return write_artifact(stable, content)

def _write_board(path: Path, html: str, data) -> Path:
    """Write a board's HTML and its K10 payload file (``reporting_common.write_board``); ``data=None`` drops one."""
    from RevenueChaser.core.reporting_common import write_board
    return write_board(path, html, data)


def _write_empty_board(path: Path, title: str, message: str, hint: str = "") -> Path:
    """Write an explicit "nothing to show, and here is why" board.

    A run that produces no rows must still overwrite its artifact. Returning
    early instead left the previous (or missing) file in place, so the job
    reported success and the tab visibly did nothing — indistinguishable from a
    broken refresh.

    Markup lives in ``templates/empty_board.html.j2``. A plain Jinja environment
    rather than ``flask.render_template``: runners execute on the job-queue
    worker thread, which has no Flask app context.
    """
    from datetime import datetime, timezone
    from RevenueChaser.core import templating
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    from RevenueChaser.core.reporting_common import write_artifact
    return write_artifact(path, templating.render(
        "empty_board.html.j2", title=title, message=message, hint=hint, stamp=stamp))


def _uw_health(uw) -> dict | None:
    """``uw.health()`` for the board banner (MB3), or None without a real client
    (store-only renders and test doubles pass ``uw=None``)."""
    fn = getattr(uw, "health", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a banner must never break a render
        return None


def _provider_gap_text(uw) -> str | None:
    """Why an empty result is a provider failure, not "no data" (ER-1), or None."""
    auth = getattr(uw, "auth_error", None)
    if isinstance(auth, dict) and auth:
        return (f"Unusual Whales rejected the API key (HTTP {auth.get('status', '?')}). "
                "Check UNUSUAL_WHALES_API_KEY in .env.")
    health = _uw_health(uw) or {}
    if int(health.get("failed") or 0) > 0:
        return (f"{health['failed']} of {health.get('calls') or health['failed']} "
                "Unusual Whales calls failed.")
    return None


def _no_events_text(start: date, end: date) -> tuple[str, str]:
    """(message, hint) for an earnings range that returned no events."""
    same_day = start == end
    when = f"{start.isoformat()}" if same_day else f"{start.isoformat()} → {end.isoformat()}"
    weekend = same_day and start.weekday() >= 5
    message = f"No earnings events scheduled for {when}."
    if weekend:
        hint = ("That date is a weekend — US issuers do not report. Pick a "
                "weekday, or use the range picker to load the week ahead.")
    else:
        hint = ("The provider returned an empty calendar for this range. Try a "
                "wider range, or check the Unusual Whales key if every board is empty.")
    return message, hint


def _coerce_earnings_days(start=None, end=None, dates=None) -> list[date]:
    """Resolve a discrete day list from ``dates=`` or a contiguous ``start``/``end``."""
    if dates:
        if isinstance(dates, str):
            parts = [p.strip() for p in dates.split(",") if p.strip()]
            return sorted({date.fromisoformat(p) for p in parts})
        out: list[date] = []
        for d in dates:
            out.append(d if isinstance(d, date) else date.fromisoformat(str(d)))
        return sorted(set(out))
    if start is None:
        return []
    if end is None:
        end = start
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def _no_events_text_days(days: list[date]) -> tuple[str, str]:
    if not days:
        return "No earnings events scheduled.", "Pick at least one weekday."
    if len(days) == 1 or days[-1] - days[0] == timedelta(days=len(days) - 1):
        return _no_events_text(days[0], days[-1])
    when = ", ".join(d.isoformat() for d in days)
    message = f"No earnings events scheduled for {when}."
    hint = ("The provider returned an empty calendar for these days. Try other "
            "weekdays, or check the Unusual Whales key if every board is empty.")
    return message, hint


EARNINGS_LATEST = OUTPUT_DIR / "earnings_radar_latest.html"
LEAPS_LATEST = OUTPUT_DIR / "leaps_latest.html"
ROTATION_LATEST = OUTPUT_DIR / "sector_rotation_latest.html"
MONEY_FLOW_LATEST = OUTPUT_DIR / "sector_rotation" / "sector_rotation_latest.json"
ETF_HOLDINGS_CACHE = OUTPUT_DIR / "sector_rotation" / "etf_holdings.json"  # legacy path; store is raw.duckdb
SEC_13F_LATEST = OUTPUT_DIR / "sec_13f_latest.html"
QUICK_LATEST = OUTPUT_DIR / "financials_latest.html"
QUICK_STATE = OUTPUT_DIR / "financials" / "financials_latest.json"
ASYMMETRIC_GROWTH_LATEST = OUTPUT_DIR / "asymmetric_growth_latest.html"
ASYMMETRIC_GROWTH_JSON = OUTPUT_DIR / "asymmetric_growth" / "asymmetric_growth_latest.json"
SWING_LATEST = OUTPUT_DIR / "swing_potential_latest.html"
SWING_JSON = OUTPUT_DIR / "swing_potential" / "swing_potential_latest.json"
FUNDAMENTALS_LATEST = OUTPUT_DIR / "fundamentals_latest.html"
FUNDAMENTALS_JSON = OUTPUT_DIR / "fundamentals" / "fundamentals_latest.json"
COMOVEMENT_LATEST = OUTPUT_DIR / "comovement_latest.html"
COMOVEMENT_JSON = OUTPUT_DIR / "comovement" / "comovement_latest.json"
VALUE_CHAIN_LATEST = OUTPUT_DIR / "value_chain_latest.html"
VALUE_CHAIN_JSON = OUTPUT_DIR / "value_chain" / "value_chain_latest.json"
RELATEDNESS_LATEST = OUTPUT_DIR / "relatedness_latest.html"
RELATEDNESS_JSON = OUTPUT_DIR / "relatedness" / "relatedness_latest.json"
OIPD_LATEST = OUTPUT_DIR / "oipd_latest.html"
OIPD_JSON = OUTPUT_DIR / "oipd" / "oipd_latest.json"
SETTINGS_LATEST = OUTPUT_DIR / "settings_latest.html"

def _load_watchlist() -> set:
    from RevenueChaser.core import rawstore
    return rawstore.load_watchlist()


def _compute_nrc_results(cards) -> dict:
    from RevenueChaser.module_earnings_radar.pipeline import nrc_from_card
    wl = _load_watchlist()
    return {card.ticker: nrc_from_card(card, wl) for card in cards}


def _calendar_event(item: dict, d: date, session: str) -> dict | None:
    """One UW calendar row as the run's event dict, or None when the row names no ticker.

    ``source`` stays "UW" for a stored row too: ``history._input_problems`` counts these against
    ``UW_CALENDAR_ROW_LIMIT``, the client's page limit, whichever path served them.
    """
    t = item.get("symbol") or item.get("ticker", "")
    if not t:
        return None
    return {
        "ticker": t, "earnings_date": d,
        "earnings_time": "BMO" if session == "premarket" else "AMC",
        "sector": item.get("sector", ""),
        "full_name": item.get("full_name", ""),
        "marketcap": item.get("marketcap", ""),
        "expected_move_perc": item.get("expected_move_perc"),
        "street_mean_est": item.get("street_mean_est"),
        "pre_earnings_close": item.get("pre_earnings_close"),
        "post_earnings_close": item.get("post_earnings_close"),
        "report_time": item.get("report_time", ""),
        "source": "UW",
    }


# The coverage of the run's last ``_fetch_calendar_days`` (E5a, #372), for the history's ``input_sources``. A
# module-level slot rather than a third return value: every caller and test fake unpacks two.
_CALENDAR_SOURCES: dict = {}


def _fetch_calendar_days(uw: UWClient, days: list[date]) -> tuple[list, dict]:
    """Fetch calendar events for the given days (weekends skipped), from the store when the nightly sweep covered
    the (day, session) pair and from UW for the pairs it did not.

    The pairs are walked in the same order as the live-only read, so ``raw_cal``'s "premarket wins" rule gives the
    same answer either way.

    Today's and the next trading day's pairs are always read live (0-4 calls: none when the Fetch's days miss them): the stored row is last night's, and
    the EM gate, the daily cap, R4 and the card's estimate and report time read it, fields UW moves during the
    day for the names about to report (review S4).
    """
    from RevenueChaser.core import calendar
    from RevenueChaser.module_earnings_radar import stored_inputs
    events = []
    raw_cal = {}
    covered, _session = stored_inputs.calendar_items(days)
    today = calendar.today_ny()
    try:
        live_until = calendar.next_trading_day(today)
    except ValueError:                  # past the holiday table (late 2028): no next trading day is known, so
        # no pair is forced live and the store decides; the holiday table's yearly update removes this case
        live_until = today - timedelta(days=1)
    n_stored = n_live = 0
    for d in days:
        if d.weekday() >= 5:
            continue
        for session in ("premarket", "afterhours"):
            items = None if today <= d <= live_until else covered.get((d, session))
            if items:
                n_stored += 1
                rows = sorted(items, key=lambda i: str(i.get("symbol") or i.get("ticker") or ""))
            else:
                n_live += 1
                rows = uw.calendar_day_raw(d, session)
            for item in rows:
                ev = _calendar_event(item, d, session)
                if ev is None:
                    continue
                events.append(ev)
                t = ev["ticker"]
                if t not in raw_cal or session == "premarket":
                    raw_cal[t] = ev
    _CALENDAR_SOURCES.clear()
    _CALENDAR_SOURCES.update({"stored": n_stored, "live": n_live,
                              "session": _session.isoformat() if _session else None})
    return events, raw_cal


def _fetch_calendar_range(uw: UWClient, start: date, end: date) -> tuple[list, dict]:
    """Fetch UW calendar events for [start, end], returning (events, raw_cal)."""
    return _fetch_calendar_days(uw, _coerce_earnings_days(start, end))


def _no_cards_text(n_tickers: int, stats: dict | None, gap: str | None) -> str:
    """Why a run with calendar events scored nothing (E23)."""
    stats = stats or {}
    if stats.get("filtered") and not stats.get("passed") and not stats.get("bypassed"):
        by_reason = stats.get("by_reason") or {}
        if set(by_reason) == {"no screener row"}:      # E2, #372: unknown liquidity, not a failed gate
            return (f"No UW screener row for any of the {n_tickers} calendar tickers; nothing was scored."
                    + (f" {gap}" if gap else ""))
        reasons = ", ".join(f"{k} {v}" for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]))
        return (f"All {n_tickers} calendar tickers failed the filter gates ({reasons}); nothing was scored."
                + (f" {gap}" if gap else ""))
    return (f"{n_tickers} calendar tickers could not be scored; see the dashboard log."
            + (f" {gap}" if gap else ""))


def _record_earnings_history(cards, nrc, *, uw, span, events, filter_stats=None, rotation_failed=False,
                             reason=None, screener=None, run_at=None, input_sources=None) -> None:
    """Result history of an Earnings Fetch (#278): after the board is written, before a JobMessage ends the job.

    A failure is a job note, never a failed Fetch: the board file does not depend on it."""
    from RevenueChaser.module_earnings_radar import history as er_history
    try:
        er_history.record(cards, nrc, run_id=_freeze_run_id(),
                          run_at=run_at if run_at is not None else time.time(), uw_health=_uw_health(uw),
                          filter_stats=filter_stats, span=span, events=events, rotation_failed=rotation_failed,
                          reason=reason, screener=screener, input_sources=input_sources)
    except Exception as exc:
        logger.exception("Earnings result history failed")
        ledger.add_note(f"Earnings result history was not recorded: {type(exc).__name__}: {exc}")


def run_earnings_range(uw: UWClient, start: date | None = None, end: date | None = None,
                       dates=None) -> Path:
    """Run the earnings pipeline for a date range or a discrete day list."""
    days = _coerce_earnings_days(start, end, dates)
    run_at = time.time()   # one clock for the board stamp and the history header (ER-9, #372)
    _CALENDAR_SOURCES.clear()          # a patched calendar read leaves the slot empty, not the last run's
    if dates:
        events, raw_cal = _fetch_calendar_days(uw, days)
        empty_msg = _no_events_text_days(days)
        span = (min(days), max(days)) if days else None
    else:
        events, raw_cal = _fetch_calendar_range(uw, start, end if end is not None else start)
        empty_msg = _no_events_text(start, end if end is not None else start)
        span = (start, end if end is not None else start)
    if span:
        # The CLI adds the watchlist names the session calendar omits (unknown time, estimated
        # date); the dashboard Fetch did not, so they dropped off the board (#198 E15).
        from RevenueChaser.module_earnings_radar.watchlist_augment import augment_calendar
        augment_calendar(uw, events, raw_cal, _load_watchlist(), span[0], span[1],
                         days=days if dates else None)
    calendar_sources = dict(_CALENDAR_SOURCES) or {"stored": 0, "live": 0, "session": None}
    if not events:
        gap = _provider_gap_text(uw)
        if gap:
            # ER-1: an empty calendar after a rejected key or an outage is not
            # "no earnings scheduled" — say what actually happened.
            message = f"Could not load the earnings calendar: {gap}"
            _write_empty_board(EARNINGS_LATEST, "Earnings Radar", message,
                               "Run Fetch again later; the previous calendar is not shown.")
            _record_earnings_history([], {}, uw=uw, span=span, events=events, reason=message, run_at=run_at,
                                     input_sources={"calendar": calendar_sources})
            raise JobMessage(message)
        message, hint = empty_msg
        _write_empty_board(EARNINGS_LATEST, "Earnings Radar", message, hint)
        _record_earnings_history([], {}, uw=uw, span=span, events=events, reason=message, run_at=run_at,
                                 input_sources={"calendar": calendar_sources})
        raise JobMessage(message)

    tickers = list(dict.fromkeys(e["ticker"] for e in events))

    try:
        from RevenueChaser.module_sector_rotation.engine import rotation_quadrant_map
        etf_quadrant_map = rotation_quadrant_map(uw)
    except Exception as e:
        logger.warning(f"Rotation engine failed: {e}")
        etf_quadrant_map = {}
    rotation_failed = not etf_quadrant_map          # raised, or came back empty (SPY weekly bars short): no quadrants

    pipeline = EarningsPipeline(tickers, uw, raw_cal, etf_quadrant_map=etf_quadrant_map)
    # What the calendar read came from (E5a, #372): pairs covered by the store vs pairs asked of UW live.
    sources = getattr(pipeline, "input_sources", None)
    if isinstance(sources, dict):
        sources["calendar"] = calendar_sources
    cards = pipeline.run(use_preselect=True)
    job_id = _freeze_run_id()           # one id for the vendor rows and the prints (a second call could differ)
    vendor = getattr(pipeline, "vendor", None)
    if vendor is not None:              # the UW statements the run read, stored with the job's id (#297)
        vendor.flush(run_id=job_id, label="Earnings")
    prints = getattr(pipeline, "earnings_capture", None)
    if prints is not None:              # the UW earnings rows the run read, appended on change (#298, D13)
        prints.flush(run_id=job_id, label="Earnings")

    from RevenueChaser.module_earnings_radar.report import export_html
    from RevenueChaser.module_earnings_radar import history as er_history

    nrc = _compute_nrc_results(cards)
    # A partial/failed screener answer is one story, told on the board and in the history note (E2, #372).
    screener = getattr(pipeline, "screener_result", None)
    export_html(cards, events, nrc, filename=str(EARNINGS_LATEST.relative_to(OUTPUT_DIR)),
                filter_stats=pipeline._filter_stats, provider_health=_uw_health(uw),
                notice=er_history.screener_problem(screener), run_at=run_at)
    # Result history (#278): the board file is written, a JobMessage below still follows. A Refresh has no path here.
    no_cards = (_no_cards_text(len(tickers), pipeline._filter_stats, _provider_gap_text(uw))
                if not cards else None)
    _record_earnings_history(cards, nrc, uw=uw, span=span, events=events, filter_stats=pipeline._filter_stats,
                             rotation_failed=rotation_failed, reason=no_cards, screener=screener, run_at=run_at,
                             input_sources=getattr(pipeline, "input_sources", None))
    if not cards:
        # E23: an empty table with a "done" job reads as a broken Fetch. Say why.
        raise JobMessage(no_cards)
    return EARNINGS_LATEST


def run_leaps_latest(uw: UWClient) -> Path:
    from RevenueChaser.module_leaps.scan import scan_leaps
    from RevenueChaser.module_leaps.report import render_leaps_dashboard
    from RevenueChaser.module_leaps import history as leaps_history
    notes: list[str] = []
    footprints: list[dict] = []                # every watchlist footprint, for the result history (#377)
    run, run_at = _freeze_run_id(), time.time()
    watched, unwatched = scan_leaps(uw, warnings=notes, footprints=footprints)
    health = _uw_health(uw)
    render_leaps_dashboard(watched, unwatched, watchlist=_load_watchlist(),
                           filename=str(LEAPS_LATEST.relative_to(OUTPUT_DIR)),
                           provider_health=health, notice=" ".join(notes) or None)
    # Result history (#377): after the board file, so it holds what the board showed. A failure is a job note,
    # never a failed Fetch: the board file does not depend on it.
    try:
        leaps_history.record(footprints, run_id=run, run_at=run_at, uw_health=health, warnings=notes,
                             shown={"watched": len(watched), "unwatched": len(unwatched)})
    except Exception as exc:
        logger.exception("LEAPS result history failed")
        ledger.add_note(f"LEAPS result history was not recorded: {type(exc).__name__}: {exc}")
    if not watched and not unwatched:
        # The empty board is written above; say why on the job too (O13).
        raise JobMessage(_provider_gap_text(uw)
                         or "No far-dated flow prints in the lookback window and no watchlist "
                            "footprint stored — the board shows its empty state.")
    return LEAPS_LATEST


def last_fetch_notice(board: str, *, partial: bool = False) -> str | None:
    """C24 (#383): the last Fetch's notes for a Refresh, from its result-history header (K2, ``history.last_run``).

    The header's ``warnings``, after its partial reason when ``partial`` (13F records "names not refreshed" there),
    as one line marked with the time the Fetch ran. ``None`` when no run is recorded, it has no notes, or the history
    cannot be read (a Refresh never fails on it). A read never creates the history file, and waits only briefly for
    another RevenueChaser process that holds it.
    """
    from datetime import datetime, timezone
    from RevenueChaser.core import history, rawstore
    try:
        with rawstore.brief_wait():
            head = history.last_run(board)
    except Exception as exc:  # noqa: BLE001 - a missing note line, never a failed Refresh
        logger.warning("%s: last Fetch notes not read: %s", board, exc)
        return None
    if not head:
        return None
    notes = [head["quality_note"]] if partial and head.get("quality") == "partial" and head.get("quality_note") else []
    notes += [str(w) for w in (head.get("warnings") or []) if w]
    if not notes:
        return None
    try:
        when = datetime.fromtimestamp(float(head["run_at"]), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        when = "time unknown"
    return f"Last Fetch ({when}): " + " ".join(notes)


def run_leaps_cached(uw=None) -> Path:
    """Model-only LEAPS render from the store — no network.

    Reads the last cached footprint per watchlist ticker (``leaps_cache``) and
    the stored far-dated prints (``leaps_flow_prints``), then renders the board.
    The dashboard's Refresh uses this; the live UW re-scan is ``run_leaps_latest``.
    The last Fetch's notes come back from its history header (C24, #383).
    """
    from RevenueChaser.module_leaps.scan import scan_leaps_cached
    from RevenueChaser.module_leaps.report import render_leaps_dashboard
    watched, unwatched = scan_leaps_cached()
    render_leaps_dashboard(watched, unwatched, watchlist=_load_watchlist(),
                           filename=str(LEAPS_LATEST.relative_to(OUTPUT_DIR)),
                           notice=last_fetch_notice("module_leaps"))
    return LEAPS_LATEST


def run_rotation_latest(uw: UWClient, benchmark: str = "SPY", start: str = None,
                        end: str = None) -> Path:
    from RevenueChaser.module_sector_rotation.engine import (
        RotationEngine, fetch_all_holdings, sector_etf_trees,
    )
    from RevenueChaser.module_sector_rotation.report import export_rotation_dashboard
    from RevenueChaser.module_sector_rotation import history as rotation_history, store as rotation_store

    run, run_at, window, saved, kept, notes = _freeze_run_id(), time.time(), None, [], None, []
    short, stale = [], None
    if start or end:
        # Range fetch: reconstruct snapshots for every week in [start, end].
        from datetime import date as _date
        e = _date.fromisoformat(end) if end else _date.today()
        if start:
            s = _date.fromisoformat(start)
            if e < s:  # picker sent the ends the other way round
                s, e = e, s
        else:
            # Open start: run from the earliest stored date. With an empty store
            # the fallback must not overshoot ``end`` (defaulting to today would
            # silently widen an end-bounded request past what was asked for).
            dates = rotation_store.list_snapshot_dates()
            s = _date.fromisoformat(dates[0]) if dates else e
            s = min(s, e)
        window = (s, e)
        backfill = rotation_store.backfill_weekly(uw, s, e)
        # Days the backfill refused to write (D01 short of SPY for them) go in the
        # job message, so a gap in the trail is explained instead of looking like a quiet week.
        backfill_note = getattr(backfill, "message", lambda: "")()
        if backfill_note:
            notes.append(backfill_note)
        # Load only the requested window (loading every stored date made the range picker a no-op),
        # then keep each ticker's newest row in it: one map row per ETF, and the header shows the
        # newest date (DF66). The trail page shows the weeks.
        snapshots = _latest_per_ticker(_load_rotation_snapshots(s, e))
    else:
        backfill_note = ""
        # M-ROT-2: D01 weekly closes (no UW candle), dated by the session they end on (#283).
        engine = RotationEngine(uw, benchmark=benchmark)
        snapshots = engine.process_all()
        if not snapshots:
            return _rotation_empty_fetch(uw, run, run_at, benchmark)
        short = list(getattr(engine, "too_short", None) or [])
        stale = getattr(engine, "stale", None)
        early = stale or ("an intraday run (the US session is open)" if getattr(engine, "intraday", False) else None)
        # ROT-5: a symbol with no series is missing. A thinner pull must not replace a session snapshot that covers
        # more ETFs: z-scores are cross-sectional, so every other point would move too. #283: a run that is not a
        # complete session (intraday, or D01 short of the session) never replaces a stored one at least as wide.
        # The stored rows are shown and kept; the job says why. Too-short symbols are a warning, never missing.
        missing = _rotation_missing(snapshots, short)
        stored = _stored_session_snapshot(snapshots, same_width=bool(early)) if (missing or early) else []
        if stored:
            why = "; ".join(filter(None, [
                f"{len(missing)} of {len(missing) + len(snapshots)} rotation ETFs are missing from the live RRG "
                f"pull (no stored series up to the session): {', '.join(missing)}" if missing else "",
                f"this run is not a complete session ({early})" if early else ""]))
            kept = (f"{why}; kept the stored snapshot of the {stored[0].as_of_date.isoformat()} session "
                    f"({len(stored)} ETFs)")
            notes.append(f"{kept}.")
            snapshots, stale = stored, None
        else:
            day = getattr(snapshots[0], "as_of_date", None)
            saved = rotation_store.save_snapshot(snapshots, as_of=day, benchmark=benchmark) or []
            if missing:
                notes.append(f"{len(missing)} of {len(missing) + len(snapshots)} rotation ETFs are missing from the "
                             f"live RRG pull (no stored series up to the session): {', '.join(missing)}. Saved without "
                             "them; the run is recorded as partial.")
            if stale:
                notes.append(f"{stale}.")
        if short:
            notes.append(f"{rotation_history.short_warning(short)}.")
    # Embed holdings so the served map is self-contained (expandable rows work
    # even if the holdings route is unavailable / server is stale). Theme rows
    # only (ROT-4): sector rows expand to the sector -> industry tree
    # (cap-weighted, from the shared universe) rather than a 10-name ETF snapshot.
    holdings = fetch_all_holdings(uw, snapshots) if snapshots else {}
    sector_holdings = sector_etf_trees() if snapshots else {}
    health = _uw_health(uw)
    html_path = export_rotation_dashboard(snapshots, filename=str(ROTATION_LATEST.relative_to(OUTPUT_DIR)),
                                          uw=uw, benchmark=benchmark, holdings=holdings,
                                          sector_holdings=sector_holdings,
                                          provider_health=health)
    # Result history (#374): after the board file, so it holds what the board showed.
    _record_rotation(rotation_history, snapshots, holdings, sector_holdings, run, run_at, health, benchmark,
                     window=window, warnings=[backfill_note] if backfill_note else None, snapshot_rows=saved,
                     kept=kept, too_short=short, stale=stale)
    if notes:
        raise JobMessage(" ".join(notes))
    return html_path


def _record_rotation(rotation_history, snapshots, holdings, sector_holdings, run, run_at, health, benchmark,
                     **kw) -> None:
    """Result history (#374). A failure is a job note, never a failed Fetch: the board file does not depend on it."""
    try:
        rotation_history.record_rotation(snapshots, holdings, sector_holdings, run_id=run, run_at=run_at,
                                         uw_health=health, benchmark=benchmark, **kw)
    except Exception as exc:
        logger.exception("Rotation result history failed")
        ledger.add_note(f"Rotation result history was not recorded: {type(exc).__name__}: {exc}")


def _rotation_missing(snapshots, short=()) -> list[str]:
    """Rotation symbols the live pull did not return, in universe order; the too-short ones (a warning) are not."""
    from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS
    got = {s.ticker for s in snapshots}
    return [t for t in ROTATION_ETFS if t != "SPY" and t not in got and t not in short]


def _stored_session_snapshot(snapshots, same_width: bool = False) -> list:
    """The stored snapshot of the live pull's session when it holds every ETF the pull returned and more, else ``[]``.
    With ``same_width`` (a run that is not a complete session) one holding exactly the same ETFs qualifies too.

    A complete stored snapshot always qualifies. So does one that lacks only ETFs this pull lacks too: an ETF whose
    series is short on every run must not make every stored session replaceable by a thinner pull."""
    from RevenueChaser.module_sector_rotation import store as rotation_store
    from RevenueChaser.module_sector_rotation.report import _rows_to_snapshots
    day = getattr(snapshots[0], "as_of_date", None)
    if day is None:
        return []
    rows = (rotation_store.load_snapshot(day) or {}).get("etfs") or []
    stored, live = {r.get("ticker") for r in rows}, {s.ticker for s in snapshots}
    return _rows_to_snapshots(rows) if rows and (stored > live or (same_width and stored >= live)) else []


def _rotation_empty_fetch(uw, run, run_at, benchmark) -> Path:
    """A live Fetch whose RRG pull returned no ETF (ROT-5): the board is not blanked.

    The board file stays as it is; only a first run, with no board yet, writes the empty map. Either way the
    history header records the run as partial and the job says so."""
    from RevenueChaser.module_sector_rotation.report import export_rotation_dashboard
    from RevenueChaser.module_sector_rotation import history as rotation_history
    health = _uw_health(uw)
    if ROTATION_LATEST.exists():
        what = "the board was not rewritten (it keeps its last render)"
    else:
        export_rotation_dashboard([], filename=str(ROTATION_LATEST.relative_to(OUTPUT_DIR)), uw=uw,
                                  benchmark=benchmark, provider_health=health)
        what = "the board shows its empty state"
    _record_rotation(rotation_history, [], {}, {}, run, run_at, health, benchmark)
    raise JobMessage(" ".join(filter(None, [
        _provider_gap_text(uw), f"The live RRG pull returned no ETF; {what}."])))


def _latest_per_ticker(snapshots) -> list:
    """Each ticker's newest snapshot, in first-seen order (DF66)."""
    newest: dict = {}
    for snap in snapshots:
        cur = newest.get(snap.ticker)
        if cur is None or snap.as_of_date >= cur.as_of_date:
            newest[snap.ticker] = snap
    return list(newest.values())


def _load_rotation_snapshots(start: date = None, end: date = None):
    """Stored rotation snapshots as ETFRotationSnapshot objects.

    With ``start``/``end`` only that window is loaded (one query), which is what
    the range picker asks for. Without them every stored date is returned.
    """
    from RevenueChaser.module_sector_rotation import store as rotation_store
    from RevenueChaser.module_sector_rotation.report import _rows_to_snapshots
    out = []
    if start is not None or end is not None:
        dates = rotation_store.list_snapshot_dates()
        lo = start.isoformat() if start else (dates[0] if dates else "")
        hi = end.isoformat() if end else (dates[-1] if dates else "")
        for _, rows in sorted(rotation_store.load_range(
                date.fromisoformat(lo), date.fromisoformat(hi)).items()):
            out.extend(_rows_to_snapshots(rows))
        return out
    for d in rotation_store.list_snapshot_dates():
        snap = rotation_store.load_snapshot(date.fromisoformat(d))
        if snap:
            out.extend(_rows_to_snapshots(snap.get("etfs", [])))
    return out


_MF_WINDOW, _MF_TOP_N = 21, 10           # the Money Flow leaderboard: 21-day window, top 10 per side


def run_money_flow_latest(uw: UWClient, days: int = 30) -> Path:
    """Compute the 21-day money-flow leaderboard (top 10) and cache to JSON."""
    import json
    from datetime import datetime
    from RevenueChaser.module_sector_rotation.money_flow import compute_money_flow

    run, run_at = _freeze_run_id(), time.time()
    result = compute_money_flow(uw, days=days, window=_MF_WINDOW, top_n=_MF_TOP_N)
    record = functools.partial(_record_money_flow, result, run, run_at, uw, days)   # result history (#374)
    partial = bool(result.get("partial"))
    empty = not (result.get("inflows") or result.get("outflows"))
    try:
        prev = json.loads(MONEY_FLOW_LATEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    shown = bool(prev.get("inflows") or prev.get("outflows"))
    prev_when = prev.get("as_of") or prev.get("generated_at") or "the last run"
    missing = result.get("missing") or []
    why = ("Money flow came back empty (no stored daily bars to rank)" if empty else
           f"Money flow is partial ({len(missing)} ETF(s) with no stored daily bars up to the session: "
           f"{', '.join(missing)})")
    if empty or partial:
        # The guard is the newest complete session in result history (ROT-9, K2), not the regenerable .cache file:
        # a thin pull never replaces it, and a deleted board file is rebuilt from it.
        try:
            stored, read = _money_flow_stored(), True
        except Exception as exc:  # noqa: BLE001 - a history fault falls back to the board file, as before
            logger.warning("Money Flow history read failed (%s: %s); the guard reads the board file",
                           type(exc).__name__, exc)
            stored, read = None, False
        if stored:
            # A missing, empty or partial board file is rebuilt from the complete session, so the board shows the
            # leaderboard the message names (review 2026-10-16).
            if not MONEY_FLOW_LATEST.exists() or not shown or prev.get("partial"):
                board = {k: stored[k] for k in ("inflows", "outflows", "generated_at", "as_of")}
                _write_output("sector_rotation", json.dumps(board, indent=2), "latest", ext="json")
                kept = stored["as_of"] or "the last complete run"
            else:
                kept = prev_when
            record(kept=kept)
            raise JobMessage(f"{why}; kept the stored leaderboard from {kept}.")
        # Nothing complete recorded: an empty pull still never replaces a board file with rows (PL2 / N1), and when
        # history could not be read a partial one does not replace a whole board file (#166).
        if shown and (empty or (not read and not prev.get("partial"))):
            record(kept=prev_when)
            raise JobMessage(f"{why}; kept the stored leaderboard from {prev_when}.")
    result["generated_at"] = datetime.now().isoformat()
    _write_output("sector_rotation", json.dumps(result, indent=2), "latest", ext="json")
    record()
    if partial:
        raise JobMessage(f"{why}; saved flagged on the board.")
    return MONEY_FLOW_LATEST


def _money_flow_stored() -> dict | None:
    """The newest complete Money Flow leaderboard in result history (``history.stored_leaderboard``)."""
    from RevenueChaser.module_sector_rotation import history as rotation_history
    return rotation_history.stored_leaderboard()


def _record_money_flow(result: dict, run: str, run_at: float, uw, days: int, kept: str | None = None) -> None:
    """Result history (#374): after the leaderboard file, or where the stored one was kept. A failure is a job note,
    never a failed Fetch: the board file does not depend on it."""
    from RevenueChaser.module_sector_rotation import history as rotation_history
    try:
        rotation_history.record_money_flow(result, run_id=run, run_at=run_at, days=days, window=_MF_WINDOW,
                                           top_n=_MF_TOP_N, uw_health=_uw_health(uw), kept=kept)
    except Exception as exc:
        logger.exception("Money Flow result history failed")
        ledger.add_note(f"Money Flow result history was not recorded: {type(exc).__name__}: {exc}")


def run_sec_13f_latest(uw: UWClient | None = None, notice: str | None = None,
                       provider_health: dict | None = None, capture: dict | None = None) -> Path:
    """Render the SEC 13F total-ownership dashboard from cached totals.

    No API calls — reads ``sec13f_totals``, the meta and the snapshot inputs from the store: shares outstanding
    and insider % from ``ownership_snapshot``, quarter-end prices from D01 (``enrich.load_inputs``, #375 F2; the
    retired ``sec13f_enrichment`` is not read). The Fetch renders through here too, so a Refresh shows the numbers
    the Fetch showed. The board banner comes from ``provider_health`` (or ``uw.health()`` when a client is
    passed), and ``notice`` adds a one-line note (e.g. "names were not refreshed"). ``capture`` (the Fetch only)
    receives the totals, meta and inputs (under ``enrichment``) the board was rendered from, for the result
    history (#375).
    """
    from RevenueChaser.module_sec_13f import engine as sec_13f
    from RevenueChaser.module_sec_13f.enrich import load_inputs
    from RevenueChaser.module_sec_13f.report import export_sec13f_dashboard

    totals = sec_13f.load_totals()
    if capture is not None:
        capture["totals"] = totals
    if not totals:
        # Say so on the board instead of leaving the old (or no) file behind.
        _write_empty_board(
            SEC_13F_LATEST, "SEC 13F", "No 13F ownership totals are stored yet.",
            "Press Fetch to download and ingest the newest SEC Form 13F data set.")
        from RevenueChaser.core.reporting_common import payload_path
        payload_path(SEC_13F_LATEST).unlink(missing_ok=True)     # K10: the rows go with the rows' file (#429)
        raise JobMessage("No 13F totals stored yet — press Fetch to ingest the SEC data set.")
    meta, enrichment = sec_13f.load_sec13f_meta(), load_inputs(totals)
    if capture is not None:
        capture.update(meta=meta, enrichment=enrichment)
    export_sec13f_dashboard(totals, meta, enrichment,
                            filename="sec_13f_latest.html",
                            provider_health=provider_health or _uw_health(uw), notice=notice)
    return SEC_13F_LATEST


def run_sec13f_check(uw=None) -> None:
    """Ask the SEC whether a new Form 13F data set is out — downloads nothing.

    Always ends with ``JobMessage`` so the board says what it found: the new
    set(s) a Fetch would ingest, or "up to date" with the newest listed name.
    """
    from RevenueChaser.module_sec_13f import engine as sec_13f
    res = sec_13f.check_new_zips()
    if not res["latest"]:
        raise JobMessage("The SEC Form 13F page lists no data sets — try again later.")
    if res["new"]:
        names = ", ".join(res["new"])
        raise JobMessage(f"New 13F data set(s) available: {names} — press Fetch to ingest.")
    raise JobMessage(f"Up to date — newest SEC data set {res['latest']} is already ingested.")


def build_sec13f_meta(uw: UWClient) -> dict:
    """Enrich the full SEC 13F universe (totals keys) with name/sector/type.

    Writes the dedicated 13F security meta so the 13F board covers every ticker
    in the totals, not just a watchlist.
    """
    from RevenueChaser.module_sec_13f import engine as sec_13f
    from RevenueChaser.module_sec_13f.universe_info import fetch_universe_info

    totals = sec_13f.load_totals()
    universe = sorted({t for t in totals if t})
    info = fetch_universe_info(uw, universe)
    # Start from the stored meta, not from blank: a failed or partial screener
    # pass must not wipe names/sectors it did not return (PL2 / N1, O18).
    stored = sec_13f.load_sec13f_meta()
    meta = {t: stored.get(t) or {"name": "", "sector": UNCLASSIFIED, "type": "Other"}
            for t in universe}
    if not info and stored:
        logger.warning("13F meta: screener returned nothing; kept %d stored rows",
                       sum(1 for t in universe if t in stored))
    _apply_universe_info(meta, info)
    sec_13f.save_sec13f_meta(meta)
    return meta


def _apply_universe_info(meta: dict, info: dict) -> None:
    """Merge screener info (name/sector/type) into the shared meta cache.

    ``sector`` is a Morningstar sector name (UW's list; ``universe_info.resolve_sector`` maps the screener's
    spelling through ``canonical_sector``), an asset class (crypto, indices) or unknown. The cache stores the
    unknown bucket, ``Unclassified``, either way (a legacy ``Other`` reads as unknown too). ``type`` is the
    security type (ETF / Stock / Other), not a sector value.
    """
    for t, i in info.items():
        if i.get("name") or not is_unclassified(i.get("sector")):
            meta[t] = {"name": i.get("name", ""), "sector": sector_or_unclassified(i.get("sector")),
                       "type": i.get("type", "Other")}


def _13f_map_step(run: str, run_at: float, *, first_run: bool = False) -> tuple[dict | None, str | None, dict]:
    """The CUSIP map step (#207, D19): ``(cusip_map, note, stats)``.

    Asks OpenFIGI (``core/providers/openfigi.py``, at most 400 requests) about the CUSIPs ``map_candidates`` lists
    (held, never asked) and records the answers in ``identity_cusip``. ``cusip_map`` is the whole new map when every
    candidate was answered: OpenFIGI's (``identity_cusip.current_map``) plus the stored legacy entries whose issuer
    prefix OpenFIGI confirms for the same ticker and whose issue number is not 9x (``legacy_fallback``, avo's option
    B minus 9x). The derive then uses it and, if it passes the guards, rebuilds ``cusip_ticker`` from it. ``None``
    otherwise, with ``note`` saying why: the derive uses the stored map. A Fetch (``first_run=False``) asks only once
    the map is seeded: the first map (about 380 requests) runs through the 13F map job (``run_sec13f_map``), after
    the dry run on a store copy. Never raises: a provider or store failure is the note.
    """
    from RevenueChaser.core import identity_cusip
    from RevenueChaser.core.providers import openfigi
    from RevenueChaser.module_sec_13f import engine as sec_13f
    stats: dict = {"candidates": 0, "requests": 0, "rate_limited": 0, "answered": 0, "present": 0, "absent": 0,
                   "job_errors": 0, "appended": 0, "legacy_kept": 0, "legacy_other_ticker": 0,
                   "legacy_unknown_prefix": 0, "legacy_option_like": 0, "legacy_strict_dropped": 0}
    try:
        candidates = sec_13f.map_candidates()
        stats["candidates"] = len(candidates)
        if candidates:
            if not first_run and not identity_cusip.has_rows():
                return None, ("CUSIP map: OpenFIGI has not mapped the 13F holdings yet (#207 first run); "
                              "the totals use the stored map."), stats
            result = openfigi.map_cusips(candidates)
            stats.update(requests=result.requests, rate_limited=result.rate_limited, answered=len(result.answers),
                         present=sum(a["status"] == openfigi.PRESENT for a in result.answers.values()),
                         absent=sum(a["status"] == openfigi.ABSENT for a in result.answers.values()),
                         job_errors=result.job_errors)
            if result.answers:
                stats["appended"] = identity_cusip.record(result.answers, run_id=run, run_at=run_at)["appended"]
            logger.info("13F CUSIP map: %s", stats)
            if result.status == "skipped":
                return None, (f"CUSIP map: not refreshed, {result.error}; {len(candidates)} new CUSIP(s) wait for "
                              "it. The totals use the stored map."), stats
            if not result.complete:
                return None, (f"CUSIP map: incomplete, {len(result.unasked)} of {len(candidates)} new CUSIP(s) not "
                              f"asked ({result.error}); the totals use the stored map."), stats
        openfigi_map = identity_cusip.current_map()
        if not openfigi_map:
            return None, None, stats
        cusip_map, fallback = sec_13f.legacy_fallback(openfigi_map, sec_13f.load_cusip_ticker(),
                                                      identity_cusip.ever_mapped(), strict=sec_13f._FALLBACK_STRICT)
        stats.update(legacy_kept=len(fallback["kept"]), legacy_other_ticker=len(fallback["other_ticker"]),
                     legacy_unknown_prefix=len(fallback["unknown_prefix"]),
                     legacy_option_like=len(fallback["option_like"]),
                     legacy_strict_dropped=len(fallback["strict_dropped"]))
        logger.info("13F CUSIP map, legacy prefix fallback: %s", {k: len(v) for k, v in fallback.items()})
        return cusip_map, None, stats
    except Exception as exc:  # noqa: BLE001 - the derive still runs on the stored map
        logger.exception("13F CUSIP map step failed")
        return None, f"CUSIP map step failed ({type(exc).__name__}: {exc}); the totals use the stored map.", stats


def _refresh_13f_meta(uw, ingested: int) -> tuple[dict | None, str | None, UWClient | None]:
    """The UW name/sector step, best-effort: ``(provider_health, note, client)``.

    The 13F job needs no UW key (the SEC data is the board), so a missing key or an
    outage only means the stored names and sectors stay as they were; ``note`` says
    so for the board. Skipped entirely (F-1) when nothing new was ingested and the
    stored meta already covers every ticker in the totals. A client is opened here
    when the runner was not given one, and returned so the caller can close it.
    """
    from RevenueChaser.module_sec_13f import engine as sec_13f
    universe = {t for t in sec_13f.load_totals() if t}
    meta = sec_13f.load_sec13f_meta()
    # Covered = a real name or sector. A failed UW pass leaves ''/Unclassified
    # placeholders; counting those would skip the retry until a new zip lands (B2).
    covered = {t for t, m in meta.items()
               if (m or {}).get("name") or not is_unclassified((m or {}).get("sector"))}
    if not universe or (not ingested and universe <= covered):
        return None, None, None
    own = None
    if uw is None:
        uw = own = UWClient()
    try:
        ok, message = uw.verify()
        if not ok:
            return None, f"Names and sectors were not refreshed: {message}", own
        try:
            build_sec13f_meta(uw)
        except Exception as exc:  # noqa: BLE001 - best-effort: the SEC data stands without it
            logger.warning("13F meta step failed: %s", exc)
            return None, f"Names and sectors were not refreshed: {exc}", own
        health = _uw_health(uw)
        gaps = int((health or {}).get("failed") or 0)
        note = ("Names and sectors may be incomplete: some Unusual Whales calls failed."
                if gaps else None)
        return health, note, own
    except Exception:
        if own is not None:
            own.close()
        raise


def _sec13f_busy_message() -> str:
    """The message a refused 13F job ends with: another Fetch, rebuild or map holds the family's lock (#26)."""
    pid = dict(job_lock.holders()).get("sec_13f", 0)
    who = f" (pid {pid})" if pid else ""
    return f"A 13F job is already running{who}; nothing was written."


def build_sec13f(uw: UWClient | None = None) -> Path:
    """The 13F Fetch: download -> ingest (SEC) -> derive (store) -> names -> shares out -> render -> history.

    The job needs no Unusual Whales key. The SEC stages run only when the data-set
    page is reachable; the derive (``recompute_totals``) and the render always run,
    and an SEC failure is raised at the end, after the board is up to date. The UW
    name/sector step is best-effort (``_refresh_13f_meta``), and so is the UW shares
    outstanding step (``_store_13f_shares``, #300 D20), which replaced the 13F enrichment. The shares step runs
    before the render (#375 F2): the board's % Out reads the rows it stores.

    One 13F job at a time (#26): the ``"sec_13f"`` lock covers this job, ``run_sec13f_rebuild`` and
    ``run_sec13f_map``, because they all write the ``sec13f_*`` tables and the CUSIP map. A second one refuses with a
    ``JobMessage`` and writes nothing.
    """
    with job_lock.acquire("sec_13f", wait=0) as got:
        if not got:
            raise JobMessage(_sec13f_busy_message())
        return _build_sec13f(uw)


def _build_sec13f(uw: UWClient | None = None) -> Path:
    """The Fetch's work, once ``build_sec13f`` holds the ``"sec_13f"`` lock."""
    from RevenueChaser.module_sec_13f import engine as sec_13f

    run, run_at = run_id(), time.time()
    sec_error = None
    ingested = 0
    ingest_failed = False
    try:
        sec_13f.download_missing_zips()
        zips = sorted(str(p) for p in sec_13f.SEC13F_DIR.glob("*_form13f.zip"))
        try:
            ingested = sec_13f.ingest_zips(zips) if zips else 0
        except Exception:
            ingest_failed = True  # a half-written zip: never derive from it (B1)
            raise
    except Exception as exc:  # noqa: BLE001 - reported below, once the board is rendered
        logger.warning("13F SEC stage failed: %s", exc)
        sec_error = exc

    unmatched, summary, map_note = None, None, None
    if not ingest_failed:  # a download failure alone leaves the store consistent
        cusip_map, map_note, _stats = _13f_map_step(run, run_at)
        try:
            try:
                sec_13f.recompute_totals(cusip_map)
            except sec_13f.DeriveRefused as exc:
                if cusip_map is None:
                    raise
                # A new map the guards refuse must not fail every later Fetch: derive with the stored map, say so.
                logger.warning("13F derive with the new CUSIP map refused: %s", exc)
                map_note = (f"CUSIP map: the new map was not applied, the derive with it was refused ({exc}) "
                            "The totals use the stored map.")
                sec_13f.recompute_totals()
            unmatched, summary = _13f_unmatched()
        except sec_13f.DeriveRefused as exc:
            logger.warning("13F derive refused: %s", exc)
            sec_error = sec_error or exc

    health, note, own = None, None, None
    shown: dict = {}                        # what the board was rendered from, for the result history (#375)
    try:
        health, names_note, own = _refresh_13f_meta(uw, ingested)
        note = " ".join(n for n in (names_note, map_note, unmatched) if n) or None
        _store_13f_shares(uw or own)        # before the render: the board's % Out reads these rows (#375 F2)
        path = _render_13f(health, note, shown)
        _record_13f(shown, run, run_at, sec_error, names_note, unmatched, ingested,
                    sec_13f.map_identity(sec_13f.load_cusip_ticker(), summary), map_note)
    finally:
        if own is not None:
            own.close()

    if sec_error is not None:
        raise sec_error
    if note:
        raise JobMessage(note)
    return path


def _store_13f_shares(uw: UWClient | None) -> None:
    """D20 (#300): UW ``info`` shares outstanding of every 13F ticker into ``ownership_snapshot``, best-effort.

    One call per ticker, once per complete session, at most ``ownership.UW_MAX_CALLS_PER_FETCH`` per Fetch (the job
    cannot be cancelled, so the cap is the bound; a capped run is one job note). An intraday run, or a session whose
    tickers are all stored, asks nothing and opens no client (``ownership.pending_uw``). A client is opened here
    only when the runner has none, and closed here. A key that fails ``verify`` (cached per client) asks nothing; it
    and a store error are a job note, never a failed Fetch: the board then shows the shares stored before.
    """
    from RevenueChaser.core import ownership
    from RevenueChaser.module_sec_13f import engine as sec_13f

    run_at = ownership._clock()
    _, todo = ownership.pending_uw(sec_13f.load_totals(), run_at)
    if not todo:
        return
    own = None
    try:
        if uw is None:
            uw = own = UWClient()
        ok, message = uw.verify()          # cached per client: free when the meta step already asked
        if not ok:
            ledger.add_note(f"13F shares outstanding were not stored: {message}")
            return
        res = ownership.store_uw_shares(uw, todo, run_id=run_id(), run_at=run_at)
        logger.info("13F shares outstanding: %d asked, %d stored, %d without a value, %d left (session %s)",
                    res["asked"], res["written"], res["no_value"], res["left"], res["session"])
        if res["left"]:
            ledger.add_note(f"13F shares outstanding: the cap of {ownership.UW_MAX_CALLS_PER_FETCH:,} UW calls per "
                            f"Fetch was reached; {res['left']:,} ticker(s) left for the next Fetch.")
    except Exception as exc:  # noqa: BLE001 - best-effort, see docstring (a cancel is not an Exception)
        logger.exception("13F shares outstanding were not stored")
        ledger.add_note(f"13F shares outstanding were not stored: {type(exc).__name__}: {exc}")
    finally:
        if own is not None:
            own.close()


def _13f_unmatched() -> tuple[str | None, dict | None]:
    """DF89: the CUSIPs the derive left out (no ticker in the stored map) as ``(one line, summary)``; store read
    only. ``(None, None)`` when the summary fails: a coverage note must never fail the job."""
    from RevenueChaser.module_sec_13f import engine as sec_13f
    try:
        # The note prints dollars next to the totals, so it is on the totals' scale (#208): a filer
        # reporting in thousands is rescaled in both or the two lines disagree by 1000x.
        cusip_ticker = sec_13f.load_cusip_ticker()
        summary = sec_13f.unmatched_summary(cusip_ticker, sec_13f.value_scale_factors(cusip_ticker))
    except Exception as exc:  # noqa: BLE001 - a coverage note must never fail the job
        logger.warning("13F unmatched-CUSIP summary failed: %s", exc)
        return None, None
    note = sec_13f.unmatched_note(summary)
    if note:
        logger.warning("13F derive: %s", note)
    return note, summary


def _render_13f(health: dict | None, note: str | None, capture: dict | None = None) -> Path:
    """Render the board with the best-effort UW banner (``health``) and ``note``."""
    try:
        return run_sec_13f_latest(None, notice=note, provider_health=health, capture=capture)
    except JobMessage:
        return SEC_13F_LATEST  # no totals at all: the empty board was written


def _record_13f(shown: dict, run: str, run_at: float, sec_error: Exception | None, names_note: str | None,
                unmatched: str | None, ingested: int, map_identity: dict | None = None,
                map_note: str | None = None) -> None:
    """Result history (#375): after the board file, so it holds what the board showed, with the identity of the
    CUSIP map the totals were derived with (#207). A failure is a job note, never a failed Fetch: the board file
    does not depend on it."""
    from RevenueChaser.module_sec_13f import history as sec13f_history
    try:
        sec13f_history.record(shown, run_id=run, run_at=run_at,
                              error=f"{type(sec_error).__name__}: {sec_error}" if sec_error is not None else None,
                              names_note=names_note, unmatched=unmatched, ingested=ingested,
                              map_identity=map_identity, map_note=map_note)
    except Exception as exc:
        logger.exception("13F result history failed")
        ledger.add_note(f"13F result history was not recorded: {type(exc).__name__}: {exc}")


def run_sec13f_rebuild(uw=None) -> None:
    """Re-derive ``sec13f_totals`` from the ingested holdings — store only, zero network.

    The offline path for a fix to the totals logic (or the names it matches by): no
    download, no UW. Ends with a ``JobMessage`` saying what it rebuilt.

    One 13F job at a time (#26): a refused run raises a ``JobMessage`` and writes nothing.
    """
    with job_lock.acquire("sec_13f", wait=0) as got:
        if not got:
            raise JobMessage(_sec13f_busy_message())
        _run_sec13f_rebuild()


def _run_sec13f_rebuild() -> None:
    """The rebuild's work, once ``run_sec13f_rebuild`` holds the ``"sec_13f"`` lock."""
    from RevenueChaser.module_sec_13f import engine as sec_13f
    totals = sec_13f.recompute_totals()  # the stored map; DeriveRefused propagates
    if not totals:
        raise JobMessage("No ingested 13F holdings in the store — nothing to rebuild "
                         "(the stored totals were left alone). Press Fetch to ingest a data set.")
    unmatched, _summary = _13f_unmatched()
    _render_13f(None, unmatched)
    latest = max(p for periods in totals.values() for p in periods)
    raise JobMessage(f"Rebuilt 13F totals: {len(totals)} tickers, latest period {latest}."
                     + (f" {unmatched}" if unmatched else ""))


_FALLBACK_DROPS = ("legacy_other_ticker", "legacy_unknown_prefix", "legacy_option_like", "legacy_strict_dropped")


def run_sec13f_map(uw=None) -> None:
    """The 13F CUSIP map job (#207): the map step, then the derive with the new map, then the board.

    OpenFIGI only: at most 400 requests (``core/providers/openfigi.py``), no SEC and no Unusual Whales call, no
    download and no ingest. It is the first live map (avo's typed words, 2026-10-14: after the dry run on a store
    copy) and the way to finish a map a Fetch left incomplete. The answers go to ``identity_cusip``; ``cusip_ticker``
    is rebuilt from them only when the derive with the new map passes the guards (``DeriveRefused`` propagates; the
    stored map and totals are then kept). A Fetch records the result history; this job, like the rebuild, does not.

    One 13F job at a time (#26): a refused run raises a ``JobMessage`` and writes nothing.
    """
    with job_lock.acquire("sec_13f", wait=0) as got:
        if not got:
            raise JobMessage(_sec13f_busy_message())
        _run_sec13f_map()


def _run_sec13f_map() -> None:
    """The map's work, once ``run_sec13f_map`` holds the ``"sec_13f"`` lock."""
    from RevenueChaser.module_sec_13f import engine as sec_13f
    cusip_map, map_note, stats = _13f_map_step(run_id(), time.time(), first_run=True)
    asked = (f"CUSIP map: {stats['answered']} of {stats['candidates']} CUSIP(s) answered in {stats['requests']} "
             f"OpenFIGI request(s) ({stats['rate_limited']} rate-limited): {stats['present']} mapped, "
             f"{stats['absent']} unknown to OpenFIGI, {stats['job_errors']} refused; {stats['appended']} row(s) "
             f"appended to identity_cusip. Legacy fallback: kept {stats['legacy_kept']} legacy CUSIP(s) whose issuer "
             f"prefix OpenFIGI confirms, dropped "
             f"{sum(stats[k] for k in _FALLBACK_DROPS)} "
             f"({stats['legacy_other_ticker']} prefix of another ticker, {stats['legacy_unknown_prefix']} prefix "
             f"unknown to OpenFIGI, {stats['legacy_option_like']} option-like issue number, "
             f"{stats['legacy_strict_dropped']} by B-strict).")
    if map_note:
        raise RuntimeError(f"{asked} {map_note}")
    if cusip_map is None:
        raise JobMessage(f"{asked} No CUSIP is mapped yet: nothing to derive with.")
    totals = sec_13f.recompute_totals(cusip_map)  # DeriveRefused propagates: the stored map and totals stay
    if not totals:
        raise JobMessage(f"{asked} No ingested 13F holdings in the store: the totals were left alone.")
    unmatched, _summary = _13f_unmatched()
    _render_13f(None, unmatched)
    latest = max(p for periods in totals.values() for p in periods)
    raise JobMessage(f"{asked} Derived {len(totals)} tickers, latest period {latest}, with "
                     f"{len(sec_13f.load_cusip_ticker())} mapped CUSIPs." + (f" {unmatched}" if unmatched else ""))


def run_etf_holdings_sync(uw: UWClient) -> None:
    """Bulk-resolve sector+theme ETF holdings via UW->FMP->yfinance, cache once."""
    from datetime import datetime
    from RevenueChaser.core import calendar, rawstore
    from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS
    from RevenueChaser.module_sector_rotation.etf_holdings import get_etf_holdings, holdings_row

    # ``get_etf_holdings`` returns [] when every source failed; that must not
    # replace an ETF's last good holdings (PL2 / N1). Such ETFs keep their
    # stored rows and are named in the job message.
    stored = {r["ticker"]: r.get("entry") for r in rawstore.get_all("etf_holdings")}
    cache, kept, fetched = {}, [], {}
    for ticker, info in ROTATION_ETFS.items():
        if info.get("group") not in ("sector", "theme"):
            continue
        rows = get_etf_holdings(ticker, uw)
        if not rows and stored.get(ticker):
            kept.append(ticker)
            continue
        cache[ticker], fetched[ticker] = rows, calendar._now().timestamp()
    if cache:
        rawstore.write_rows("etf_holdings", [  # #303: the store appends etf_holdings_changes on a change
            holdings_row(t, rows, fetched[t]) for t, rows in cache.items()
        ])
    if any(cache.values()):
        rawstore.write_rows("store_meta", [{
            "key": "etf_holdings_generated_at",
            "value": datetime.now().isoformat(),
        }])
    # The live RS enrichment that used to run inside the holdings GET view now
    # runs here (backend D3/B9): theme ETFs get RS-vs-SPY rows stored for the
    # view. Sector ETFs show the sector -> industry tree instead, so they are
    # skipped (no wasted calls).
    from RevenueChaser.module_sector_rotation import store as rotation_store
    from RevenueChaser.module_sector_rotation.engine import (
        SPY_MIN_CLOSES, fetch_spy_closes, holdings_with_rs_from,
    )
    enriched, rs_kept = 0, []
    # SPY once for the whole job (it used to be fetched again for every theme ETF). Without enough
    # of it every RS would be 0.0, so the theme ETFs keep the RS they already have and are named.
    spy_closes = fetch_spy_closes(uw) if uw is not None else []
    for ticker, info in ROTATION_ETFS.items():
        if uw is None or info.get("group") != "theme" or not cache.get(ticker):
            continue
        if len(spy_closes) < SPY_MIN_CLOSES:
            rs_kept.append(ticker)
            continue
        rows = holdings_with_rs_from(cache[ticker], uw, spy_closes=spy_closes)
        if rows:
            rotation_store.save_holdings_cache(ticker, rows)
            enriched += 1
    msg = (f"Holdings synced for {sum(1 for r in cache.values() if r)} ETFs; "
           f"RS computed for {enriched} theme ETFs.")
    if rs_kept:
        msg += (f" Kept stored RS for {len(rs_kept)} theme ETF(s): SPY candles missing "
                f"(provider failure): {', '.join(rs_kept)}.")
    if kept:
        msg += (f" Kept stored holdings for {len(kept)} ETF(s) whose fetch returned nothing "
                f"(provider failure): {', '.join(kept)}.")
    raise JobMessage(msg)


def spxgamma_latest_path() -> Path:
    """Stable pointer written by the SPX Option Position Map pipeline."""
    from RevenueChaser.module_spx_gamma.config import DATA_ROOT as spx_data_root
    return spx_data_root / "latest.json"


# The SPX report the last Fetch or calendar click built, so the board shows the clicked day. Kept in
# this process only: a restart, or a Refresh, puts the board back on the newest day (latest.json).
_SPX_VIEW: Path | None = None


def spxgamma_view_report() -> Path | None:
    """The report the board shows instead of ``latest.json``'s, or None for the newest day."""
    return _SPX_VIEW


def run_spxgamma(uw, asof_date: str, force: bool = False) -> Path:
    """Run the SPX Option Position Map (Unusual Whales) for one as-of date.

    The module uses the job's own ``uw`` client, so its calls count in the job's provider health.
    A stored past day is rebuilt from disk with no network; ``force`` refetches it (#179 D5).
    """
    global _SPX_VIEW
    from RevenueChaser.module_spx_gamma.run import run as spx_run
    from RevenueChaser.module_spx_gamma.config import DATA_ROOT as spx_data_root

    out = spx_run(asof_date, uw=uw, force=force)
    _SPX_VIEW = (out or {}).get("paths", {}).get("report_html")
    return spx_data_root / "latest.json"


def run_spxgamma_stored(uw, asof_date: str) -> Path:
    """Rebuild a stored past SPX day from disk (#179 D5): no UW client, so no auth probe either.

    Registered with ``requires_uw=False``; the route sends a date here only when
    ``serves_from_disk`` holds. ``refresh`` reads ``raw/<day>`` and never writes it.
    """
    global _SPX_VIEW
    from RevenueChaser.module_spx_gamma.run import refresh
    from RevenueChaser.module_spx_gamma.config import DATA_ROOT as spx_data_root

    out = refresh(asof_date)
    _SPX_VIEW = out["paths"]["report_html"]
    return spx_data_root / "latest.json"


def run_spxgamma_cached() -> Path | None:
    """Refresh for SPX Gamma: rebuild the newest stored day from disk, zero network.

    Returns the rebuilt report, or None when no raw day is stored (a Fetch is needed first).
    """
    global _SPX_VIEW
    from RevenueChaser.module_spx_gamma.run import refresh

    out = refresh()
    _SPX_VIEW = None
    return out["paths"]["report_html"] if out else None


def _remember_quick_ticker(ticker: str, generated_at: str | None = None) -> None:
    """Cache the ticker + render date for the Financials tab in .cache/.

    State is a per-module JSON file (clearable, regenerable) — the view
    recomputes from .store on Refresh (no network) whenever the raw payload
    for the ticker is there. ``generated_at`` is the UTC stamp of the render
    that produced the current artifact and is always surfaced in the UI.
    """
    from datetime import datetime, timezone
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat()
    from RevenueChaser.core.reporting_common import write_artifact
    write_artifact(QUICK_STATE, json.dumps({
        "ticker": ticker.upper(),
        "generated_at": generated_at,
    }))


def _last_quick_ticker() -> str:
    try:
        state = json.loads(QUICK_STATE.read_text(encoding="utf-8"))
        return state.get("ticker") or "AAPL"
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return "AAPL"


def _quick_state() -> dict:
    """Last rendered ticker + generated_at (UTC) — or empty dict when absent."""
    try:
        state = json.loads(QUICK_STATE.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _copy_quick_artifact(html: str) -> bool:
    """Copy a freshly rendered report to the stable .cache artifact."""
    import shutil
    p = Path(html)
    if not p.exists():
        return False
    QUICK_LATEST.parent.mkdir(parents=True, exist_ok=True)
    if p.resolve() != QUICK_LATEST.resolve():
        shutil.copy(p, QUICK_LATEST)
    return True


def run_quick(uw, ticker: str) -> Path:
    """Run Fin Research for one ticker — Quick Remarks (ticker_scores) + Fin Model (DCF/3-stmt/comps/credit/sensitivity).

    yfinance / SEC EDGAR only (``uw`` ignored, no Unusual Whales key required).
    Fetches fresh raw data into .store, then recomputes the report into .cache.
    """
    from RevenueChaser.module_financials.run import run as fm_run
    from RevenueChaser.module_financials import history as fin_history

    # The dashboard keeps one report (financials_latest.html); per-ticker files are the CLI's (#173 D5).
    modules = ["dcf", "3stmt", "comps", "credit", "sensitivity"]
    run = run_id()                            # one id: the vendor rows it stores (#297) and its history header
    res = fm_run(ticker, modules=modules, output="html", report_path=QUICK_LATEST, run_id=run)
    html = res["paths"].get("html")
    written = _copy_quick_artifact(html or "")
    if written:
        _remember_quick_ticker(ticker)
    # Result history (#385): after the board file, so it holds what the board showed. A failure is a job note,
    # never a failed Fetch: the board file does not depend on it.
    try:
        fin_history.record(res.get("results") or {}, ticker=ticker.upper(), run_id=run, run_at=time.time(),
                           modules=modules, report_written=written)
    except Exception as exc:
        logger.exception("Financials result history failed")
        ledger.add_note(f"Financials result history was not recorded: {type(exc).__name__}: {exc}")
    return QUICK_LATEST


def _write_quick_error(path: Path, ticker: str, exc: Exception) -> Path:
    """Write an explicit error board over the Financials artifact.

    A store-only re-render failure used to be swallowed: ``run_quick_cached``
    caught every exception, logged a warning and returned a stale artifact or
    None, so the tab silently emptied and the app fell back to a network Fetch.
    The error board makes the failure visible in the tab itself.
    """
    from datetime import datetime, timezone
    from RevenueChaser.core import templating
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    from RevenueChaser.core.reporting_common import write_artifact
    return write_artifact(path, templating.render(
        "refresh_error.html.j2",
        title=f"Financials refresh failed for {ticker}",
        message=f"{type(exc).__name__}: {exc}",
        detail=str(exc),
        stamp=stamp,
    ))
def run_quick_cached(uw=None, ticker: str | None = None) -> Path:
    """Store-only Financials render — no network.

    Recomputed from ``.store/raw.duckdb`` (raw yfinance payload, TTL ignored),
    so clicking Refresh always ignores any stale ``financials_latest.html`` and
    re-derives the report. When nothing is stored for the requested ticker —
    neither a raw payload nor the annual statements a Fetch stores in
    ``vendor_financials`` (FIN-5); a ``vendor_valuation`` row alone does not
    count — the stale artifact is dropped and None is
    returned, so the view shows an empty state and a live Fetch is genuinely
    required.

    A failure is never silently swallowed: if the store-only render raises, an
    explicit error board is written over ``financials_latest.html`` and returned,
    so the tab says what broke instead of degrading into an empty state and a
    network Fetch. (This used to hide errors like a cleared ``.cache`` behind a
    bare warning + a FileNotFoundError.)

    """
    from RevenueChaser.core import vendor_data
    from RevenueChaser.core.providers import cache as cache_manager
    from RevenueChaser.module_financials.run import run as fm_run

    explicit = ticker is not None
    ticker = (ticker or _last_quick_ticker()).upper()
    # The gate is "no statements stored anywhere", not "no ticker_payload_cache row": a Fetch also stores its
    # statements in ``vendor_financials`` (FIN-5, #385), and ``fm_run(no_fetch=True)`` builds the model inputs from
    # those rows. A ticker with only a ``vendor_valuation`` row (what a comps peer gets) has no annual statements,
    # so it is "nothing stored", not a board of "insufficient financial data" errors.
    if cache_manager.load(ticker, ignore_ttl=True) is None and not (
            (vendor := vendor_data.read_financials(ticker)) and vendor.get("annual")):
        if explicit:
            QUICK_LATEST.unlink(missing_ok=True)  # no raw data -> drop stale artifact
        if QUICK_LATEST.exists():
            return QUICK_LATEST
        return None
    try:
        res = fm_run(ticker, modules=["dcf", "3stmt", "comps", "credit", "sensitivity"],
                     output="html", no_fetch=True, report_path=QUICK_LATEST)
    except Exception as exc:  # noqa: BLE001 - a render failure is a visible error
        logger.error("run_quick_cached %s failed to re-render from the store: %s",
                     ticker, exc, exc_info=True)
        return _write_quick_error(QUICK_LATEST, ticker, exc)
    html = res["paths"].get("html")
    if _copy_quick_artifact(html or ""):
        _remember_quick_ticker(ticker)
        return QUICK_LATEST
    # fm_run reported success but produced nothing to serve — also a hard
    # failure, never something to paper over with a stale artifact or a None.
    exc = RuntimeError(f"store-only render for {ticker} produced no report file")
    logger.error("run_quick_cached %s: %s", ticker, exc)
    return _write_quick_error(QUICK_LATEST, ticker, exc)


def run_asymmetric_growth(uw, refresh_universe: bool = False, no_fetch: bool = False) -> Path:
    """Run the Asymmetric Growth Hunter scan (US-listed, all caps, SEC EDGAR + yfinance).

    ``refresh_universe=True`` re-fetches the shared ticker universe first;
    ``no_fetch=True`` (a Refresh) renders the last Fetch's stored cards (``engine.stored_result``), no scoring.
    """
    import json
    from RevenueChaser.module_asymmetric_growth.engine import run_asymmetric_growth_hunter
    from RevenueChaser.module_asymmetric_growth import history as ag_history
    from RevenueChaser.module_asymmetric_growth.report import board_payload, render_asymmetric_growth_hunter_html
    from RevenueChaser.core import ticker_store
    from RevenueChaser.core.reporting_common import write_board

    from RevenueChaser.module_asymmetric_growth.engine import stored_result

    # One id per Fetch (jobs.py:97): the vendor rows it stores (#297), its history header and its freeze.
    run = None if no_fetch else _freeze_run_id()
    # A Refresh shows the last Fetch read back from the store and never re-scores (S5, Common K4 #383 R1).
    result = (stored_result() if no_fetch else
              run_asymmetric_growth_hunter(uw, refresh_universe=refresh_universe, no_fetch=False, run_id=run))
    _write_output("asymmetric_growth", json.dumps(result, ensure_ascii=False, default=str), "latest", ext="json")
    # The page and its cards (S7, K10 #383 C6a): the cards go to the gzip payload file next to the page.
    write_board(ASYMMETRIC_GROWTH_LATEST, render_asymmetric_growth_hunter_html(result), board_payload(result))
    if not no_fetch:
        # Only a Fetch rewrites the durable per-ticker table; a Refresh renders and leaves it alone (#145 D5).
        ticker_store.write_from_cards(result["cards"], result.get("asof"))
        # Result history (#332): after the latest table, before the freeze. A failure is a job note, never a
        # failed Fetch: the latest table and the freeze below do not depend on it.
        try:
            ag_history.record(result, run_id=run)
        except Exception as exc:
            logger.exception("AG result history failed")
            ledger.add_note(f"AG result history was not recorded: {type(exc).__name__}: {exc}")
        # Last step of a Fetch, after the board files are written. A Refresh (no_fetch) never freezes.
        # The outcome is a job note (core.ledger), not a JobMessage: ``run_universe_refresh`` still has
        # two boards to run. A freeze *error* propagates, so the job shows it.
        from RevenueChaser.module_asymmetric_growth import freeze as ag_freeze
        ag_freeze.freeze_result(result, run_id=run)
    return ASYMMETRIC_GROWTH_LATEST


class UniverseBusy(JobMessage):
    """The universe lock is held elsewhere; this run made no network call and wrote nothing (#244).

    ``run=True``: another universe run holds it. Otherwise another universe writer (Phase B, "Fetch caps") does.
    ``board``: a single-board Fetch of that board is running (#356).
    """

    def __init__(self, pid: int | None, run: bool = True, board: str | None = None):
        self.pid = pid
        who = pid or "unknown"
        if board:
            msg = f"a {board} Fetch is in progress, pid {who}; this universe run did not start"
        elif run:
            msg = f"another universe run is in progress, pid {who}"
        else:
            msg = f"the universe data is being written by another job, pid {who}; this universe run did not start"
        super().__init__(msg)


# The single-board Fetches (``refused_during_universe``): the runner's name without ``run_``, and its label.
_BOARD_LABELS = {"asymmetric_growth": "Asymmetric Growth", "swing": "Swing", "fundamentals": "Fundamentals"}


def run_universe_refresh(uw=None, quotes_uw=None, *, uw_quotes: bool = False) -> Path:
    """One-stop data refresh for the three ticker-level scanners.

    Fetches the shared universe and every per-ticker input the three boards need
    (SEC EDGAR facts, yfinance financials, 2y OHLC, ticker info), then runs all
    three boards. After this, "Refresh Tab" on any of them is a pure offline
    model run (no API calls) — everything is cached.

    The pull writes Yahoo's rows of ``quote_snapshot`` itself (#294). With a UW client in ``quotes_uw``, or with
    ``uw_quotes`` (the run builds its own client under the lock and closes it after the step), UW's rows follow right
    after the pull (``quotes.ensure_quote_snapshot``, screener batches of 100), before Swing and Fundamentals; the
    outcome, or a failure, is a job note and never stops the run.

    One run at a time (#244): the whole run holds the ``universe`` lock (``universe_run_lock``), on the dashboard
    job and on the CLI (``app/universe_cli.py``) alike, and a second run, or a run while Phase B writes, raises
    ``UniverseBusy`` before any call or write. So does a run while a single-board Fetch runs (#356): the run takes
    its locks first and then checks the boards' markers (``board_fetch_holders``). The lock is re-entrant per
    thread, so the universe pull inside AG takes it again. A cancel or a passed deadline (#243)
    stops the run at the next board boundary, so no later board runs or freezes.
    """
    from RevenueChaser.core.universe_fetch import board_fetch_holders, universe_run_holder, universe_run_lock

    with universe_run_lock() as holder:
        if holder is not None:
            raise UniverseBusy(holder, run=universe_run_holder() is not None)
        boards = board_fetch_holders()
        if boards:
            board, pid = boards[0]
            raise UniverseBusy(pid, board=_BOARD_LABELS.get(board, board))
        job_control.checkpoint("Asymmetric Growth")
        run_asymmetric_growth(uw, refresh_universe=True)  # universe + hunter financials + EDGAR facts
        if quotes_uw is not None or uw_quotes:
            job_control.checkpoint("Quotes")
            _record_uw_quotes(quotes_uw)
        # Both boards run even when the first scores nothing; an empty board's reason ends the job (#145).
        empty = []
        for label, step in (("Swing", run_swing), ("Fundamentals", run_fundamentals)):
            job_control.checkpoint(label)
            try:
                step(uw)                          # swing OHLC + info + model, then the fundamentals model
            except JobMessage as exc:
                empty.append(f"{label}: {exc.message}")
    if empty:
        raise JobMessage(" ".join(empty))
    return FUNDAMENTALS_LATEST


def _record_uw_quotes(uw=None) -> None:
    """UW's ``quote_snapshot`` rows for the session (#294). The outcome is a job note; a failure never raises.

    With no client given, it builds one (:func:`_quotes_uw_client`) and closes it after the step: the caller holds
    the universe lock by then, so a run refused as busy never opens a UW connection pool.
    """
    from RevenueChaser.core import quotes
    owned = uw is None
    try:
        if owned:
            uw = _quotes_uw_client()
            if uw is None:
                return
        res = quotes.ensure_quote_snapshot(uw=uw, run_id=run_id())
    except Exception as exc:  # noqa: BLE001 - the boards after it must still run
        logger.exception("UW quote snapshot failed")
        ledger.add_note(f"Quote snapshot (UW) was not written: {type(exc).__name__}: {exc}")
        return
    finally:
        if owned and uw is not None:
            getattr(uw, "close", lambda: None)()
    ledger.add_note(quotes.summary_line(res))


def _quotes_uw_client():
    """A UW client for the quote rows, or None (with a note) when there is no key or it does not verify.

    The universe job stays ``requires_uw=False``: a missing or rejected UW key must never stop the Yahoo pull and
    the three boards, so the key is checked here, for the quote rows only.
    """
    client = UWClient()
    if not getattr(client, "key", None):
        ledger.add_note("UW quote rows were not written: no UW key.")
        getattr(client, "close", lambda: None)()
        return None
    ok, message = client.verify()
    if not ok:
        ledger.add_note(f"UW quote rows were not written: {message}")
        getattr(client, "close", lambda: None)()
        return None
    return client


def run_universe_job(uw=None) -> Path:
    """The dashboard's ``universe`` job: :func:`run_universe_refresh` plus UW's quote rows when a UW key verifies.

    The quote client is built inside the run, under the universe lock, and closed after the quote step; a client
    handed in as ``uw`` is used as it is and left to its owner (the job queue closes the one it built)."""
    return run_universe_refresh(uw, quotes_uw=uw, uw_quotes=True)


def refused_during_universe(runner):
    """A single-board Fetch job that is refused while a universe run is going (#244), and refuses one (#356).

    The boards write the same caches and the same ``forecast_ledger`` rows as the universe run (#128). The Fetch
    holds its board's marker (``board_fetch_lock``) while it runs, so a universe run that starts meanwhile is
    refused; it reads the ``universe_run`` marker and takes no universe lock. Phase B, which holds only the writer
    lock, does not refuse a board Fetch, as on main. A second Fetch of the same board is refused too. A Refresh
    (``no_fetch``) takes no marker and is never refused. The dashboard registers the wrapped runner;
    ``run_universe_refresh`` calls the plain one.
    """
    board = runner.__name__.removeprefix("run_")
    label = _BOARD_LABELS.get(board, board)

    @functools.wraps(runner)
    def fetch(uw, *args, **params):
        if params.get("no_fetch"):
            return runner(uw, *args, **params)
        from RevenueChaser.core.universe_fetch import board_fetch_lock
        with board_fetch_lock(board) as busy:
            if busy is not None:
                kind, pid = busy
                if kind == "universe":
                    raise JobMessage(f"A universe run is in progress, pid {pid}. This Fetch did not start: "
                                     "the universe run refreshes this board too.")
                raise JobMessage(f"Another {label} Fetch is in progress, pid {pid or 'unknown'}. "
                                 "This Fetch did not start.")
            return runner(uw, *args, **params)
    return fetch


def _end_empty_fetch(payload: dict, no_fetch: bool) -> None:
    """A Fetch that scored nothing says why (AGENTS.md §2) instead of ending "done" without a word (#145).

    Only a Fetch is a job. A Refresh is a GET with no job to carry a message; its board already shows the reason.
    The board is written before this raises, so the empty state is on disk either way.
    """
    if payload.get("no_data") and not no_fetch:
        raise JobMessage(str(payload.get("no_data_message") or "No data."))


def run_swing(uw, refresh_universe: bool = False, no_fetch: bool = False) -> Path:
    """Run the Swing Screener (broad US scan, Minervini/Weinstein/Qullamaggie).

    yfinance-only (``uw`` is unused; options flow is manual-review). Writes the
    structured JSON payload, the HTML board and its rows file (K10, #383 R3).
    ``refresh_universe=True`` re-fetches the shared ticker universe first;
    ``no_fetch=True`` (model-only) runs purely from cached per-ticker data.
    """
    from RevenueChaser.module_swing_potential.pipeline import run_scan, save_payload
    from RevenueChaser.module_swing_potential.report import render_swing_board

    rows: dict = {}                       # the score and gate rows a Fetch persisted, for the result history
    payload = run_scan(uw, refresh_universe=refresh_universe, no_fetch=no_fetch, rows_sink=rows)
    save_payload(payload)
    _write_board(SWING_LATEST, *render_swing_board(payload))
    if not no_fetch:                      # last step of a Fetch; a Refresh never freezes (see AG above)
        from RevenueChaser.module_swing_potential import freeze as swing_freeze, history as swing_history
        run = _freeze_run_id()            # one id for the history header and the freeze (jobs.py:97)
        # Result history (#278): after the latest tables, before the freeze. A failure is a job note, never a
        # failed Fetch: the latest tables and the freeze below do not depend on it.
        try:
            swing_history.record(payload, rows=rows, run_id=run)
        except Exception as exc:
            logger.exception("Swing result history failed")
            ledger.add_note(f"Swing result history was not recorded: {type(exc).__name__}: {exc}")
        swing_freeze.freeze_payload(payload, run_id=run)
    _end_empty_fetch(payload, no_fetch)
    return SWING_LATEST


def run_fundamentals(uw, refresh_universe: bool = False, no_fetch: bool = False) -> Path:
    """Run the Fundamental Analysis scan (Rule of 40 / Quality / Momentum).

    SEC EDGAR + yfinance only (``uw`` unused; options flow is manual-review).
    Writes the JSON payload, the HTML board and its rows file (K10, #383 R3).
    ``refresh_universe=True`` re-fetches the shared ticker universe first;
    ``no_fetch=True`` (model-only) runs purely from cached per-ticker data.
    """
    from RevenueChaser.module_fundamentals.pipeline import run_scan, save_payload
    from RevenueChaser.module_fundamentals.report import render_fundamental_board

    rows: dict = {}                       # the score and gate rows a Fetch persisted, for the result history
    payload = run_scan(uw, refresh_universe=refresh_universe, no_fetch=no_fetch, rows_sink=rows)
    save_payload(payload)
    _write_board(FUNDAMENTALS_LATEST, *render_fundamental_board(payload))
    if not no_fetch:                      # last step of a Fetch; a Refresh never freezes (see AG above)
        from RevenueChaser.module_fundamentals import freeze as fundamentals_freeze, history as fundamentals_history
        run = _freeze_run_id()            # one id for the history header and the freeze (jobs.py:97)
        # Result history (#278): after the latest tables, before the freeze. A failure is a job note, never a
        # failed Fetch: the latest tables and the freeze below do not depend on it.
        try:
            fundamentals_history.record(payload, rows=rows, run_id=run)
        except Exception as exc:
            logger.exception("Fundamentals result history failed")
            ledger.add_note(f"Fundamentals result history was not recorded: {type(exc).__name__}: {exc}")
        fundamentals_freeze.freeze_payload(payload, run_id=run)
    _end_empty_fetch(payload, no_fetch)
    return FUNDAMENTALS_LATEST


def run_comovement(uw=None, no_fetch: bool = False,
                   horizon: int | None = None,
                   benchmark: str | None = None,
                   window: int | None = None) -> Path:
    """Run the Co-movement board (return-correlation clusters).

    Fetch fills daily OHLC for the liquid universe then clusters; the
    ``no_fetch`` (model-only) path reads the cached store and re-renders with
    zero network. ``horizon`` (1/3/5/10) and ``benchmark`` (off/spy/dia/qqq)
    come from the board's Remove-market / Horizon controls and drive the
    residual clustering; ``window`` (sessions) is the board's window control,
    ``None`` meaning the Settings default. Writes the JSON payload + the
    self-contained HTML board.

    After a Fetch's board is written, the popular windows
    (``config.PRECOMPUTE_WINDOWS``) are computed and stored so switching to them
    serves a stored run (#95). A precompute failure is logged; it never fails
    the Fetch or touches the board just written.

    A Fetch's runs share its ``run_id``, and its last step records ONE
    header-only ``result_runs`` row listing every run it stored or served
    (#379 D2). A store-only run records nothing.
    """
    from RevenueChaser.module_comovement import engine, report

    run = None if no_fetch else _freeze_run_id()       # a store-only run stores nothing, so it has no id
    payload = engine.run(no_fetch=no_fetch, horizon=horizon, benchmark=benchmark,
                         window=window, **({} if no_fetch else {"run_id": run}))
    _write_output("comovement", json.dumps(payload, ensure_ascii=False, default=str),
                  "latest", ext="json")
    _write_output("comovement", report.render_comovement_dashboard(payload),
                  "latest", ext="html")
    if no_fetch:
        return COMOVEMENT_LATEST
    as_ofs: dict[int, str] = {}
    done: dict[int, str] = {}
    warnings: list[str] = []
    if payload.get("config_hash"):
        try:
            done = engine.precompute(horizon=horizon, benchmark=benchmark, run_id=run,
                                     as_ofs=as_ofs)
            logger.info("comovement precompute: %s", done)
        except Exception as exc:  # noqa: BLE001 - optional speed-up, never fatal
            logger.warning("comovement precompute failed: %s", exc, exc_info=True)
            warnings.append(f"precompute failed: {type(exc).__name__}: {exc}")
    # Result history (#379 D2): after the precompute, so the header names every window's run. A failure is a job
    # note, never a failed Fetch: the runs and the board above do not depend on it.
    try:
        from RevenueChaser.module_comovement import history as comovement_history
        comovement_history.record(payload, run_id=run, as_ofs=as_ofs, precompute=done, warnings=warnings)
    except Exception as exc:
        logger.exception("Co-movement result history failed")
        ledger.add_note(f"Co-movement result history was not recorded: {type(exc).__name__}: {exc}")
    return COMOVEMENT_LATEST


def run_comovement_recompute(uw=None) -> None:
    """Store-only recompute of Co-movement after a Settings save or reset (#40).

    ``requires_uw=False``, short lane, zero network: re-clusters the stored prices
    at the new effective settings (``run_comovement(no_fetch=True)``), then
    re-renders the Settings board so its card funnel follows. Always ends with a
    ``JobMessage`` so the UI reports the run.
    """
    run_comovement(None, no_fetch=True)
    try:
        payload = json.loads(COMOVEMENT_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    run_settings(uw)
    st = payload.get("settings") or {}
    message = (f"Co-movement recomputed: {payload.get('eligible', 0):,} clustered, "
               f"{payload.get('window') or st.get('window')}-session window")
    if st.get("custom"):
        message += " (custom settings)"
    raise JobMessage(message)


def run_value_chain(uw=None, no_fetch: bool = False) -> Path:
    """Run the Value Chain board (10-K/20-F supplier/customer edges).

    Fetch pulls the latest 10-K/20-F for the seed universe and slices Item 1/4;
    the ``no_fetch`` (model-only) path reads the cached corpus and re-renders with
    zero network.
    """
    from RevenueChaser.module_value_chain import engine, report

    # One id per Fetch (jobs.py:97): its result history header (#387). A Refresh records nothing.
    payload = engine.run(no_fetch=no_fetch, run_id=None if no_fetch else _freeze_run_id())
    _write_output("value_chain", json.dumps(payload, ensure_ascii=False, default=str),
                  "latest", ext="json")
    _write_output("value_chain", report.render_value_chain_dashboard(payload),
                  "latest", ext="html")
    fetch = payload.get("fetch") or {}
    if not no_fetch and not fetch.get("fetched"):
        # A Fetch that wrote nothing says why instead of looking like a silent no-op.
        message = f"No new filings fetched ({fetch.get('cached', 0)} already cached"
        for key, label in (("kept", "kept"), ("form_40f", "40-F"),
                           ("no_filing", "no 10-K/20-F"), ("known_miss", "recorded misses"),
                           ("no_cik", "no CIK"), ("failed", "failed")):
            if fetch.get(key):
                message += f", {fetch[key]} {label}"
        raise JobMessage(message + ")")
    return VALUE_CHAIN_LATEST


def run_daily_bars(uw=None, tickers: list[str] | None = None) -> None:
    """Write the latest complete session's daily bars (#156); RC-Daily-Bars runs the same function."""
    from RevenueChaser.core import prices

    res = prices.ensure_daily_bars(tickers, uw=uw, run_id=run_id())
    if res.get("locked"):
        raise JobMessage("Daily bars are already running; nothing started.")
    if not prices.run_ok(res):
        raise RuntimeError(prices.summary_line(res))
    raise JobMessage(prices.summary_line(res))


def run_index_lists(uw=None, first_fill: list[str] | None = None) -> None:
    """Write the index lists (#302, D24); RC-Index-Lists runs the same step weekly. ``first_fill=["sp600"]`` is the
    attended first SmallCap 600 fill, which needs the user's OK for that run."""
    from RevenueChaser.core import index_registry

    res = index_registry.refresh_lists(run_id=run_id(), first_fill=first_fill or ())
    if res.get("locked"):
        raise JobMessage("Index lists are already running; nothing started.")
    if not index_registry.lists_ok(res):
        raise RuntimeError(index_registry.summary_line(res))
    raise JobMessage(index_registry.summary_line(res))


def run_edgar_sync(uw=None, limit: int | None = None, force: bool = False) -> Path:
    """Sync the stored 10-K/20-F corpus against SEC ``submissions.recent``.

    SEC-only (``requires_uw=False``) and long-running: one throttled submissions
    request per universe CIK (#299; up to ~7.7k tickers at 10 req/s), stored in
    edgar.duckdb, with archive downloads for classification tickers only when the
    stored accession differs. Then re-renders the Value
    Chain board from the store with ``no_fetch=True`` — zero network — since the
    sync may have just extended the corpus.

    Value Chain / Relatedness *Fetch* stay cache-first (they only fill tickers
    with no row); detecting a newer filing is this job's job. ``sync_filings``
    already holds ``job_lock('edgar_sync')`` and the SEC throttle, so no second
    lock is added here. A run that changed nothing still raises ``JobMessage``
    with its counts, so the UI says so instead of looking like a silent no-op.

    The same run is the daily SEC submissions sweep (#299, D15): one request per
    CIK of the whole universe, each answer stored in ``sec_submissions`` /
    ``sec_submissions_cik`` with this job's ``run_id``; the message ends with
    that sweep's counts and names the CIKs that failed.
    """
    from RevenueChaser.module_value_chain import sync

    res = sync.sync_filings(limit=limit, force=bool(force), run_id=run_id())
    if res.get("locked"):
        raise JobMessage("Another EDGAR crawl is already running — try again shortly.")

    path = run_value_chain(uw, no_fetch=True)

    updated = res.get("updated", 0)
    current = res.get("current", 0)
    no_filing = res.get("no_filing", 0)
    failed = res.get("failed", 0)
    kept = res.get("kept", 0)
    form_40f = res.get("form_40f", 0)
    known_miss = res.get("known_miss", 0)
    no_cik = res.get("no_cik", 0)
    requested = res.get("requested", updated + current + no_filing + failed)
    # ``requested`` counts no-CIK tickers (no request made) and known misses (skipped).
    checked = requested - no_cik - known_miss
    message = (f"{updated} filing updated, {current} already current, "
               f"{no_filing} with no 10-K/20-F ({checked} checked)")
    if kept:
        message += f"; {kept} kept (stored slice better than the newest filing)"
    if form_40f:
        message += f"; {form_40f} 40-F (not supported yet)"
    if known_miss:
        message += f"; {known_miss} recorded misses skipped"
    if no_cik:
        message += f"; {no_cik} with no CIK"
    if failed:
        message += f"; {failed} failed"
    message += _submissions_message(res.get("submissions"))
    raise JobMessage(message)


def _submissions_message(subs: dict | None) -> str:
    """The SEC submissions part of the edgar-sync message (#299); empty when the sync made no sweep."""
    if not subs:
        return ""
    text = (f". SEC submissions: {subs.get('ciks', 0)} of {subs.get('requested', 0)} CIKs answered, "
            f"{subs.get('filings_appended', 0)} new filings, {subs.get('headers_appended', 0)} header changes")
    if subs.get("headers_absent"):
        text += f", {subs['headers_absent']} no longer served"
    if subs.get("failed"):
        names = subs.get("failed_ciks") or []
        shown = ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
        text += f"; {subs['failed']} CIKs failed ({shown})"
    if subs.get("pending"):
        text += f"; {subs['pending']} CIKs pending (SEC unavailable)"
    if subs.get("error"):
        text += f"; not stored: {subs['error']}"
    return text


def run_relatedness(uw=None, no_fetch: bool = False) -> Path:
    """Run the Relatedness board (LLM business-profile similarity).

    Fetch builds profiles for the pilot universe via DeepSeek (off-peak gated);
    the ``no_fetch`` (model-only) path re-renders from the cached profiles with
    zero network.
    """
    from RevenueChaser.module_relatedness import engine, report

    try:
        # One id per Fetch (jobs.py:97): its result history header (#388). A Refresh records nothing.
        payload = engine.run(no_fetch=no_fetch, run_id=None if no_fetch else _freeze_run_id())
    except engine.AlreadyRunning as exc:
        raise JobMessage(str(exc)) from exc
    _write_output("relatedness", json.dumps(payload, ensure_ascii=False, default=str),
                  "latest", ext="json")
    _write_output("relatedness", report.render_relatedness_dashboard(payload),
                  "latest", ext="html")
    # A Fetch that stopped early (peak, missing key, auth, the worker-pool stop)
    # or left work pending is not a silent success: say what is done and what waits.
    # A Refresh is a GET that writes nothing and must never raise (C-F6), so the
    # job message is Fetch-only.
    pending = payload.get("pending") or 0
    if not no_fetch and (payload.get("stopped_reason") or pending > 0):
        parts = [f"{payload.get('n') or 0} profiled, {pending} pending"]
        if payload.get("stopped_reason"):
            parts.append(f"stopped: {payload['stopped_reason']}")
        if payload.get("next_off_peak"):
            parts.append(f"next off-peak {payload['next_off_peak']} UTC")
        raise JobMessage("; ".join(parts))
    return RELATEDNESS_LATEST


def run_oipd(uw: UWClient, ticker: str | None = None, no_fetch: bool = False) -> Path:
    """Run the Implied Probability (OIPD) board for one ticker.

    Fetch pulls the Unusual Whales option chain, fits an SVI smile per expiry and
    persists the chain to ``oipd_chain`` / ``oipd_meta``; the ``no_fetch``
    (Refresh) path re-fits from the store with zero network. Writes the JSON
    payload + the self-contained HTML board.

    A run that fits no expiry still writes the board (with its error/warning
    state) and raises ``JobMessage`` so the UI explains itself rather than
    looking like a silent success.
    """
    from RevenueChaser.module_oipd import engine, history as oipd_history

    capture: dict | None = None if no_fetch else {}      # what the fit stood on, for the result history (#382)
    run, run_at = (None, None) if no_fetch else (_freeze_run_id(), time.time())
    payload = engine.run(uw=uw, ticker=ticker, no_fetch=no_fetch, capture=capture)
    if not no_fetch:
        payload["provider_health"] = _uw_health(uw)
    path = _render_oipd(payload)
    if not no_fetch:
        # Result history (#382): after the board file, so it holds what the board showed. A failure is a job note,
        # never a failed Fetch: the board file does not depend on it.
        try:
            oipd_history.record(payload, capture, run_id=run, run_at=run_at, uw_health=payload["provider_health"])
        except Exception as exc:
            logger.exception("OIPD result history failed")
            ledger.add_note(f"OIPD result history was not recorded: {type(exc).__name__}: {exc}")
    if not payload.get("expiries"):
        msg = payload.get("error")
        if not msg:
            warns = [w for w in (payload.get("warnings") or []) if w]
            msg = (" ".join(warns) if warns else
                   f"No expiry could be fitted for {payload.get('ticker')}.")
        raise JobMessage(msg)
    return path


def run_oipd_cached(uw=None, ticker: str | None = None) -> Path:
    """Store-only Implied Probability render — no network, no credentials.

    Runs in the ``/view`` path, not the job queue, so it never raises: an empty
    store still writes an error board and returns it.
    """
    from RevenueChaser.module_oipd import engine
    return _render_oipd(engine.run(uw=None, ticker=ticker, no_fetch=True))


def _render_oipd(payload: dict) -> Path:
    from RevenueChaser.module_oipd import report

    _write_output("oipd", json.dumps(payload, ensure_ascii=False, default=str),
                  "latest", ext="json")
    _write_output("oipd", report.render_oipd_dashboard(payload), "latest", ext="html")
    return OIPD_LATEST


def run_universe_stats(uw=None, limit: int | None = None) -> Path:
    """Fill only the NULL ``equity_universe.market_cap`` values from Yahoo.

    A standalone Phase B (``requires_uw=False``): unlike ``/run/universe`` it
    never calls ``yf.screen`` and never runs the three scanners — it resolves the
    null-cap rows (optionally capped to the first ``limit``) and then re-renders
    the Settings board from the store with ``run_settings`` (zero network).

    Always raises ``JobMessage`` with the counts so the UI reports the run even
    when it filled nothing; a rate-limited early stop is called out so a re-run
    is the obvious next step.
    """
    from RevenueChaser.core import universe_fetch

    res = universe_fetch.backfill_universe_caps(refreshed_by="Settings", limit=limit)
    if res.get("locked") is not None:  # 0 = lock held, owner unknown
        owner = f" (pid {res['locked']})" if res["locked"] else ""
        raise JobMessage(f"Another run{owner} holds the universe lock; "
                         "locked, nothing written. Try again shortly.")
    run_settings(uw)

    message = (f"{res.get('filled', 0)} caps filled, {res.get('skipped', 0)} skipped, "
               f"{res.get('failed', 0)} failed, {res.get('deferred', 0)} still null "
               f"({res.get('requested', 0)} checked)")
    if res.get("rate_limited"):
        message += " — Yahoo rate-limited, stopped early; run again to resume"
    raise JobMessage(message)


def run_identity(uw=None, limit: int | None = None) -> None:
    """Refresh the identity master (``security_master``): the deltas from every source, then the rules.

    Store-only for the boards (``requires_uw=False``): it fetches from the identity providers and writes
    the identity tables (plus new SEC ticker-map rows). After a successful build it also applies the master's
    sector and industry (and UW's next earnings date) to ``equity_universe`` (issues #86, #265): those columns
    only, never another column, and never a ticker the master does not know.
    ``refresh`` never raises; this runner always ends with ``JobMessage`` so the UI shows what happened.
    """
    from RevenueChaser.core.identity import refresh as identity_refresh

    raise JobMessage(identity_refresh.summary(identity_refresh.refresh(limit=limit)))


def _discord_busy_message(r: dict) -> str:
    """The sentence a Discord job ends with when ``engine`` reports another Discord job holds the lock (#26)."""
    pid = r.get("holder_pid") or 0
    who = f" (pid {pid})" if pid else ""
    return (f"Discord: another Discord job{who} is running; nothing was written. "
            "Try again when it finishes.")


def run_discord_discover(uw=None) -> None:
    """List the Discord guild's channels via the DCE CLI (new ones start disabled). Needs RC_DCE_CLI, DISCORD_TOKEN and RC_DISCORD_GUILD_ID; network."""
    from RevenueChaser.module_discord import engine

    r = engine.discover()
    if r.get("busy"):                         # another Discord job holds the lock: not an error, nothing written
        raise JobMessage(_discord_busy_message(r))
    raise JobMessage(f"Discord: {r['channels']} channels found, {r['new']} new (disabled until you enable them).")


DISCORD_MIN_INTERVAL_S = 15 * 60      # the epic's Fetch rule: one export per channel per 15 minutes


def run_discord(uw=None, media: bool = False, channel_id: str | None = None, force: bool = False) -> None:
    """Export enabled Discord channels (or just ``channel_id``) incrementally into ``.store/discord.duckdb`` (#72).

    A channel exported (successfully or not) less than 15 minutes ago is skipped unless ``force``;
    if every target is that recent the job ends with a ``JobMessage`` and runs no export.
    """
    import time

    from RevenueChaser.module_discord import engine, store

    ids = [channel_id] if channel_id else None
    skipped: list[str] = []
    if not force:
        targets = [channel_id] if channel_id else [c["channel_id"] for c in store.channels(enabled_only=True)]
        last = store.last_export_started(targets)
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - DISCORD_MIN_INTERVAL_S))
        ready = [c for c in targets if last.get(c, "") < cutoff]
        if targets and not ready:
            who = "this channel was" if channel_id else "every followed channel was"
            raise JobMessage(f"Discord: {who} exported less than 15 minutes ago "
                             f"(last start {max(last.values())}). Wait, then Update again.")
        if channel_id is None and ready and len(ready) < len(targets):
            ids = ready
            names = {c["channel_id"]: c.get("name") or c["channel_id"] for c in store.channels()}
            skipped = [names.get(c, c) for c in targets if c not in ready]
    r = engine.update(media=media, channel_ids=ids)
    if r.get("busy"):                         # another Discord job holds the lock: not an error, nothing written
        raise JobMessage(_discord_busy_message(r))
    if not r["channels"] and not r["failed"]:
        raise JobMessage("Discord: no enabled channels; run Discover and enable some first.")
    text = f"Discord: {r['channels']} channels exported, {r['messages']} messages, {r['new']} new."
    if skipped:
        text += f" Skipped (exported less than 15 minutes ago): {', '.join(skipped)}."
    if r["failed"]:
        text += f" Failed: {', '.join(r['failed'])} (see the export runs for the reason)."
    raise JobMessage(text)


def run_settings(uw=None) -> Path:
    """Render the Settings / Data health board from the store — no network.

    Store-only (``requires_uw=False``): the inventory reads ``raw.duckdb`` /
    ``edgar.duckdb`` and needs no credentials. Runs in the ``/view`` path
    (Refresh or first load), not the job queue, so it never raises; an empty
    store still writes the artifact with its explicit empty state.
    """
    from RevenueChaser.module_settings import inventory, report

    payload = inventory.collect()
    return _write_output(
        "settings", report.render_settings_dashboard(payload), "latest")


def run_uw_interaction(uw: UWClient, **params) -> Path:
    """Run one UW Interaction (lab) tool and persist its payload under .cache/.

    ``params`` carries the catalog tool key plus its validated query params.
    The board (``/view/module_uw_interaction``) is rendered from the stored
    payload, so this runner only writes the regenerable JSON — never ``.store``.

    A tool that yields nothing must say so: the payload is still written with an
    explicit empty/error state and a ``JobMessage`` explains the run so the UI
    does not look like a silent success.
    """
    from RevenueChaser.module_uw_interaction import engine as uw_engine
    from RevenueChaser.module_uw_interaction.config import save_state, utc_now

    tool = str(params.get("tool") or "").strip()
    try:
        result = uw_engine.run_tool(uw, tool, {k: v for k, v in params.items() if k != "tool"})
    except uw_engine.ToolError as exc:
        raise JobMessage(str(exc))

    payload = {
        "tool_key": result.tool_key,
        "label": result.label,
        "family": result.family,
        "path": result.path,
        "help": result.help,
        "params_used": result.params_used,
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "error": result.error,
        "http_status": result.http_status,
        "generated_at": utc_now(),
        "provider_health": _uw_health(uw),
    }
    save_state(payload)

    if result.http_status in (401, 403):
        raise JobMessage(
            f"UW rejected the credentials for '{result.label}' (HTTP {result.http_status}). "
            "Check the Unusual Whales key in .env."
        )
    if result.error:
        raise JobMessage(f"'{result.label}' returned nothing to show: {result.error}")
    if not result.rows and _provider_gap_text(uw):
        # UW-1: a timeout or 5xx is not "no rows for those parameters".
        raise JobMessage(f"'{result.label}' request failed: {_provider_gap_text(uw)}")
    if not result.rows:
        raise JobMessage(f"'{result.label}' returned no rows for those parameters.")
    from RevenueChaser.module_uw_interaction.config import UW_STATE_JSON as _st
    return _st

