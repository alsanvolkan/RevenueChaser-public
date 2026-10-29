"""Sector & Theme Rotation Engine — RRG-style relative strength vs SPY."""
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional
import numpy as np

from .universe import ROTATION_ETFS, MACRO_SERIES
from RevenueChaser.core import rawstore
from RevenueChaser.core.gics_map import UNCLASSIFIED, canonical_sector, is_unclassified
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot

logger = logging.getLogger(__name__)

LOOKBACKS = {"rs_1w": 1, "rs_1m": 4, "rs_3m": 12}

# Daily trading-day lookbacks for ETF holdings RS (≈ 1W / 1M / 3M).
HOLDING_LOOKBACKS = {"rs_1w": 5, "rs_1m": 21, "rs_3m": 63}


def _finite(v):
    """True for numbers that are not None and not NaN."""
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def _normalize_ohlc(rows: list[dict]) -> list[dict]:
    """One regular-session close per date, oldest first.

    The rule now lives in the provider (core/providers/ohlc.py, review X2) so
    Earnings and the Rotation backfill use the same bars; kept here as the
    name the board and money flow already call. Daily bars arrive already
    session-clean from ``UWClient``; this also serves weekly candles, where a
    week with only an extended-hours row is kept (``sessions_only=False``), as
    before.
    """
    from RevenueChaser.core.providers.ohlc import regular_session_bars
    return regular_session_bars(rows, sessions_only=False)


def _closes_from(rows: list[dict]) -> list[float]:
    return [float(c["close"]) for c in _normalize_ohlc(rows)]


# SPY closes the holdings RS needs: the longest lookback plus today's close. Fewer means the
# benchmark is missing (a failed fetch looks exactly like this), and every RS would read 0.0.
SPY_MIN_CLOSES = HOLDING_LOOKBACKS["rs_3m"] + 1


def fetch_etf_bars(uw, ticker: str, days: int, candle_size: str = "1d") -> ProviderResult:
    """Candles for ``ticker`` with the request outcome: ``ok`` (bars may be empty) or ``failed``.

    A client without ``etf_ohlc_result`` (a test fake, an older client) is taken at its word:
    whatever list it returns is an ``ok`` answer; an exception is a ``failed`` one.
    """
    try:
        fn = getattr(uw, "etf_ohlc_result", None)
        if fn is not None:
            res = fn(ticker, days=days, candle_size=candle_size)
            if isinstance(res, ProviderResult):
                return res
        return ProviderResult("ok", uw.etf_ohlc(ticker, days=days, candle_size=candle_size) or [], None, "")
    except Exception as exc:  # noqa: BLE001 - a provider fault is a failed result, never a crash
        return ProviderResult("failed", [], {"error": type(exc).__name__}, "")


def etf_fetch_failed(res: ProviderResult) -> bool:
    """True when the provider could not answer: no HTTP status (a transport error, or a client that raised),
    429 or 5xx. 401/403 count too: our key was rejected, so every fetch fails and "no data" would hide a dead
    key. Any other 4xx (404 symbol not listed, 400/422 a request UW refuses) is about that one ETF, so it is
    "no data": a persistent refusal for one ETF must not freeze Money Flow or block every backfill day."""
    if res.status == "ok":
        return False
    status = (res.error or {}).get("status")
    return status is None or status in (401, 403, 429) or status >= 500


def fetch_spy_closes(uw, days: int = 90) -> list[float]:
    """SPY's regular-session closes, oldest first; ``[]`` when the fetch failed or came back empty."""
    return _closes_from(fetch_etf_bars(uw, "SPY", days).data or [])


def relative_strength_pct(etf_closes: list[float], spy_closes: list[float], lookback: int) -> float:
    if len(etf_closes) <= lookback or len(spy_closes) <= lookback:
        return 0.0
    etf_ret = etf_closes[-1] / etf_closes[-1 - lookback] - 1
    spy_ret = spy_closes[-1] / spy_closes[-1 - lookback] - 1
    return round((etf_ret - spy_ret) * 100, 2)


