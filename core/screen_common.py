"""The shared skeleton of the Swing and Fundamentals scans (#147, 27-3a; U20).

Both boards ran the same ``run_scan`` body, copied: load the stored universe, drop the identity master's excluded
types (R5), prefilter on price and volume (the prices store's since PR 10, D04's quotes before), score every kept
ticker on a thread pool (no shortlist cap since C12, #383 R3: the whole screenable universe), build the payload, and persist on a Fetch. ``run_screen`` is that body once; what differs per board stays in the
board's package and is passed in: ``compute_row``, ``build_payload`` and ``persist`` (and the board's own report
and score tables).

``prefilter`` and ``spy_summary`` are the two helpers that were identical in both pipelines. ``prefilter`` is the one
hook for the quote-level hygiene rule (7-A: price and dollar volume, #383 R3 C12; from the prices store since PR 10); the thresholds come from the
board's config, which holds the same values in both boards today. ``load_bars`` is a scored name's D01 bars (levels
nominal, returns adjusted), read for a chunk of names at a time in a scan (``load_bars_many``); a name with no bar of
SPY's newest D01 session drops as ``stale_bars`` (#434 review S1, S3). ``funnel_steps`` is the payload's
``funnel_steps`` (C16): where every stored name went.

``load_spy`` is the SPY baseline of both boards. Since PR 10 (G4) "D01" in this module is the prices store
(``prices.duckdb``, read through ``rcdata.read`` and ``core.market_bars``): the scored names' bars, SPY's baseline and
the freeze's checks all come from it, and the old ``swing_ohlc`` SPY fallback is gone (F4 A, part 1).

A Refresh (``no_fetch=True``) scores nothing (K4, #383 R1): ``stored_payload`` returns the board the last Fetch
built (its history header's payload, or the saved board JSON when that is newer), so the board shows what that Fetch
showed.

Behaviour is unchanged except U5 (``core/tests/test_screen_parity_147.py`` pins that against a golden captured before
the refactor): a ticker whose row raises is counted as ``compute_error`` and the scan carries on, instead of the whole
scan dying on the first unguarded ``f.result()``.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pandas as pd

from RevenueChaser.core import calendar, earnings_dates, fiscal_year, funnel, job_control, ledger
from RevenueChaser.core import universe as _universe
from RevenueChaser.core.config import to_float
from RevenueChaser.core.gics_map import UNCLASSIFIED
from RevenueChaser.core.scan_quality import DROP_LABELS, count_drops, no_data_payload

logger = logging.getLogger(__name__)

WORKERS = 8


PREFILTER_REASONS = ("no_quote", "low_price", "low_dollar_volume")


@dataclass
class Prefiltered:
    """What the prefilter kept, and the tickers it dropped per reason (``PREFILTER_REASONS``, in input order).

    ``quote_session`` is the newest session of the quotes it read; ``older_quotes`` counts the names whose quote is
    from an earlier session (U9: the board says which session its prefilter saw). ``no_volume`` lists the kept names
    with no average volume (in the prices store since PR 10), which the dollar-volume rule could not test (S4, #434 review). ``cfg`` is
    the thresholds it dropped on, so the funnel names those and not the board's defaults (Swing review finding 1).
    """
    kept: list[dict]
    drops: dict[str, list[str]] = field(default_factory=dict)
    quote_session: date | None = None
    older_quotes: int = 0
    no_volume: list[str] = field(default_factory=list)
    cfg: Any = None

    @property
    def dropped(self) -> int:
        return sum(len(v) for v in self.drops.values())


def prefilter_inputs(tickers) -> dict[str, dict]:
    """``{ticker (the caller's spelling): {"price", "avg_volume", "session_date", "source"}}`` from the prices store
    (``rcdata.read.liquidity``, PR 10): the newest nominal close at most ``LATEST_WITHIN_DAYS`` old and the mean
    volume of the last ``LIQUIDITY_SESSIONS`` bars on today's share basis. A name with no current close is absent.

    shortcut: the mean rests on however many bars the store holds (fewer than 63 until Run H fills the history),
    upgrade when Run H has loaded the long history, so every name's mean covers the full window.
    """
    from RevenueChaser.rcdata import read
    asked = [t for t in (tickers or ()) if t and str(t).strip()]
    got = read.liquidity(asked)
    out: dict[str, dict] = {}
    for t in asked:
        row = got.get(str(t).strip().upper())
        if row is not None and row.get("price") is not None:
            out[t] = {"price": float(row["price"]), "session_date": row["price_session"], "source": "prices",
                      "avg_volume": None if row.get("avg_volume") is None else float(row["avg_volume"])}
    return out


def prefilter(rows: list[dict], cfg: Any, quotes: dict[str, dict] | None = None) -> Prefiltered:
    """The 7-A prefilter (avo 2026-10-02; #383 R3 C12): price and dollar volume only.

    The inputs are the prices store's since PR 10 (``prefilter_inputs``: each name's newest close and its mean volume
    over the last ``rcdata.read.LIQUIDITY_SESSIONS`` bars), not D04's ``quote_snapshot``, which no job fills since the
    27 Oct purge; ``quotes`` stands in for that read in tests. A name drops as ``no_quote`` with no current close,
    ``low_price`` under ``cfg.MIN_PRICE``, ``low_dollar_volume`` when price times the average daily volume is under
    ``cfg.MIN_DOLLAR_VOLUME``. A name with no volume is kept, since its price passed, and listed in ``no_volume`` so
    the board counts it (S4, #434 review). The trend rules (52-week distance, above the 50-day average) are gone: the
    gates test them on fresh bars.
    """
    if quotes is None:
        quotes = prefilter_inputs([r.get("ticker") for r in rows])
    kept: list[dict] = []
    drops: dict[str, list[str]] = {}
    no_volume: list[str] = []
    sessions: list[date] = []
    for r in rows:
        q = quotes.get(r.get("ticker")) or {}
        if q.get("session_date") is not None:
            sessions.append(q["session_date"])
        price = to_float(q.get("price"))
        volume = to_float(q.get("avg_volume"))
        if price is None:
            reason = "no_quote"
        elif price < cfg.MIN_PRICE:
            reason = "low_price"
        elif volume and price * volume < cfg.MIN_DOLLAR_VOLUME:
            reason = "low_dollar_volume"
        else:
            kept.append(r)
            if not volume:
                no_volume.append(r.get("ticker"))
            continue
        drops.setdefault(reason, []).append(r.get("ticker"))
    newest = max(sessions) if sessions else None
    return Prefiltered(kept, drops, newest, sum(1 for d in sessions if d < newest) if newest else 0, no_volume,
                       SimpleNamespace(MIN_PRICE=cfg.MIN_PRICE, MIN_DOLLAR_VOLUME=cfg.MIN_DOLLAR_VOLUME))


def spy_summary(spy_df) -> dict:
    """SPY price, 200-day average, 3-month return and the regime they give.

    No usable SPY trend means no regime (``None``), never a made-up RISK-ON (#145 U6).
    """
    out = {"regime": None, "spy_price": None, "spy_sma200": None, "spy_3m_return": None}
    if spy_df is None or spy_df.empty:
        return out
    close = spy_df["Close"]
    price = to_float(close.iloc[-1])
    sma200 = to_float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    spy_3m = to_float(close.iloc[-1] / close.iloc[-64] - 1.0) if len(close) >= 64 else None
    out["spy_price"] = round(price, 2) if price is not None else None
    out["spy_sma200"] = round(sma200, 2) if sma200 is not None else None
    out["spy_3m_return"] = round(spy_3m, 4) if spy_3m is not None else None
    if price is not None and sma200 is not None:
        out["regime"] = "RISK-ON" if price > sma200 else "RISK-OFF"
    return out


SPY_BASELINE_DAYS = 730          # the window of the old read (``load_ohlc``: yfinance ``period="2y"``)


def load_spy(now=None):
    """SPY's daily bars, the Swing and Fundamentals baseline (U1, #402; PR 10).

    The prices store's split- and dividend-adjusted series (``core.market_bars``, the total-return basis), as
    yfinance's ``auto_adjust`` was, over the last ``SPY_BASELINE_DAYS``, in the frame shape ``load_ohlc`` returned.
    ``attrs["stale"]`` is true unless SPY's newest bar is one complete latest session: the test the freeze anchor uses
    (``ledger.prices_has_session``), so the baseline and the anchor agree, and a stale SPY refuses the freeze
    (``payload_problem``: ``spy_stale``). The ``swing_ohlc`` fallback is gone (F4 A, part 1): no SPY bars is an empty,
    stale frame (no regime), never a yfinance read.
    """
    try:
        session = calendar.latest_complete_session(now)
        from RevenueChaser.core import market_bars
        frame = _ohlcv(market_bars.daily_bars(["SPY"], basis="adjusted",
                                              start=session - timedelta(days=SPY_BASELINE_DAYS)))
        frame.attrs["stale"] = not ledger.prices_has_session(session, "SPY")
        logger.info("SPY baseline: the prices store through %s (%d bars%s)", session, len(frame),
                    ", stale" if frame.attrs["stale"] else "")
        return frame
    except Exception as exc:  # noqa: BLE001 - a failed read is no baseline, never a failed scan
        logger.warning("SPY baseline: the prices store could not be read (%s)", exc)
        empty = pd.DataFrame()
        empty.attrs["stale"] = True
        return empty


def _ohlcv(bars: pd.DataFrame) -> pd.DataFrame:
    """Daily bars as a date-indexed Open/High/Low/Close/Volume frame, marked ``attrs["source"] = "prices"``."""
    if bars is None or bars.empty:
        return pd.DataFrame()
    frame = pd.DataFrame({"Open": bars["open"].to_numpy(), "High": bars["high"].to_numpy(),
                          "Low": bars["low"].to_numpy(), "Close": bars["close"].to_numpy(),
                          "Volume": bars["volume"].to_numpy()},
                         index=pd.DatetimeIndex(bars["session_date"]))
    frame = frame.apply(pd.to_numeric, errors="coerce").dropna(subset=["Close"])
    frame.attrs["source"] = "prices"
    return frame


BAR_DAYS = SPY_BASELINE_DAYS     # a scored name's window: the old read's two years, as for SPY
BARS_CHUNK = 200                 # names per D01 read in a scan (S3, #434 review): one bars and one actions query each

_PRELOADED = threading.local()   # the scan's preloaded bars of the name a pool thread is scoring (``_score``)


def _split_bars(frame: pd.DataFrame, ticker: str, session: date) -> pd.DataFrame:
    """One ticker's rows of a ``daily_bars`` frame in the ``load_ohlc`` shape, ``attrs["stale"]`` set."""
    one = _ohlcv(frame[frame["ticker"] == ticker]) if not frame.empty else pd.DataFrame()
    if not one.empty:
        one.attrs["stale"] = bool(one.index[-1].date() < session)
    return one


def load_bars_many(tickers, now=None) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    """``{ticker: (nominal, adjusted)}`` for many names from one read of the prices store (S3, #434 review; PR 10),
    each pair equal to ``load_bars(ticker)``. A name the store has no bar of in the window gets two empty frames.
    "Nominal" is the boards' split basis and "adjusted" the total-return basis (``core.market_bars``)."""
    from RevenueChaser.core import market_bars
    names = list(dict.fromkeys(str(t).strip().upper() for t in tickers if t))
    session = calendar.latest_complete_session(now)
    both = market_bars.daily_bars_bases(names, start=session - timedelta(days=BAR_DAYS))
    return {t: (_split_bars(both["nominal"], t, session), _split_bars(both["adjusted"], t, session))
            for t in names}


def load_bars(ticker: str, now=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One scored name's D01 bars (#383 R3 C12, D3): ``(nominal, adjusted)`` frames in the ``load_ohlc`` shape.

    Both cover the last ``BAR_DAYS`` before the latest complete session (``prices.daily_bars`` windowed read, K8a).
    The nominal frame gives the levels and the adjusted one the returns (``compute_indicators(returns_df=)``).
    ``nominal.attrs["stale"]`` is true when the last bar is before that session. Store only: no ``swing_ohlc`` read
    or write, no yfinance call. An empty nominal frame means D01 holds no bar of the name in the window. Inside a
    scan the pair comes from the chunk ``run_screen`` read for this name (``load_bars_many``), with no store read.
    """
    pre = getattr(_PRELOADED, "bars", None)
    if pre is not None and pre[0] == str(ticker).strip().upper():
        return pre[1]
    return load_bars_many([ticker], now).get(str(ticker).strip().upper(), (pd.DataFrame(), pd.DataFrame()))


STALE_DROP_MAX_SHARE = 0.05      # at most this share of the scored names drops as ``stale_bars`` (S1, #434 review)


def stale_names(tickers, reference: date | None) -> set[str]:
    """The names whose newest D01 bar is before ``reference`` (SPY's newest session): one store read.

    These drop as ``stale_bars`` only while they are a few (``STALE_DROP_MAX_SHARE`` of ``tickers``): a halt or a
    Daily Bars ``failed_both`` name, which would otherwise become a pick that refuses the whole board's freeze. More
    than that is a Daily Bars failure, not a few gaps: the names are scored as before (marked stale) and the freeze
    refuses the day, so a failed nightly job never empties the board. ``set()`` with no reference.
    """
    if reference is None:
        return set()
    names = [str(t).strip().upper() for t in tickers if t]
    last = ledger.prices_last_bars(names)
    stale = {t for t in names if t in last and last[t][0] < reference.isoformat()}
    if len(stale) > STALE_DROP_MAX_SHARE * len(names):
        logger.warning("%d of %d names have no D01 bar of %s: a Daily Bars failure, not a few gaps; "
                       "they are scored as stale and none drops", len(stale), len(names), reference)
        return set()
    return stale


def bars_reference_session() -> date | None:
    """SPY's newest complete D01 session: the session every scored name must have a bar of (S1, #434 review).

    ``None`` when D01 holds no SPY bar or cannot be read: then no name drops as ``stale_bars`` (the freeze's SPY
    anchor decides that day).
    """
    last = ledger.prices_last_bars(["SPY"]).get("SPY")
    if last is None:
        return None
    try:
        return date.fromisoformat(last[0])
    except (TypeError, ValueError):
        return None


def _score(compute_row: Callable[..., dict | None], ticker: str, bars, *args):
    """``compute_row(*args)`` on a pool thread with ``ticker``'s preloaded ``bars`` served to ``load_bars``."""
    _PRELOADED.bars = (ticker, bars) if bars is not None else None
    try:
        return compute_row(*args)
    finally:
        _PRELOADED.bars = None


def _generated(payload: dict) -> float:
    """``payload["generated_at"]`` as epoch seconds; a missing or unreadable stamp is the oldest."""
    try:
        return datetime.fromisoformat(str(payload.get("generated_at"))).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def stored_payload(*, board: str, refreshed_by: str, build_payload: Callable[..., dict],
                   load_universe: Callable[..., tuple[list[dict], dict]], history_board: str | None = None,
                   saved: Path | None = None) -> dict:
    """What a Refresh shows (K4, #383 R1): the board the last Fetch built, never a re-score.

    Two stores can hold it: the last history header's ``payload`` (``core/history.last_run``; the Fetch records it,
    ``history.record``) and the board's saved JSON (``saved``, written by every render). The newer of the two by
    ``generated_at`` wins, so a Fetch whose history write failed is still what the board shows, and a Refresh never
    overwrites it with an older header. A saved board that is empty or the demo fixture never counts. A header from
    before K4 has no payload: the saved JSON alone. With neither, the board's no-data message: no stored universe,
    or a universe with no stored scan. No ``compute_row``, no pool, no network, no write.
    """
    recorded = None
    if history_board:
        from RevenueChaser.core import history
        try:
            last = history.last_run(history_board, payload=True)
        except Exception as exc:  # noqa: BLE001 - a locked or unreadable history file falls back below
            logger.warning("%s: the last Fetch's header could not be read: %s", board, exc)
            last = None
        if last and isinstance(last.get("payload"), dict):
            recorded = last["payload"]
    shown = None
    if saved is not None:
        try:
            shown = json.loads(Path(saved).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            shown = None
        if not isinstance(shown, dict) or shown.get("no_data") or shown.get("is_fixture"):
            shown = None
    if recorded is not None and (shown is None or _generated(recorded) >= _generated(shown)):
        return recorded
    if shown is not None:
        if recorded is not None:
            logger.warning("%s: the saved board is newer than the last recorded Fetch (a history write failed?); "
                           "showing the saved board", board)
        return shown
    rows, _ = load_universe(refreshed_by=refreshed_by, refresh=False, no_fetch=True)
    reason = "no_scan_stored" if rows else "no_universe_stored"
    logger.info("%s: Refresh has no stored scan to show (%s)", board, reason)
    return no_data_payload(build_payload([], run_stats={}), reason)


# The funnel's typed drops after the excluded types (C16, #383 R3), in the order a name can leave: the prefilter's on
# the prices store's close and volume (PR 10), then the scorer's on the daily bars and EDGAR. ``(key, label, by design)``: a by-design drop is a board rule; the rest are
# data gaps that need a look. A scorer reason not listed here still gets its own step (``DROP_LABELS``' text).
def _drop_steps(cfg: Any, floors: Any = None) -> tuple[tuple[str, str, bool], ...]:
    """``floors`` (the thresholds the prefilter used, Swing review 1) names the price and volume floors; ``cfg`` (the
    board's config) the rest. Fundamentals' sector and stale-annual gates are board rules (review N3); its Rule of 40
    barrier stays a data gap, because the code drops an implausible figure as a data problem, not as a pick rule."""
    floors = floors or cfg
    sectors = ", ".join(getattr(cfg, "EXCLUDED_SECTORS", None) or ())
    barrier = getattr(cfg, "RO40_BARRIER", None)
    return (
        ("no_quote", "No current close in the prices store to prefilter on", False),
        ("low_price", f"Price under ${floors.MIN_PRICE:,.2f}", True),
        ("low_dollar_volume", f"Average daily dollar volume under ${floors.MIN_DOLLAR_VOLUME:,.0f}", True),
        ("no_bars", "No stored daily price bars (D01)", False),
        ("stale_bars", "No D01 bar of the latest stored session (a halt or a Daily Bars gap)", False),
        ("short_bars", "Fewer than 252 stored daily bars", True),
        ("no_price", "No usable price", False),
        ("sector_excluded", f"Excluded sector ({sectors})" if sectors else "Excluded sector", True),
        ("edgar_fetch_failed", "EDGAR request failed", False),
        ("no_edgar_annual", "No EDGAR annual data", False),
        ("stale_annual", f"Latest annual report ended more than {fiscal_year.STALE_DAYS} days ago", True),
        ("ro40_barrier", (f"Rule of 40 above {barrier:g}: an implausible figure, check the filing" if barrier
                          else "Implausible Rule of 40: check the filing"), False),
        ("compute_error", "Scoring errors (see the log)", False),
    )


def funnel_steps(*, universe: int, excluded_types: int, prefiltered: Prefiltered, dropped: dict[str, list[str]],
                 scored: int, passed: int, shown: int, cfg: Any,
                 screen_drops: list[tuple[str, str, list[str]]] | None = None) -> list[dict]:
    """The board's funnel (``core.funnel`` format; C16, #383 R3): the stored universe, every typed reason a name has
    no card, then the candidates shown.

    ``prefiltered`` carries the prefilter's drops and ``dropped`` the scorer's (reason -> tickers). ``scored`` names
    got a score row, ``passed`` of them pass at least one of the board's three screens, and ``shown`` of those are on
    the board (fewer only under an explicit ``top_n``). Each drop lists at most ``funnel.MAX_TICKERS`` names, sorted.
    Whatever no reason accounts for is an ``unrecorded`` drop, so the steps add up; a reason with no names has no
    step. ``screen_drops`` (``(key, label, tickers)``, from the board's ``run_stats``) names scored names that a board
    rule other than its three screens kept off the board (Swing's direction rule, #384): each is its own by-design
    step, taken out of "passed none of the board's three screens".
    """
    step = funnel.step
    steps = [step("universe", "Universe, all stored names", universe, "start")]
    accounted = excluded_types
    if excluded_types:
        steps.append(step("excluded_types", "Excluded security types (ETF, CEF, SPAC, preferred, unit, warrant)",
                          excluded_types, "drop", by_design=True))
    by_reason = {**prefiltered.drops, **dropped}
    known = _drop_steps(cfg, getattr(prefiltered, "cfg", None))
    listed = {key for key, _, _ in known}
    extra = [(k, DROP_LABELS.get(k, k).capitalize(), False) for k in sorted(by_reason) if k not in listed]
    for key, label, by_design in (*known, *extra):
        names = sorted(t for t in by_reason.get(key, []) if t)
        if names:
            accounted += len(names)
            steps.append(step(key, label, len(names), "drop", by_design=by_design,
                              tickers=names[:funnel.MAX_TICKERS]))
    rest = universe - accounted - scored
    if rest > 0:
        steps.append(step("unrecorded", "Not scored, reason not recorded", rest, "drop", by_design=False))
    rest_scored = scored - passed
    for key, label, names in screen_drops or []:
        names = sorted(t for t in names if t)
        if names and len(names) <= rest_scored:
            rest_scored -= len(names)
            steps.append(step(key, label, len(names), "drop", by_design=True, tickers=names[:funnel.MAX_TICKERS]))
    if rest_scored > 0:
        steps.append(step("passed_no_screen", "Scored, passed none of the board's three screens", rest_scored,
                          "drop", by_design=True))
    if passed > shown:
        steps.append(step("beyond_top_n", "Beyond the requested top N", passed - shown, "drop", by_design=True))
    steps.append(step("candidates", "Candidates (a card on the board)", shown, "result"))
    return steps


def run_screen(*, board: str, refreshed_by: str, cfg: Any,
               load_universe: Callable[..., tuple[list[dict], dict]],
               prefilter: Callable[[list[dict]], Prefiltered],
               load_spy: Callable[[], Any],
               compute_row: Callable[..., dict | None],
               build_payload: Callable[..., dict],
               persist: Callable[[list[dict]], Any],
               extra_stats: Callable[[], dict] | None = None,
               top_n: int | None = None, refresh_universe: bool = False, no_fetch: bool = False,
               history_board: str | None = None, saved: Path | None = None) -> dict:
    """End-to-end scan for one board; returns its JSON payload.

    ``no_fetch=True`` (a Refresh, or the plain-load self-heal) scores nothing: it returns ``stored_payload`` (the last
    Fetch's recorded payload, ``history_board``; else the board's ``saved`` JSON), so it never loads the universe
    from the network, never fetches a bar or a filing, and persists nothing (#145, K4). ``compute_row(row, spy_df, no_fetch, spine, drops)``
    returns a row or ``None`` and appends the reason for each ``None`` to ``drops`` (#205; one list per row, so the
    funnel knows each reason's tickers). ``extra_stats`` adds board-specific run stats after the scan.
    ``persist(results)`` runs only on a Fetch with at least one result. The payload's ``funnel_steps`` (C16) says where
    every stored name went; ``build_payload`` reports ``run_stats["passed_screens"]``, the names that pass at least
    one screen before any ``top_n`` cut.
    """
    if no_fetch:
        return stored_payload(board=board, refreshed_by=refreshed_by, build_payload=build_payload,
                              load_universe=load_universe, history_board=history_board, saved=saved)
    rows, u_stats = load_universe(refreshed_by=refreshed_by, refresh=refresh_universe, no_fetch=no_fetch)
    if not rows:
        # A Refresh never loads the universe (#145 D1): an empty store is its own reason.
        reason = "no_universe_stored" if no_fetch else "no_universe"
        logger.warning("%s: no universe (%s); reporting no data", board, reason)
        return no_data_payload(build_payload([], run_stats={}), reason)

    # R5 D-2 (#88): the identity master's excluded types (CEF, ETF/ETP, SPAC, preferred, unit, warrant) never screen.
    excluded = _universe.excluded_tickers()
    n_excluded = sum(1 for r in rows if r.get("ticker") in excluded)
    all_rows = rows
    rows = [r for r in rows if r.get("ticker") not in excluded]
    spine = _universe.spine_gics()
    stored_earnings = earnings_dates.stored_next_dates()    # #304: one read per run; Yahoo's date where the store has none

    pf = prefilter(rows)
    filtered = pf.kept                   # every kept name is scored: no shortlist cap (C12, card C)
    earnings_stored = sum(1 for r in filtered if str(r.get("ticker") or "").strip().upper() in stored_earnings)
    logger.info("%s: %d of %d names score the stored earnings date (%d stored in all), the rest keep Yahoo's",
                board, earnings_stored, len(filtered), len(stored_earnings))

    spy_df = load_spy()
    spy = spy_summary(spy_df)

    results: list[dict] = []
    data_short = 0
    drops: list[str] = []
    dropped: dict[str, list[str]] = {}      # reason -> tickers, for the funnel (C16)
    stale = stale_names([r.get("ticker") for r in filtered], bars_reference_session())   # S1 (#434 review)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        # S3 (#434 review): D01 is read once per chunk of names, both bases from that read, not per name.
        for i in range(0, len(filtered), BARS_CHUNK):
            chunk = filtered[i:i + BARS_CHUNK]
            if job_control.stop_requested():
                job_control.stop_pool([])
            try:
                preloaded = load_bars_many([r.get("ticker") for r in chunk])
            except Exception as exc:  # noqa: BLE001 - each name then reads its own bars (``load_bars``)
                logger.warning("%s: the D01 bars of %d names could not be read in one go (%s); reading per name",
                               board, len(chunk), exc)
                preloaded = {}
            futs = {}
            for r in chunk:
                mine: list[str] = []        # this row's own reasons, so each one is known by ticker
                t = str(r.get("ticker") or "").strip().upper()
                if t in stale:
                    # No bar of SPY's session (a halt, a Daily Bars ``failed_both`` name): never a pick, so it
                    # can never refuse the board's freeze (S1).
                    mine.append("stale_bars")
                    drops.extend(mine)
                    data_short += 1
                    dropped.setdefault("stale_bars", []).append(r.get("ticker"))
                    continue
                fut = ex.submit(_score, compute_row, t, preloaded.get(t), earnings_dates.with_stored_date(r, stored_earnings),
                                spy_df, no_fetch, spine, mine)
                futs[fut] = (r, mine)
            for f in as_completed(futs):
                if job_control.stop_requested():  # a cancel or a passed deadline (#243): no more tickers, no persist
                    job_control.stop_pool(futs)
                r, mine = futs[f]
                try:
                    res = f.result()
                except Exception as exc:  # noqa: BLE001 - one ticker never aborts the scan (#147 U5)
                    logger.warning("%s: %s could not be scored: %s", board, r.get("ticker"), exc)
                    mine.append("compute_error")
                    res = None
                drops.extend(mine)
                if res:
                    results.append(res)
                else:
                    data_short += 1
                    if mine:                # the last reason is why it left; none is the funnel's "unrecorded"
                        dropped.setdefault(mine[-1], []).append(r.get("ticker"))

    no_edgar = drops.count("no_edgar_annual")  # a name EDGAR has nothing on is not a short price history (#148 U19)
    data_short -= no_edgar
    run_stats = {
        "regime": spy["regime"],
        "spy_price": spy["spy_price"],
        "spy_sma200": spy["spy_sma200"],
        "spy_3m_return": spy["spy_3m_return"],
        "raw_universe": len(all_rows),
        "excluded_types": n_excluded,
        "no_spine_gics": sum(1 for r in filtered
                               if _universe.gics_of(r.get("ticker"), spine).sector == UNCLASSIFIED),
        "filtered_universe": len(filtered),
        "out_of_universe": u_stats.get("out_of_universe", 0),
        "hygiene": pf.dropped,
        "data_short": data_short,
        "no_edgar": no_edgar,
        "earnings_stored": earnings_stored,
        "stale_ohlc": sum(1 for r in results if r.get("ohlc_stale")),
        "spy_stale": bool(getattr(spy_df, "attrs", {}).get("stale")),
        "dropped": count_drops(drops),
        "universe_generated_at": u_stats.get("universe_generated_at"),
        "universe_refreshed_by": u_stats.get("universe_refreshed_by"),
        "universe_partial": bool(u_stats.get("partial")),
    }
    # K7 (#383 R3): the payload's data date is the newest bar over the names this Fetch scored (the prices store, PR 10).
    from RevenueChaser.core import provenance
    run_stats["data_as_of"] = provenance.prices_day(r.get("ticker") for r in results)
    if extra_stats is not None:
        run_stats.update(extra_stats())
    payload = build_payload(results, top_n=top_n, run_stats=run_stats)
    shown = len(payload.get("candidates") or [])
    # U9: the session of the closes the prefilter read, and how many names had only an older one.
    # S4 (#434 review): the kept names the dollar-volume rule could not test (no average volume).
    payload.setdefault("data_quality", {}).update(
        quote_session=pf.quote_session.isoformat() if pf.quote_session else None, older_quotes=pf.older_quotes,
        no_avg_volume=len(pf.no_volume))
    payload["funnel_steps"] = funnel_steps(
        universe=len(all_rows), excluded_types=n_excluded, prefiltered=pf, dropped=dropped, scored=len(results),
        passed=max(int(run_stats.get("passed_screens") or 0), shown), shown=shown, cfg=cfg,
        screen_drops=run_stats.get("screen_drops"))
    if not results:
        # Never the demo fixture for a real run (#205): say why nothing scored.
        logger.warning("%s: no ticker could be scored (%s)", board, run_stats["dropped"])
        return no_data_payload(payload, "no_scorable_tickers", run_stats["dropped"], len(filtered))
    if not no_fetch:
        # Only a Fetch writes the durable score tables; a Refresh renders and leaves them alone (#145 D5).
        persist(results)
    return payload
