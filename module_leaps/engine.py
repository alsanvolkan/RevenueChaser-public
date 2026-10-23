"""LEAPS / institutional accumulation — far-dated options OI + IV footprint.

Thesis ("ne degerse, iz birakir"): big money builds positions via LEAPS
(far-dated options), leaving a footprint of rising far-dated call OI, rising
IV, and aggressive far-dated call sweeps.

Direction guard: raw OI growth alone is ambiguous (could be short hedges), so
"Accumulating" requires far-dated CALL dominance, and tickers with a near-term
binary event (earnings < N days) are excluded so event-hedging does not
masquerade as accumulation.

This is a CONTEXT layer only — it never feeds direction/confidence/composite
scores. Captured in the forward-test for later validation.
"""
import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from RevenueChaser.core import calendar, rawstore

logger = logging.getLogger(__name__)

FAR_DTE = 270          # expiries at least this far out count as LEAPS
NEAR_EVENT_DAYS = 7    # exclude tickers with an earnings event within this window
UNWATCHED_CAP = 80     # the market scan lists at most this many unwatched names (the board states it, #377 L2)
CALL_SHARE_MIN = 0.60  # far-dated call share required for "Accumulating"
ASK_SHARE_MIN = 0.50   # far-dated print ask share required for "Accumulating" (O10, #377 L3)

# ── Flow-print discovery (far-dated large prints via /api/option-trades) ──
MIN_DTE = 183                    # discovery threshold: far-dated options
MIN_PREMIUM = 50_000             # large-print floor ($)
ITM_DELTA = (0.55, 0.85)         # stock-replacement zone (slightly→deep ITM)
OTM_DELTA = (0.15, 0.40)         # aggressive OTM upside zone
ITM_CONC_MIN = 0.50              # ITM call premium share for +0.2 multiplier
OTM_CONC_MIN = 0.30              # OTM call premium share for +0.1 multiplier
INDEX_TICKERS = {"SPX", "NDX", "VIX", "RUT", "XSP", "XND", "DJX", "OEX", "NDXP"}

LABELS = ("Accumulating", "Distributing", "Quiet", "N/A")


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def _to_int_or_none(v) -> Optional[int]:
    """``_to_int`` for a print field: a missing or unreadable value stays None (DF95), so it is not stored as 0."""
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _dte(expiry: str, today: date) -> int:
    try:
        e = date.fromisoformat(str(expiry)[:10])
        return (e - today).days
    except (ValueError, TypeError):
        return 0


def far_oi_totals(oi_per_expiry: list[dict], today: date = None) -> dict:
    """Sum call/put OI over far-dated (LEAPS) expiries."""
    d = today or date.today()
    far_call, far_put = 0, 0
    for row in oi_per_expiry:
        if _dte(row.get("expiry", ""), d) >= FAR_DTE:
            far_call += _to_int(row.get("call_oi"))
            far_put += _to_int(row.get("put_oi"))
    return {"far_call_oi": far_call, "far_put_oi": far_put}


def _call_share(far_call_oi: int, far_put_oi: int) -> Optional[float]:
    total = far_call_oi + far_put_oi
    if total <= 0:
        return None
    return round(far_call_oi / total, 4)