def holdings_with_rs(ticker: str, uw, max_holdings: int = 10,
                     spy_closes: Optional[list[float]] = None) -> list[dict]:
    """Top ETF holdings with SPY-relative strength, capped at `max_holdings`.

    Resolves holdings via UW -> yfinance, then computes SPY-relative RS.
    Returns {ticker, name, weight, close, rs_1w, rs_1m, rs_3m}. A job that does this for many
    ETFs fetches SPY once and passes it as ``spy_closes``.
    """
    try:
        from .etf_holdings import get_etf_holdings
        holdings = get_etf_holdings(ticker, uw)
    except Exception:
        return []
    return holdings_with_rs_from(holdings, uw, max_holdings=max_holdings, spy_closes=spy_closes)


def holdings_with_rs_from(holdings: list[dict], uw, max_holdings: int = 10,
                          spy_closes: Optional[list[float]] = None) -> list[dict]:
    """Compute SPY-relative RS for a pre-resolved holdings list (no re-fetch).

    ``spy_closes`` is SPY's closes when the caller already has them (one fetch per job); otherwise
    they are fetched here. Without at least ``SPY_MIN_CLOSES`` of them every RS would read 0.0, so
    the answer is ``[]`` (no RS this run) and the caller keeps what it stored.
    """
    if spy_closes is None:
        spy_closes = fetch_spy_closes(uw)
    if len(spy_closes) < SPY_MIN_CLOSES:
        logger.warning("holdings RS: SPY has %d closes, need %d; no RS computed", len(spy_closes), SPY_MIN_CLOSES)
        return []

    rows = []
    for h in holdings:
        t = h.get("ticker")
        if not t:
            continue
        try:
            ohlc = uw.etf_ohlc(t, days=90, candle_size="1d") or []
        except Exception:
            ohlc = []
        closes = _closes_from(ohlc)
        if not closes:
            continue  # no OHLC data (foreign/unpriced listing)
        rows.append({
            "ticker": t,
            "name": h.get("name") or "",
            "weight": _safe_float(h.get("weight")),
            "close": closes[-1],
            "rs_1w": relative_strength_pct(closes, spy_closes, HOLDING_LOOKBACKS["rs_1w"]),
            "rs_1m": relative_strength_pct(closes, spy_closes, HOLDING_LOOKBACKS["rs_1m"]),
            "rs_3m": relative_strength_pct(closes, spy_closes, HOLDING_LOOKBACKS["rs_3m"]),
        })

    rows.sort(key=lambda r: r["weight"], reverse=True)
    return rows[:max_holdings]


def _industry_label(value) -> str:
    """A stored industry for the tree: blank, ``Other`` and ``Unclassified`` are one bucket, ``Unclassified``."""
    return UNCLASSIFIED if not value or is_unclassified(value) else str(value).strip() or UNCLASSIFIED


def _unknown_last(label: str) -> tuple[bool, str]:
    """Sort key: real labels alphabetically, the ``Unclassified`` bucket after them."""
    return (label == UNCLASSIFIED, label)


def industry_buckets(rows: list[dict]) -> dict[tuple, list[dict]]:
    """``{(sector, industry): members}`` from universe rows (pure; no store access).

    The sector is read through ``canonical_sector`` (a stored GICS spelling lands under its Morningstar name; a
    blank, ``Other`` or unknown one is ``Unclassified``). Members without a positive market cap are dropped. A
    blank, ``Other`` or ``Unclassified`` industry is the one ``Unclassified`` bucket (the identity master writes
    ``Unclassified``; ``Other`` is the pre-R3 spelling).
    """
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        try:
            mc = float(r.get("market_cap"))
        except (TypeError, ValueError):
            continue
        if mc <= 0:
            continue
        buckets[(canonical_sector(r.get("sector")),
                 _industry_label(r.get("industry")))].append({
            "ticker": r.get("ticker") or "",
            "name": r.get("name") or "",
            "market_cap": mc,
        })
    return buckets


def sector_industry_tree(max_per_industry: int = 12) -> dict[str, list[dict]]:
    """Morningstar sector -> industry -> cap-weighted members.

    Built from the shared universe in the store (``equity_universe``), so it
    needs no network and stays consistent with every other board's sector and
    industry labels. ``weight`` is each member's share of its own industry's
    market cap, so an industry's weights sum to ~100% (before the
    ``max_per_industry`` cut). Members without a market cap are dropped; an
    industry with none is omitted.
    """
    rows = rawstore.query(
        "SELECT ticker, name, sector, industry, market_cap "
        "FROM equity_universe")
    return build_industry_tree(rows, max_per_industry)


def build_industry_tree(rows: list[dict], max_per_industry: int = 12) -> dict[str, list[dict]]:
    """``sector_industry_tree`` from universe rows (pure).

    Within a sector the ``Unclassified`` industry is sorted last.
    """
    buckets = industry_buckets(rows)

    grouped: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    for (sec, ind), members in buckets.items():
        total = sum(m["market_cap"] for m in members)
        if total <= 0:
            continue
        members.sort(key=lambda m: m["market_cap"], reverse=True)
        for m in members:
            m["weight"] = round(m["market_cap"] / total * 100, 2)
        grouped[sec][ind] = members[:max_per_industry]

    return {sec: [{"industry": ind, "holdings": industries[ind]}
                  for ind in sorted(industries, key=_unknown_last)]
            for sec, industries in grouped.items()}


def sector_etf_trees(max_per_industry: int = 12) -> dict[str, list[dict]]:
    """``{sector ETF ticker: industry tree}`` for the rotation map's sector rows.

    ``XLK`` -> Technology's tree, etc. Theme/region/bond rows are not sectors
    and are excluded (they keep their ETF holdings).
    """
    return etf_trees(sector_industry_tree(max_per_industry=max_per_industry))


def etf_trees(tree: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """``{sector ETF ticker: that sector's tree}`` from a ``sector_industry_tree`` result (pure).

    A sector ETF is matched on its ``ROTATION_ETFS`` name, which is the Morningstar sector name stored in
    ``equity_universe.sector``.
    """
    out: dict[str, list[dict]] = {}
    for ticker, info in ROTATION_ETFS.items():
        if info.get("group") != "sector":
            continue
        if info.get("name") in tree:
            out[ticker] = tree[info["name"]]
    return out


# Calendar days of the prices store the holdings RS reads before the rotation session: 63 sessions back plus holidays and margin.
HOLDINGS_READ_DAYS = 120


def holdings_rs_rows(holdings: list[dict], points: dict[str, list[tuple[date, float]]],
                     spy: list[tuple[date, float]], as_of: date, max_holdings: int = 10) -> list[dict]:
    """Top holdings with SPY-relative strength from the stored daily closes (M-ROT-3, ROT-8); pure, no read, no fetch.

    The lookbacks (``HOLDING_LOOKBACKS``) are counted in SPY's sessions up to ``as_of``: a holding's RS over L sessions
    compares its close on SPY's last session with its close L sessions before, both on the same dates as SPY's. A
    holding the store holds no bar of on either date gets ``None`` for that RS (shown "n/a", a dash, DF67), and one with no
    bar at all keeps its row with ``close`` ``None`` too: it is never dropped silently. ``[]`` when SPY has fewer than
    ``SPY_MIN_CLOSES`` sessions (every RS would be missing), so the caller keeps what it stored.
    """
    spy = sorted((d, c) for d, c in spy if d <= as_of and _finite(c))
    if len(spy) < SPY_MIN_CLOSES:
        logger.warning("holdings RS: the prices store holds %d SPY sessions up to %s, need %d; no RS computed",
                       len(spy), as_of, SPY_MIN_CLOSES)
        return []
    dates = [d for d, _ in spy]
    spy_at = dict(spy)
    end = dates[-1]
    rows = []
    for h in holdings:
        t = h.get("ticker")
        if not t:
            continue
        series = {d: c for d, c in points.get(t, []) if d <= as_of and _finite(c)}
        row = {"ticker": t, "name": h.get("name") or "", "weight": _safe_float(h.get("weight")),
               "close": series[max(series)] if series else None}
        for key, lookback in HOLDING_LOOKBACKS.items():
            start = dates[-1 - lookback]
            a, b = series.get(start), series.get(end)
            row[key] = (None if a is None or b is None or not a or not spy_at[start]
                        else round(((b / a - 1) - (spy_at[end] / spy_at[start] - 1)) * 100, 2))
        rows.append(row)
    rows.sort(key=lambda r: r["weight"], reverse=True)
    return rows[:max_holdings]


def _session_of(snapshots) -> date | None:
    """The rotation session the snapshots are of (their newest ``as_of_date``), or ``None``."""
    days = []
    for s in snapshots:
        d = getattr(s, "as_of_date", None)
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d[:10])
            except ValueError:
                continue
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date):
            days.append(d)
    return max(days) if days else None