def _growth_pct(current: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if current is None or baseline in (None, 0):
        return None
    return round((current - baseline) / abs(baseline) * 100.0, 2)


def matched_far_growth(cur_oi: list[dict], base_oi: list[dict], base_as_of) -> Optional[float]:
    """Far-call OI growth over the expiries present in BOTH snapshots (O9, #377 L3).

    Only the expiries on both sides are compared, so a new listing and an expiry rolling off are not growth; and
    "far" is decided at the BASELINE's date, so an expiry crossing the ``FAR_DTE`` line while its OI is unchanged
    is 0.0% growth, not a jump. ``None`` when no expiry is on both sides or the matched baseline is 0.
    """
    try:
        base_day = date.fromisoformat(str(base_as_of)[:10])
    except (TypeError, ValueError):
        return None
    base = {str(r.get("expiry") or "")[:10]: _to_int(r.get("call_oi")) for r in (base_oi or [])}
    cur = {str(r.get("expiry") or "")[:10]: _to_int(r.get("call_oi")) for r in (cur_oi or [])}
    shared = [e for e in base if e in cur and _dte(e, base_day) >= FAR_DTE]
    if not shared:
        return None
    start = sum(base[e] for e in shared)
    if start <= 0:
        return None
    return round((sum(cur[e] for e in shared) - start) / start * 100.0, 2)


def _iv_rank_latest(iv_rank_rows: list[dict]) -> Optional[float]:
    if not iv_rank_rows:
        return None
    for row in reversed(iv_rank_rows):
        v = _to_float(row.get("iv_rank_1y"))
        if v is not None:
            return v
    return None


def _iv_rank_slope(iv_rank_rows: list[dict], lookback: int = 5) -> Optional[float]:
    """Slope of iv_rank_1y over the last N entries (rough per-day)."""
    vals = []
    for row in iv_rank_rows:
        v = _to_float(row.get("iv_rank_1y"))
        if v is not None:
            vals.append(v)
    if len(vals) < 2:
        return None
    tail = vals[-lookback:]
    if len(tail) < 2:
        return None
    return round((tail[-1] - tail[0]) / max(len(tail) - 1, 1), 4)


def _far_call_sweep_pct(flow_per_expiry: list[dict], today: date = None) -> Optional[float]:
    """Ask-side call premium share within far-dated expiries (aggressive buying)."""
    d = today or date.today()
    ask_call, ask_total = 0.0, 0.0
    for row in flow_per_expiry:
        if _dte(row.get("expiry", ""), d) < FAR_DTE:
            continue
        ask_call += _to_float(row.get("call_premium_ask_side")) or 0.0
        ask_total += (_to_float(row.get("call_premium_ask_side")) or 0.0) + \
                     (_to_float(row.get("put_premium_ask_side")) or 0.0)
    if ask_total <= 0:
        return None
    return round(ask_call / ask_total, 4)


def build_snapshot(ticker: str, oi_rows: list[dict], flow_rows: list[dict],
                   iv_rank_rows: list[dict],
                   near_term_event: bool = False,
                   today: Optional[date] = None) -> dict:
    """Build a single-day LEAPS snapshot dict.

    ``today`` overrides the reference date used for the far-dated (DTE) cut and
    the ``as_of`` stamp, so callers and tests can pin it. Defaults to today.
    """
    d = today or date.today()
    totals = far_oi_totals(oi_rows, d)
    far_call, far_put = totals["far_call_oi"], totals["far_put_oi"]
    if far_call == 0 and far_put == 0:
        return {
            "ticker": ticker, "label": "N/A", "far_call_oi": 0, "far_put_oi": 0,
            "far_call_share": None, "far_oi_growth_1d_pct": None,
            "far_oi_growth_q_pct": None, "far_call_sweep_pct": None,
            "iv_rank": None, "iv_rank_slope": None,
            "near_term_event": near_term_event, "as_of": d.isoformat(),
        }
    return {
        "ticker": ticker,
        "label": "",  # set by classifier after growth is computed
        "far_call_oi": far_call, "far_put_oi": far_put,
        "far_call_share": _call_share(far_call, far_put),
        "far_call_sweep_pct": _far_call_sweep_pct(flow_rows, d),
        "iv_rank": _iv_rank_latest(iv_rank_rows),
        "iv_rank_slope": _iv_rank_slope(iv_rank_rows),
        "near_term_event": near_term_event,
        "as_of": d.isoformat(),
    }


def _baseline_growth(cur_oi: Optional[list[dict]], far_call_oi, baseline: Optional[dict]) -> Optional[float]:
    """Growth against one baseline: matched-expiry when both sides carry OI, else its ``far_call_oi``."""
    if not baseline:
        return None
    if cur_oi is not None and baseline.get("oi_per_expiry"):
        return matched_far_growth(cur_oi, baseline["oi_per_expiry"], baseline.get("as_of"))
    return _growth_pct(far_call_oi, baseline.get("far_call_oi"))


def classify(snapshot: dict, baseline_1d: Optional[dict], baseline_q: Optional[dict],
             cur_oi: Optional[list[dict]] = None, ask_share: Optional[float] = None) -> dict:
    """Attach growth + label + score to a snapshot using two baselines.

    ``cur_oi`` is the snapshot's OI per expiry; with it growth is matched-expiry (``matched_far_growth``, O9,
    #377 L3), so a new listing or an expiry crossing the far line is not growth. A baseline without stored OI (a
    test double, or a row recorded before L3) is compared on its ``far_call_oi``.

    ``ask_share`` is the footprint's far-dated print ask share: an all-bid flow is not "Accumulating"
    (``ASK_SHARE_MIN``, O10). ``None`` (no prints) is not gated.
    """
    fc = snapshot.get("far_call_oi")
    g1 = _baseline_growth(cur_oi, fc, baseline_1d)
    gq = _baseline_growth(cur_oi, fc, baseline_q)
    snapshot["far_oi_growth_1d_pct"] = g1
    snapshot["far_oi_growth_q_pct"] = gq

    share = snapshot.get("far_call_share")
    gated = ask_share is not None and ask_share < ASK_SHARE_MIN
    growing = (g1 is not None and g1 > 0) or (gq is not None and gq > 0)
    shrinking = (g1 is not None and g1 < 0) or (gq is not None and gq < 0)

    if snapshot.get("near_term_event"):
        label = "Quiet"  # exclude event hedging from accumulation
    elif growing and share is not None and share >= CALL_SHARE_MIN and not gated:
        label = "Accumulating"
    elif shrinking and share is not None and share < CALL_SHARE_MIN:
        label = "Distributing"
    else:
        label = "Quiet"

    snapshot["label"] = label
    snapshot["score"] = _score(snapshot)
    return snapshot


def _score(s: dict) -> int:
    """0-100 placeholder weights (tuned only after forward-test validation)."""
    score = 0
    # far call OI growth (z-ish, capped)
    g1 = s.get("far_oi_growth_1d_pct") or 0.0
    gq = s.get("far_oi_growth_q_pct") or 0.0
    g = max(g1, gq)
    score += max(0, min(40, 10 + g * 0.8))  # +10 floor when any growth
    # far call share
    share = s.get("far_call_share")
    if share is not None:
        score += max(0, min(20, (share - 0.5) * 40))  # 0.5->0, 1.0->20
    # sweep premium
    sweep = s.get("far_call_sweep_pct")
    if sweep is not None:
        score += max(0, min(15, (sweep - 0.5) * 30))
    # iv rank slope
    slope = s.get("iv_rank_slope")
    if slope is not None and slope > 0:
        score += min(10, slope * 5)
    return int(round(min(100, score)))


# ── cache (.store/raw.duckdb -> leaps_cache) ──
def load_cache(ticker: str) -> Optional[dict]:
    row = rawstore.get("leaps_cache", ticker.upper())
    if not row:
        return None
    return row.get("payload")


def save_cache(ticker: str, snapshot: dict):
    """Store the ticker's latest snapshot; the older ``history`` list is carried unchanged (D1, #377 L3).

    The classifier reads its baselines from ``leaps_footprint_history`` now, so the list is frozen: never appended
    and never deleted (a drop would delete data). One writer only, the LEAPS Fetch (C26, #383).
    """
    data = load_cache(ticker) or {}
    history = data.get("history") or []
    import time
    rawstore.write_rows("leaps_cache", [{
        "ticker": ticker.upper(),
        "payload": {"latest": snapshot, "history": history},
        "fetched_at": time.time(),
    }])


def baseline_prior_day(history: list[dict]) -> Optional[dict]:
    """Most recent snapshot of a frozen ``leaps_cache.history`` list (no longer the classifier's source)."""
    if not history:
        return None
    return history[-1]


def baseline_prior_quarter(history: list[dict], quarter_end: str) -> Optional[dict]:
    """Latest snapshot of a frozen ``leaps_cache.history`` list with as_of <= quarter_end (no longer read)."""
    best = None
    for s in history:
        a = s.get("as_of", "")
        if a and a <= quarter_end:
            best = s
    return best


def _baselines_for(ticker: str, d: date) -> tuple[Optional[dict], Optional[dict]]:
    """The recorded baselines of one ticker (a scan passes a batch; a single call reads its own).

    A history read never raises into the Fetch: a locked or unreadable file gives no baselines, so growth is None.
    """
    from RevenueChaser.module_leaps import history as leaps_history
    try:
        base = leaps_history.baselines([ticker], d).get(ticker.upper()) or {}
    except Exception as e:  # noqa: BLE001 - no baseline, never a failed Fetch
        logger.warning("leaps %s: recorded baselines not read: %s", ticker, e)
        return None, None
    return base.get("1d"), base.get("q")


def fetch_and_classify(ticker: str, uw, today: Optional[date] = None, inputs: Optional[dict] = None,
                       near_term_event: bool = False, ask_share: Optional[float] = None,
                       baselines: Optional[dict] = None) -> dict:
    """Always fetch fresh data, classify vs history, and cache. (Scan path.)

    ``today`` is threaded down to the snapshot's DTE cut and quarter baseline. ``near_term_event`` is the scan's
    near-event flag for the ticker (a far-dated print whose ticker reports earnings within ``NEAR_EVENT_DAYS``), so
    the classifier's event exclusion sees it (#377 L2). ``ask_share`` is that ticker's far-dated print ask share,
    which gates "Accumulating" (O10, #377 L3). ``baselines`` is this ticker's recorded baselines
    (``module_leaps.history.baselines``' ``{"1d", "q"}``); ``None`` reads them here, a given mapping (even an empty
    one) is used as it is, so a scan reads the whole watchlist once and a failed read is not retried per ticker.
    A caller that passes ``inputs`` (the result history, #377) gets what the snapshot was built and classified
    from: the three UW answers and the two baselines, plus ``read_status`` (each read's ``ok``/``failed``, from the
    ``*_result`` calls) and ``error`` when a read raised. A 4xx such as 404 is a failed read that ``health()`` does
    not count, so the status is the only sign that the empty answer is not real.

    On that path (``inputs`` given, the LEAPS Fetch) a failed or raised read is not a footprint (LP-A, #377): the
    snapshot is not classified or saved, so ``leaps_cache`` keeps its real entries and baselines, and the call returns
    an ``N/A`` snapshot with ``fetch_status`` 'provider_failed'. An ``ok`` read with no data is real and is saved.
    ``inputs`` None is the plain path (no caller on the dashboard since C26).
    """
    d = today or date.today()
    try:
        if inputs is None:
            oi = uw.oi_per_expiry(ticker) or []
            flow = uw.flow_per_expiry(ticker) or []
            iv = uw.iv_rank(ticker) or []
        else:
            reads = {"oi_per_expiry": uw.oi_per_expiry_result(ticker),
                     "flow_per_expiry": uw.flow_per_expiry_result(ticker), "iv_rank": uw.iv_rank_result(ticker)}
            inputs["read_status"] = {k: r.status for k, r in reads.items()}
            oi, flow, iv = (reads[k].data or [] for k in ("oi_per_expiry", "flow_per_expiry", "iv_rank"))
    except Exception as e:
        oi, flow, iv = [], [], []
        if inputs is not None:
            inputs["error"] = f"{type(e).__name__}: {e}"

    if baselines is None:
        base_1d, base_q = _baselines_for(ticker, d)
    else:
        base_1d, base_q = (baselines or {}).get("1d"), (baselines or {}).get("q")
    if inputs is not None:
        inputs.update(oi_per_expiry=oi, flow_per_expiry=flow, iv_rank=iv, baseline_1d=base_1d, baseline_q=base_q)
        if inputs.get("error") or "failed" in inputs.get("read_status", {}).values():
            return {**build_snapshot(ticker, [], [], [], near_term_event=near_term_event, today=d),
                    "fetch_status": "provider_failed"}
    snap = build_snapshot(ticker, oi, flow, iv, near_term_event=near_term_event, today=d)
    snap = classify(snap, base_1d, base_q, cur_oi=oi, ask_share=ask_share)
    save_cache(ticker, snap)
    return snap


def _prev_quarter_end(today: Optional[date] = None) -> str:
    """Most recent quarter-end (baseline for quarter-over-quarter growth)."""
    today = today or date.today()
    latest = date(today.year - 1, 12, 31)
    for m, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
        qe = date(today.year, m, day)
        if qe < today:
            latest = qe
    return latest.isoformat()


# ── DuckDB persistence (flow-print dedup + daily scores) ──────────────────
# .store/raw.duckdb: leaps_flow_prints (dedup on id) and leaps_cache (per-ticker rolling 60-day history).
# leaps_daily_scores is no longer persisted (retired: only the legacy import ever filled it).

PRINTS_SOURCE = "uw"   # /option-trades is the only source of leaps_flow_prints (D09)


def _print_session(run_at: float) -> Optional[date]:
    """The session a print run belongs to (rule B, ``calendar.session_for``), else None.

    ``/option-trades`` serves only the latest trading day, so a print not stored is lost: past
    the end of the holiday table the print is kept without a session date (logged), never guessed.
    """
    try:
        return calendar.session_for(run_at).session_date
    except ValueError as exc:
        logger.warning("leaps_flow_prints: no session date for run_at %s (%s); stored without one",
                       run_at, exc)
        return None


def _store_prints_rows(prints: list[dict], today: Optional[date] = None, *,
                       run_at: Optional[float] = None,
                       fetched_at: Optional[float] = None) -> int:
    """Insert prints whose id is not stored yet; a stored id is never rewritten.

    Each new row is stamped (#295, D09): ``source``, ``fetched_at`` (when its page came back),
    ``run_at`` (the scan's start, epoch UTC) and ``session_date`` (rule B on ``run_at``). Missing
    stamps come from the clock seam (``calendar._now``); ``fetched_at`` defaults to ``run_at``.
    """
    today = (today or date.today()).isoformat()
    if run_at is None:
        run_at = calendar._now().timestamp()
    if fetched_at is None:
        fetched_at = run_at
    stamps = {"source": PRINTS_SOURCE, "fetched_at": float(fetched_at),
              "session_date": None, "run_at": float(run_at)}
    rows = []
    for p in prints:
        if not p.get("id"):
            continue
        rows.append({
            "id": p["id"], "ticker": p["ticker"], "strike": p["strike"],
            "expiry": p["expiry"], "premium": p["premium"], "size": p["size"],
            "option_type": p["option_type"], "delta": p["delta"],
            "next_earnings_date": p["next_earnings_date"],
            "executed_at": p["executed_at"], "discovered_at": today,
            "ask_vol": p["ask_vol"], "bid_vol": p["bid_vol"], **stamps,
        })
    if not rows:
        return 0
    # dedup within the batch (SQLite's INSERT OR IGNORE counted only inserted rows)
    seen = {}
    for r in rows:
        seen.setdefault(r["id"], r)
    rows = list(seen.values())
    ids = [r["id"] for r in rows]
    ph = ", ".join(["?"] * len(ids))
    existing = {r["id"] for r in rawstore.query(f"SELECT id FROM leaps_flow_prints WHERE id IN ({ph})", ids)}
    new_rows = [r for r in rows if r["id"] not in existing]
    if new_rows:
        session = _print_session(run_at)
        for r in new_rows:
            r["session_date"] = session
        rawstore.write_rows("leaps_flow_prints", new_rows)
    return len(new_rows)


def _is_stock(row: dict) -> bool:
    tags = row.get("tags") or []
    if "index" in tags:
        return False
    sym = (row.get("underlying_symbol") or "").upper()
    return sym and sym not in INDEX_TICKERS


def _normalize_print(row: dict) -> Optional[dict]:
    sym = (row.get("underlying_symbol") or "").upper()
    if not sym:
        return None
    ned = row.get("next_earnings_date")
    return {
        "id": row.get("id"),
        "ticker": sym,
        "strike": _to_float(row.get("strike")),
        "expiry": str(row.get("expiry") or "")[:10],
        "premium": _to_float(row.get("premium")) or 0.0,
        "size": _to_int_or_none(row.get("size")),
        "option_type": row.get("option_type"),
        "delta": _to_float(row.get("delta")),
        "next_earnings_date": str(ned)[:10] if ned else None,
        "executed_at": row.get("executed_at"),
        "ask_vol": _to_int_or_none(row.get("ask_vol")),
        "bid_vol": _to_int_or_none(row.get("bid_vol")),
    }


def store_prints(prints: list[dict], today: Optional[date] = None, *,
                 run_at: Optional[float] = None, fetched_at: Optional[float] = None) -> int:
    """Insert far-dated prints with id dedup; returns the number of new rows.

    ``today`` stamps ``discovered_at`` (injectable clock, O12); ``run_at`` / ``fetched_at``
    (epoch UTC) stamp the run and the fetch (#295), see ``_store_prints_rows``.
    """
    return _store_prints_rows(prints, today, run_at=run_at, fetched_at=fetched_at)


def discover_flow_prints(uw, min_dte: int = MIN_DTE, min_premium: float = MIN_PREMIUM,
                         limit: int = 250, max_pages: int = 5, today: Optional[date] = None,
                         warnings: Optional[list] = None,
                         now: Optional[datetime] = None) -> tuple[list[dict], int]:
    """Fetch market-wide far-dated large prints, dedup into raw.duckdb.

    Returns ``(rows, new_count)`` where ``rows`` is the list of
    (ticker, premium, option_type, delta, ned, ask_vol, bid_vol) tuples over
    the lookback window ending ``today``.

    The tape is read ``max_pages`` pages of ``limit`` rows: when every page came back
    full, older prints in the window were never fetched. That is logged and, when a
    ``warnings`` list is passed, appended to it so the board can say so.

    Every new print gets one ``run_at`` per scan (its start) and the ``fetched_at`` of its
    page, read from ``calendar._now`` right after the page came back (#295). A caller that
    passes ``now`` (a test) gets that instant for both.
    """
    run_at = (now or calendar._now()).timestamp()
    new = 0
    capped = True
    # The real client answers a failed read with an empty page, not an exception: read its outcome
    # (``option_trades_result``) so a failed page is not taken for the end of the tape (#377 L4). A test
    # double that offers only ``option_trades`` still works.
    read = getattr(uw, "option_trades_result", None)
    for page in range(1, max_pages + 1):
        try:
            if read is not None:
                res = read(min_dte=min_dte, min_premium=min_premium, limit=limit, page=page)
                if res.status == "failed":
                    raise RuntimeError((res.error or {}).get("error") or f"{res.endpoint} request failed")
                rows = res.data or []
            else:
                rows = uw.option_trades(min_dte=min_dte, min_premium=min_premium,
                                        limit=limit, page=page)
            fetched_at = run_at if now is not None else calendar._now().timestamp()
        except Exception as exc:
            logger.warning("option-trades page %d failed: %s", page, exc)
            if page == 1 and warnings is not None:
                # No page came back at all: without this the board's empty state reads like an empty tape (#377 L4).
                warnings.append(f"The print scan's first page failed ({type(exc).__name__}: {exc}); "
                                "no far-dated prints were fetched, so this is not an empty tape.")
            capped = False
            break
        if not rows:
            capped = False
            break
        prints = [p for p in (_normalize_print(r) for r in rows if _is_stock(r)) if p]
        new += store_prints(prints, today, run_at=run_at, fetched_at=fetched_at)
        if len(rows) < limit:
            capped = False
            break
    if capped:
        msg = (f"The print scan hit its cap of {max_pages * limit:,} prints "
               f"({max_pages} pages of {limit}); older far-dated prints were not fetched, "
               "so the flow figures may be incomplete.")
        logger.warning(msg)
        if warnings is not None:
            warnings.append(msg)
    rows = load_recent_prints(today=today)
    return rows, new


def load_recent_prints(days: int = 5, today: Optional[date] = None) -> list[tuple]:
    """Prints that EXECUTED within the last ``days`` days of ``today`` (injectable clock).

    The window keys on ``executed_at``: ``discovered_at`` is only when the store first
    saw a print, so a re-fetch of month-old trades made them look five minutes old.
    A row with no ``executed_at`` falls back to ``discovered_at``.
    """
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    rows = rawstore.query(
        "SELECT ticker, premium, option_type, delta, next_earnings_date, "
        "ask_vol, bid_vol FROM leaps_flow_prints "
        "WHERE COALESCE(NULLIF(executed_at, ''), discovered_at) >= ?", [since])
    return [(r["ticker"], r["premium"], r["option_type"], r["delta"],
             r["next_earnings_date"], r["ask_vol"], r["bid_vol"]) for r in rows]


def _in_band(delta, band: tuple) -> bool:
    return delta is not None and band[0] <= abs(delta) <= band[1]


def aggregate_flow_prints(rows: list[tuple], today: Optional[date] = None) -> dict:
    """(ticker, premium, option_type, delta, ned, ask_vol, bid_vol) -> stats.

    Per ticker: frequency, gross notional, call premium, ITM-call premium,
    OTM-call premium, premium-weighted ask-side share, and near-event flag.
    """
    d = today or date.today()
    agg = defaultdict(lambda: {"frequency": 0, "notional": 0.0, "call_prem": 0.0,
                               "itm_call_prem": 0.0, "otm_call_prem": 0.0,
                               "ask_num": 0.0, "ask_den": 0.0, "near_event": False})
    for ticker, premium, otype, delta, ned, ask_vol, bid_vol in rows:
        a = agg[ticker]
        a["frequency"] += 1
        prem = premium or 0.0
        a["notional"] += prem
        is_call = otype == "call"
        if is_call:
            a["call_prem"] += prem
        if _in_band(delta, ITM_DELTA):
            if is_call:
                a["itm_call_prem"] += prem
        if _in_band(delta, OTM_DELTA):
            if is_call:
                a["otm_call_prem"] += prem
        if ask_vol is not None and bid_vol is not None and (ask_vol + bid_vol) > 0:
            a["ask_num"] += prem * (ask_vol / (ask_vol + bid_vol))
            a["ask_den"] += prem
        if ned:
            try:
                if 0 <= (date.fromisoformat(ned) - d).days <= NEAR_EVENT_DAYS:
                    a["near_event"] = True
            except ValueError:
                pass
    out = {}
    for ticker, a in agg.items():
        out[ticker] = {
            "frequency": a["frequency"],
            "notional": a["notional"],
            "call_prem": a["call_prem"],
            "itm_call_prem": a["itm_call_prem"],
            "otm_call_prem": a["otm_call_prem"],
            "ask_share": round(a["ask_num"] / a["ask_den"], 4) if a["ask_den"] > 0 else None,
            "near_event": a["near_event"],
        }
    return out


def concentration_multiplier(stat: dict) -> tuple:
    """(C, itm_conc, otm_conc): tiered multiplier on delta concentration.

    C = 1.0 + 0.2·(ITM-call premium share ≥ ITM_CONC_MIN)
            + 0.1·(OTM-call premium share ≥ OTM_CONC_MIN)
    """
    total = stat["notional"]
    if total <= 0:
        return 1.0, None, None
    itm_conc = stat["itm_call_prem"] / total
    otm_conc = stat["otm_call_prem"] / total
    c = 1.0
    if itm_conc >= ITM_CONC_MIN:
        c += 0.2
    if otm_conc >= OTM_CONC_MIN:
        c += 0.1
    return round(c, 2), round(itm_conc, 4), round(otm_conc, 4)


def _minmax(xs: list[float]) -> list[float]:
    lo, hi = min(xs), max(xs)
    if hi == lo:
        return [1.0] * len(xs)
    return [(x - lo) / (hi - lo) for x in xs]


def score_flow_candidates(agg: dict) -> list[dict]:
    """Min-max normalize frequency + log-notional, × concentration × 100."""
    if not agg:
        return []
    rows = []
    for ticker, stat in agg.items():
        mult, itm_conc, otm_conc = concentration_multiplier(stat)
        call_share = (stat["call_prem"] / stat["notional"]) if stat["notional"] > 0 else None
        net_notional = 2 * stat["call_prem"] - stat["notional"]
        rows.append({
            "ticker": ticker,
            "frequency": stat["frequency"],
            "notional": stat["notional"],
            "net_notional": round(net_notional, 1),
            "log_notional": math.log10(stat["notional"] + 1),
            "concentration": itm_conc,
            "itm_conc": itm_conc,
            "otm_conc": otm_conc,
            "concentration_mult": mult,
            "call_share": round(call_share, 4) if call_share is not None else None,
            "ask_share": stat["ask_share"],
            "near_event": stat["near_event"],
        })
    f_norm = _minmax([r["frequency"] for r in rows])
    v_norm = _minmax([r["log_notional"] for r in rows])
    for i, r in enumerate(rows):
        r["final_score"] = round(
            min(100.0, (0.4 * f_norm[i] + 0.6 * v_norm[i]) * r["concentration_mult"] * 100.0), 1)
    return sorted(rows, key=lambda r: -r["final_score"])


def radar_snapshot(candidate: dict, today: Optional[date] = None) -> dict:
    """Build a flow-print-only snapshot dict (label + score + metrics).

    The score is the flow score, so ``flow_score`` carries it and ``footprint_score``
    (OI + IV, a watchlist-only measure) is None.

    An all-bid flow (``ask_share`` below ``ASK_SHARE_MIN``) is not "Accumulating" (O10, D3, #377 L3); no prints
    (``ask_share`` None) are not gated. Near-term earnings are a *tag* (orange warning), not a hard drop.
    """
    gated = candidate.get("ask_share") is not None and candidate["ask_share"] < ASK_SHARE_MIN
    label = "Quiet"
    if candidate["call_share"] is not None:
        if candidate["call_share"] >= CALL_SHARE_MIN and not gated:
            label = "Accumulating"
        elif candidate["call_share"] <= 0.40:
            label = "Distributing"
    return {
        "ticker": candidate["ticker"],
        "label": label,
        "score": int(round(candidate["final_score"])),
        "footprint_score": None,
        "flow_score": int(round(candidate["final_score"])),
        "frequency": candidate["frequency"],
        "notional": candidate["notional"],
        "net_notional": candidate.get("net_notional"),
        "concentration": candidate.get("concentration"),
        "itm_conc": candidate.get("itm_conc"),
        "otm_conc": candidate.get("otm_conc"),
        "concentration_mult": candidate["concentration_mult"],
        "call_share": candidate["call_share"],
        "ask_share": candidate.get("ask_share"),
        "far_call_oi": None, "far_put_oi": None, "far_call_share": candidate["call_share"],
        "far_oi_growth_1d_pct": None, "far_oi_growth_q_pct": None,
        "iv_rank": None, "iv_rank_slope": None, "far_call_sweep_pct": None,
        "near_term_event": candidate["near_event"], "as_of": (today or date.today()).isoformat(),
    }


def bucket_score(score) -> str:
    """Tier a score: high (≥50) / emerging (20–49) / quiet (<20)."""
    if score is None:
        return "quiet"
    if score >= 50:
        return "high"
    if score >= 20:
        return "emerging"
    return "quiet"