def fetch_all_holdings(uw, snapshots, max_holdings: int = 10, max_workers: int = 8) -> dict[str, list[dict]]:
    """Top holdings (with SPY-relative RS) for every theme ticker.

    Used to embed holdings into the exported rotation map so it is self-contained
    (no live server round-trip needed to expand a row). The holdings come from the store, written by the
    holdings job (#303, D25); an ETF with no stored holdings gets none. Sector rows expand to the
    sector -> industry tree (``sector_etf_trees``) instead, so their ETF holdings are not priced (ROT-4).

    The RS reads the prices store (M-ROT-3, PR 9): one ``rcdata.read.ticker_bars`` read of SPY and every holding up
    to the rotation session; ``uw`` (and ``max_workers``) are kept for the callers' signature and not used. A holding
    with no stored bars shows "n/a" (``holdings_rs_rows``). ``{}`` when SPY is short in the store.
    """
    from RevenueChaser.core import calendar
    from .etf_holdings import stored_holdings

    tickers = [s.ticker for s in snapshots if getattr(s, "group_name", "") == "theme"]
    held = {t: h for t in tickers if (h := stored_holdings(t))}
    if not held:
        return {}
    as_of = _session_of(snapshots)
    if as_of is None:
        try:
            as_of = calendar.latest_complete_session()
        except ValueError:
            return {}
    wanted = sorted({h.get("ticker") for rows in held.values() for h in rows if h.get("ticker")} - {"SPY"})
    points = bar_points(["SPY", *wanted], as_of - timedelta(days=HOLDINGS_READ_DAYS), as_of)
    spy = points.get("SPY", [])
    if len(spy) < SPY_MIN_CLOSES:                       # no RS this run: the caller keeps what it stored
        logger.warning("holdings RS: the prices store holds %d SPY sessions up to %s, need %d", len(spy), as_of,
                       SPY_MIN_CLOSES)
        return {}
    out: dict[str, list[dict]] = {}
    for t, rows in held.items():
        if got := holdings_rs_rows(rows, points, spy, as_of, max_holdings):
            out[t] = got
    return out


def _safe_float(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def calculate_ratio_and_momentum(rs_1w: float, rs_1m: float, rs_3m: float) -> tuple[float, float]:
    ratio = 0.20 * rs_1w + 0.35 * rs_1m + 0.45 * rs_3m
    momentum = 0.55 * rs_1w + 0.30 * rs_1m - 0.15 * rs_3m
    return round(ratio, 2), round(momentum, 2)


def classify_rrg(ratio_z: float, momentum_z: float) -> str:
    if ratio_z >= 0 and momentum_z >= 0:
        return "LEADING"
    if ratio_z >= 0 and momentum_z < 0:
        return "WEAKENING"
    if ratio_z < 0 and momentum_z >= 0:
        return "IMPROVING"
    return "LAGGING"


def classify_quadrant(rs_ratio: float, rs_momentum: float) -> str:
    """Strict RRG quadrant classification on Z-scores (Ratio Z, Momentum Z)."""
    return classify_rrg(rs_ratio, rs_momentum)


def zscore_normalize(values: list[float]) -> list[float]:
    arr = np.array([v for v in values if _finite(v)], dtype=np.float64)
    if arr.size == 0:
        return [0.0] * len(values)
    mean = arr.mean()
    std = arr.std(ddof=0)
    if std == 0:
        return [0.0] * len(values)
    return ((arr - mean) / std).round(4).tolist()


# ── the RRG cross-section from the stored bars (M-ROT-2, #374: D-M3 "weekly, both"; #283 session date; PR 9) ─────────
MIN_WEEKS = 48                    # weekly closes an ETF (and SPY) needs to be on the map; fewer is "too short"
READ_WEEKS = MIN_WEEKS + 8        # the window read before a session: enough ISO weeks for the floor


def _weekly(points, as_of: date) -> dict[tuple, float]:
    """``{(ISO year, ISO week): close}``, oldest first, from ``(session, close)`` daily points up to ``as_of``."""
    weeks: dict[tuple, float] = {}
    for d, close in sorted(points, key=lambda p: p[0]):
        if d > as_of or not _finite(close):
            continue
        weeks[tuple(d.isocalendar())[:2]] = float(close)      # dict order = week order; the last session wins
    return weeks


def weekly_closes(points, as_of: date) -> list[float]:
    """ISO-week closes, oldest first, from ``(session, close)`` daily points: each week's last session up to
    ``as_of``. The current week is closed at ``as_of`` (the store holds complete sessions only, so never intraday)."""
    return list(_weekly(points, as_of).values())


def aligned_weekly_closes(points, benchmark_points, as_of: date) -> list[float]:
    """``weekly_closes`` of one symbol lined up with the benchmark's ISO weeks, so the 1/4/12-week lookbacks of
    ``relative_strength_pct`` (counted from the end of both lists) compare the same weeks.

    ``[]`` when the symbol has no close in the benchmark's last week: a series that stopped early is not plotted as of
    the session (it is missing). A benchmark week the symbol lacks in between carries its previous week's close
    (a 0% week) instead of shifting every older week by one (review 2026-10-16)."""
    own = _weekly(points, as_of)
    weeks = list(_weekly(benchmark_points, as_of))
    if not own or not weeks or weeks[-1] not in own:
        return []
    out, last = [], None
    for w in weeks:
        last = own.get(w, last)
        if last is not None:
            out.append(last)
    return out


def bar_points(tickers, start: date, end: date) -> dict[str, list[tuple[date, float]]]:
    """``{ticker: [(session, close), ...]}`` from one prices-store read (``rcdata.read.ticker_bars``, split basis: the
    level a chart shows, as D01's "nominal" was); never fetches. A ticker the store holds no bar of in the window is
    absent."""
    from RevenueChaser.rcdata import read
    frame = read.ticker_bars(list(tickers), start=start, end=end, basis="split")
    out: dict[str, list[tuple[date, float]]] = {}
    if frame is None or frame.empty:
        return out
    for t, d, c in zip(frame["ticker"], frame["session_date"], frame["close"]):
        if _finite(c):
            out.setdefault(str(t), []).append((d.date() if hasattr(d, "date") else d, float(c)))
    return out


def macro_points(start: date, end: date, tickers=None) -> dict[str, list[tuple[date, float]]]:
    """``{macro symbol: [(day, value), ...]}`` from one ``market_series`` read (PR 9): each symbol's own row
    (``MACRO_SERIES[...]["series"]``: FRED's yields and oil, Cboe's VVIX, Yahoo's MOVE and wheat). A symbol with no
    row in the window is absent; nothing is fetched (the market build stores the series)."""
    from RevenueChaser.rcdata import read
    wanted = {t: tuple(MACRO_SERIES[t]["series"]) for t in (tickers if tickers is not None else MACRO_SERIES)
              if (MACRO_SERIES.get(t) or {}).get("series")}
    if not wanted:
        return {}
    of = {v: t for t, v in wanted.items()}
    out: dict[str, list[tuple[date, float]]] = {}
    for r in read.market_series(sorted({sid for sid, _ in wanted.values()}), start=start, end=end):
        t = of.get((r["series_id"], r["source"]))
        if t is not None and _finite(r.get("value")):
            out.setdefault(t, []).append((r["obs_date"], float(r["value"])))
    return out


def rrg_tickers() -> list[str]:
    """The RRG symbols the prices store can hold: every rotation ETF but the benchmark and the macro series."""
    return [t for t in ROTATION_ETFS if t != "SPY" and t not in MACRO_SERIES]


def too_short(closes: dict[str, list[float]]) -> list[str]:
    """Symbols with some weekly closes but fewer than ``MIN_WEEKS``, in universe order: a warning, not a failure."""
    return [t for t in ROTATION_ETFS if 0 < len(closes.get(t) or []) < MIN_WEEKS]


def cross_section(closes: dict[str, list[float]], spy_closes: list[float], as_of: date) -> list[ETFRotationSnapshot]:
    """The RRG cross-section of ``as_of``: one snapshot per symbol of ``closes`` with at least ``MIN_WEEKS`` weekly
    closes, SPY-relative strength over ``LOOKBACKS`` weeks, z-scores across the snapshots and the quadrant.

    The one function the live Fetch and the backfill share (D-M3): the same closes give the same rows.
    """
    snaps = []
    for ticker, info in ROTATION_ETFS.items():
        series = closes.get(ticker) or []
        if ticker == "SPY" or len(series) < MIN_WEEKS:
            continue
        # Yield series are interest rates, not prices: a rising yield is bearish.
        # Invert so that *falling* yields register as positive RS (outperforming).
        rs_closes = [-c for c in series] if info.get("group") == "yield" else series
        rs_1w = relative_strength_pct(rs_closes, spy_closes, LOOKBACKS["rs_1w"])
        rs_1m = relative_strength_pct(rs_closes, spy_closes, LOOKBACKS["rs_1m"])
        rs_3m = relative_strength_pct(rs_closes, spy_closes, LOOKBACKS["rs_3m"])
        ratio, momentum = calculate_ratio_and_momentum(rs_1w, rs_1m, rs_3m)
        snaps.append(ETFRotationSnapshot(
            as_of_date=as_of, ticker=ticker, group_name=info.get("group", ""), close_price=series[-1],
            rs_1w=rs_1w, rs_1m=rs_1m, rs_3m=rs_3m, rs_ratio=ratio, rs_momentum=momentum))
    normalize_z_scores(snaps)
    return snaps


def normalize_z_scores(snapshots: list[ETFRotationSnapshot]) -> None:
    """Cross-sectional z-scores of ratio and momentum, and the quadrant, set on ``snapshots`` in place."""
    ratios = [s.rs_ratio for s in snapshots if _finite(s.rs_ratio)]
    momentums = [s.rs_momentum for s in snapshots if _finite(s.rs_momentum)]
    if not ratios or not momentums:
        return
    ratio_zs = zscore_normalize(ratios)
    momentum_zs = zscore_normalize(momentums)
    # assign z-scores value-by-value over finite snapshots (not positionally),
    # so a None/NaN entry can never misalign subsequent tickers.
    ri = mi = 0
    for snap in snapshots:
        if _finite(snap.rs_ratio):
            snap.rs_ratio_z = ratio_zs[ri]
            ri += 1
        if _finite(snap.rs_momentum):
            snap.rs_momentum_z = momentum_zs[mi]
            mi += 1
        if snap.rs_ratio_z is not None and snap.rs_momentum_z is not None:
            snap.rrg_quadrant = classify_quadrant(snap.rs_ratio_z, snap.rs_momentum_z)


class RotationEngine:
    """The RRG pull (PR 9): weekly closes of the ETFs from the prices store, the 11 macro series from ``market_series``
    (``macro_points``), both lined up with SPY's weeks, one ``cross_section`` dated by the session its closes end on.
    Nothing is fetched: a Fetch and a Refresh of the same stores give the same map.

    After ``process_all``:
    - ``as_of`` is that session: the latest complete session, or SPY's last stored session before it when the prices
      build has not stored it yet (``stale`` then says so);
    - ``intraday`` is true when the run happened while the US session was open (rule B, ``calendar.session_for``);
    - ``too_short`` lists the symbols left off the map for having under ``MIN_WEEKS`` weekly closes (a warning).
    ``uw`` is not called for closes any more; it is kept for the callers that pass one.
    """

    def __init__(self, uw: UWClient = None, benchmark: str = "SPY", now: datetime | None = None):
        self.uw = uw
        self.benchmark = benchmark
        self.now = now
        self.spy_closes: list[float] = []
        self.snapshots: list[ETFRotationSnapshot] = []
        self.as_of: date | None = None
        self.stale: str | None = None
        self.intraday = False
        self.too_short: list[str] = []

    def process_all(self) -> list[ETFRotationSnapshot]:
        from RevenueChaser.core import calendar
        self.snapshots, self.too_short, self.stale = [], [], None
        try:
            session = calendar.latest_complete_session(self.now)
            self.intraday = calendar.session_for(
                self.now or calendar._now()).quality == calendar.QUALITY_INTRADAY
        except ValueError as exc:                       # outside the holiday table: no session to date it by
            logger.warning("Rotation: no session for this run: %s", exc)
            return []
        start = session - timedelta(weeks=READ_WEEKS)
        points = bar_points([self.benchmark, *rrg_tickers()], start, session)
        spy = points.get(self.benchmark) or []
        if not spy:
            logger.warning("Rotation: the prices store holds no %s bar up to %s", self.benchmark, session)
            return []
        self.as_of = spy[-1][0]
        if self.as_of < session:
            self.stale = (f"the prices store has no {self.benchmark} bar of the {session.isoformat()} session yet "
                          f"(the prices build has not stored it); the RRG is as of {self.as_of.isoformat()}")
        self.spy_closes = weekly_closes(spy, self.as_of)
        if len(self.spy_closes) < MIN_WEEKS:
            logger.warning("Rotation: %s has %d weekly closes in the prices store, need %d",
                           self.benchmark, len(self.spy_closes), MIN_WEEKS)
            return []
        closes = {t: aligned_weekly_closes(points.get(t) or [], spy, self.as_of) for t in rrg_tickers()}
        macro = macro_points(start, self.as_of, [t for t in MACRO_SERIES if t in ROTATION_ETFS])
        for ticker in MACRO_SERIES:
            if ticker in ROTATION_ETFS:
                closes[ticker] = aligned_weekly_closes(macro.get(ticker) or [], spy, self.as_of)
        self.too_short = too_short(closes)
        self.snapshots = cross_section(closes, self.spy_closes, self.as_of)
        return self.snapshots


def ticker_rotation_adjustment(
    quadrant: str,
    rotation_score: float = 50,
    flow_score: float = 50,
) -> tuple[int, float]:
    if quadrant == "LEADING" and flow_score >= 60:
        return 10, 1.00
    if quadrant == "IMPROVING" and flow_score >= 60:
        return 5, 0.75
    if quadrant == "WEAKENING":
        return -5, 0.60
    if quadrant == "LAGGING" and flow_score < 40:
        return -10, 0.50
    return 0, 0.75


CLUSTER_TO_ETFS: dict[str, list[tuple[str, float]]] = {
    "C1_saas":              [("IGV", 0.40), ("WCLD", 0.35), ("SKYY", 0.25)],
    "C2_hardware":          [("SMH", 0.50), ("SOXX", 0.30), ("XLK", 0.20)],
    "C3_fintech_lending":   [("XLF", 0.55), ("KRE", 0.25), ("XHB", 0.20)],
    "C4_crypto_mining":     [("IWM", 0.40), ("ARKW", 0.35), ("XLK", 0.25)],
    "C5_marketplace_consumer": [("XLY", 0.50), ("JETS", 0.25), ("XRT", 0.25)],
    "C6_energy_utilities":  [("XLE", 0.50), ("XOP", 0.25), ("XLU", 0.25)],
    "C7_healthcare":        [("IHI", 0.40), ("IBB", 0.30), ("XBI", 0.30)],
    "unmapped":             [("SPY", 1.00)],
}

QUADRANT_ORDER = {"LEADING": 0, "IMPROVING": 1, "WEAKENING": 2, "LAGGING": 3}


def get_quadrant_for_ticker(ticker: str, cluster: str, snapshots_map: dict[str, str]) -> str:
    from .universe import TICKER_ETF_MAP
    exposures = TICKER_ETF_MAP.get(ticker) or CLUSTER_TO_ETFS.get(cluster, [("SPY", 1.0)])
    quadrants = {}
    for etf, weight in exposures:
        q = snapshots_map.get(etf, "")
        if q:
            quadrants[q] = quadrants.get(q, 0) + weight

    if not quadrants:
        return ""

    return max(quadrants, key=quadrants.get)


def rotation_quadrant_map(uw: UWClient = None, compute: bool = True,
                          as_of: date = None, benchmark: str = "SPY") -> dict[str, str]:
    """Earnings Radar's Sector Rotation dependency — same-session freshness policy.

    Returns ``{ETF ticker: quadrant}`` for ``as_of`` (default: the latest complete session, #283). A stored
    snapshot for that session is reused — the Sector Rotation board or an earlier run already refreshed it. When none
    exists and ``compute=True`` the engine runs once (stored closes, no call) and persists its snapshot under the
    session its closes end on, so Earnings Radar and the Sector Rotation board agree on the same read; a run that is
    not a complete session (intraday, or the store short of the session) never replaces a snapshot stored for that
    day.
    With ``compute=False`` only the store is read (zero network).
    """
    from RevenueChaser.core import calendar
    from . import store as rotation_store
    d = as_of or calendar.latest_complete_session()
    rows = (rotation_store.load_snapshot(d) or {}).get("etfs") or []
    if rows:
        return _quadrants(rows)
    if not compute:
        return {}
    engine = RotationEngine(uw, benchmark=benchmark)
    snapshots = engine.process_all()
    if not snapshots:
        return {}
    day = getattr(snapshots[0], "as_of_date", None) or d
    stored = (rotation_store.load_snapshot(day) or {}).get("etfs") or [] if day != d else []
    if stored and (getattr(engine, "stale", None) or getattr(engine, "intraday", False)):
        return _quadrants(stored)
    rotation_store.save_snapshot(snapshots, as_of=day, benchmark=benchmark)
    logger.info("Rotation: computed + persisted the %s snapshot (%d ETFs)", day.isoformat(), len(snapshots))
    return {s.ticker: s.rrg_quadrant for s in snapshots if s.rrg_quadrant}


def _quadrants(rows: list[dict]) -> dict[str, str]:
    return {r.get("ticker"): r.get("quadrant") for r in rows if r.get("ticker") and r.get("quadrant")}
