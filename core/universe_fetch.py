"""The shared equity universe, write side: the SEC member list, the Yahoo screener fetch and the writers of ``equity_universe``.

Moved here from ``module_asymmetric_growth/engine.py`` (#146, folded from #217): the universe is shared by Asymmetric
Growth, Swing, Fundamentals and the Settings jobs, so none of them should own its writer. ``core/universe.py`` stays the
read side. The move changed no behavior; the original comments are kept as written.
"""
from __future__ import annotations

import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone

from RevenueChaser.core import job_lock, rawstore
from RevenueChaser.core import quotes as quote_store
from RevenueChaser.core.config import to_float
from RevenueChaser.core import universe as _spine
from RevenueChaser.core.universe import (
    load_cached_universe as _load_cached_universe,
    load_universe_meta as _load_universe_meta,
)
from RevenueChaser.core.gics_map import MORNINGSTAR_SECTORS, UNCLASSIFIED
from RevenueChaser.core.identity.sources import norm_ticker

logger = logging.getLogger(__name__)

UNIVERSE_MIN_MC = 0
UNIVERSE_MAX_MC = 10_000_000_000_000  # geniş evren (tüm ABD hisseleri)
# SEC exchange names (``company_tickers_exchange.json``) whose rows are the universe (#264): OTC and rows
# with no exchange are out. Compared case-insensitively.
SEC_LIST_EXCHANGES = frozenset({"nasdaq", "nyse", "cboe"})
US_EXCHANGES = {"NMS", "NYQ", "NCM", "ASE", "NGM", "NYSEMKT", "BATS", "BTS", "PCX", "CBOE"}


def _screener_query(lo: float = UNIVERSE_MIN_MC, hi: float = UNIVERSE_MAX_MC):
    from yfinance.screener import EquityQuery as EQ
    return EQ("and", [
        EQ("btwn", ["intradaymarketcap", lo, hi]),
        EQ("eq", ["region", "us"]),
    ])


# Market-cap dilimleri — Yahoo screener ~10K sonuçta sayfalamayı kesiyor; geniş evreni
# bu dilimler halinde çekip birleştiriyoruz (her dilim <10K, böylece kimse düşmez).
MC_CHUNKS = [
    (0, 50_000_000),
    (50_000_000, 100_000_000),
    (100_000_000, 300_000_000),
    (300_000_000, 2_000_000_000),
    (2_000_000_000, 10_000_000_000),
    (10_000_000_000, 200_000_000_000),
    (200_000_000_000, 10_000_000_000_000),
]



# Floor on the SEC member list (#264): a file that parses but lists fewer Nasdaq / NYSE / CBOE rows than this is a
# failed pull, because SEC defines membership and a short list would replace the stored universe with those few rows.
# It is no longer the test for a partial Yahoo pull (that was the absolute check that let a 3,000-name pull delete
# every other stored name); see ``FULL_PULL_MIN_SHARE``.
MIN_FULL_UNIVERSE = 3000

# D6 (#146): a pull replaces the stored universe only when every market-cap chunk ran to the total the screener
# reported AND the screener returned at least this share of the stored names that carry a Yahoo quote. Anything else
# is *partial*: it upserts what it got and deletes nothing.
FULL_PULL_MIN_SHARE = 0.9


_UNIVERSE_META_KEY = _spine.UNIVERSE_META_KEY
_UNIVERSE_STATS_KEY = "__stats__"
# Outcome of the last fetch: ``refreshed_by`` holds "full" or "partial", ``generated_at`` when it ran. Boards read it
# to say "partial fetch"; a full pull writes "full" and clears the flag.
_UNIVERSE_FETCH_KEY = "__fetch__"
_LOCK_NAME = "universe"
_RUN_LOCK_NAME = "universe_run"   # held, beside _LOCK_NAME, only by the universe refresh run (#244)
# Politeness pause before each ticker in Phase B (tests patch ``time.sleep``).
# The live run (2026-09-24) tripped Yahoo after ~100 names at 4 workers / 0.05s;
# this is deliberately gentler.
_CAP_BACKFILL_SLEEP = 0.3
_CAP_BACKFILL_WORKERS = 2
# Per-ticker retry policy for Yahoo 429s. ``_CAP_BACKFILL_BACKOFF[i]`` is the
# sleep before retry i+1; after ``_CAP_BACKFILL_ATTEMPTS`` the ticker is marked
# rate-limited and left NULL.
_CAP_BACKFILL_ATTEMPTS = 3
_CAP_BACKFILL_BACKOFF = (5.0, 20.0)
# Consecutive tickers that exhaust their rate-limit retries ⇒ Yahoo is refusing
# us; stop the whole run. Hammering makes the block longer, so the rest stay
# NULL and the next (resumable) run picks them up.
_RATE_LIMIT_STREAK_STOP = 5


def _flatten_universe(rows: list[dict]) -> list[dict]:
    """The typed ``equity_universe`` rows for ``rows`` (see rawstore MANIFEST.equity_universe).

    Phase A carry-forward: the Yahoo screener quote routinely omits ``marketCap``
    (and sometimes price), which used to wipe a previously-good value and force
    Phase B to redo that name. For a row whose new value is None, reuse the stored
    non-null value for the same ticker — only a genuinely new value overwrites.

    The classification is the identity master's (``core.identity.apply``, UW's Morningstar sector and
    industry names): the stored ``sector`` and ``industry`` are carried forward unchanged, and so is
    ``next_earnings_date``, so a rebuild never mixes in a second vocabulary or blanks a column it does not own.
    A ticker with nothing stored takes the row's own sector only when it is one of the 11 names (else
    ``Unclassified``) and the row's own industry text. ``industry_group`` / ``sub_industry`` are GICS-only levels
    nothing fills any more: NULL.
    """
    stored = {r["ticker"]: r for r in rawstore.query(
        "SELECT ticker, market_cap, price, sector, industry, next_earnings_date "
        "FROM equity_universe")} if rows else {}
    flat = []
    for r in rows:
        t = (r.get("ticker") or "").strip().upper()
        if not t:
            continue
        quote = r.get("quote") or {}
        prev = stored.get(t) or {}
        vendor_sector = r.get("sector") if r.get("sector") in MORNINGSTAR_SECTORS else None
        sector = prev.get("sector") or vendor_sector or UNCLASSIFIED
        industry = prev.get("industry") or r.get("industry") or quote.get("industry") or ""
        cap = to_float(r.get("market_cap"))
        if cap is None:
            cap = to_float(prev.get("market_cap"))
        price = to_float(r.get("price"))
        if price is None:
            price = to_float(prev.get("price"))
        flat.append({
            "ticker": t,
            "market_cap": cap,
            "sector": sector,
            "price": price,
            "name": r.get("name") or "",
            "industry": industry,
            "industry_group": None,
            "sub_industry": None,
            "next_earnings_date": prev.get("next_earnings_date"),
            "quote": r.get("quote"),
        })
    return flat


def _save_universe(rows: list[dict], replace: bool = True) -> None:
    """Write the typed universe rows, carrying stored cap / price / sector forward (``_flatten_universe``).

    ``replace=True`` (a full pull) swaps the whole table in one transaction. ``replace=False`` (a partial pull,
    D6) upserts: the rows given overwrite their stored namesakes and every other stored name stays.
    """
    flat = _flatten_universe(rows)
    if flat:
        if replace:
            rawstore.replace_all("equity_universe", flat)
        else:
            rawstore.write_rows("equity_universe", flat)


def _apply_identity_master() -> dict:
    """Re-apply ``security_master`` to the universe right after ``_save_universe`` rewrote every row (R3, #86; #265).

    ``_save_universe`` carries the stored sector and industry forward; the master is the single classification
    writer, so its sector and industry (and UW's earnings date) go back on top. Store-only (no network); a
    ticker the master does not know keeps the carried values; an empty master writes nothing.
    """
    from RevenueChaser.core.identity import apply as identity_apply
    return identity_apply.apply_to_universe()


def _save_universe_meta(refreshed_by: str | None, generated_at: str) -> None:
    """Membership provenance (``__meta__``). ``write_rows`` refreshes only this key's row; the old ``replace_all``
    deleted the ``__stats__`` and ``__fetch__`` rows with it (F10)."""
    rawstore.write_rows("equity_universe_provenance", [{
        "key": _UNIVERSE_META_KEY,
        "generated_at": generated_at or "",
        "refreshed_by": refreshed_by or "",
    }])


def _save_fetch_state(partial: bool, generated_at: str) -> None:
    """Record whether the last universe pull was full or partial (a third provenance key, no schema change)."""
    rawstore.write_rows("equity_universe_provenance", [{
        "key": _UNIVERSE_FETCH_KEY,
        "generated_at": generated_at or "",
        "refreshed_by": "partial" if partial else "full",
    }])


def load_fetch_state() -> dict:
    """``{"partial": bool, "generated_at": str}`` of the last pull, or ``{}`` when none is recorded yet."""
    row = rawstore.get("equity_universe_provenance", _UNIVERSE_FETCH_KEY)
    if not row:
        return {}
    return {"partial": (row.get("refreshed_by") or "") == "partial", "generated_at": row.get("generated_at") or ""}


_lock_state = threading.local()


@contextmanager
def universe_write_lock():
    """Serialise the universe writers: fetch + write + identity apply + provenance, and Phase B.

    Yields ``None`` when this thread holds the lock, or the PID of the run that holds it (``0`` when unknown), in
    which case the caller must make no network call and write nothing. Re-entrant per thread, so a fetch can call
    Phase B and #243 can take it again at job level. ``job_lock`` is a file lock: it already excludes other
    threads of this process and other processes, but it is not re-entrant. Released on any exit.
    """
    depth = getattr(_lock_state, "depth", 0)
    if depth:
        _lock_state.depth = depth + 1
        try:
            yield None
        finally:
            _lock_state.depth -= 1
        return
    with job_lock.acquire(_LOCK_NAME, wait=0) as got:
        if not got:
            yield dict(job_lock.holders()).get(_LOCK_NAME, 0)
            return
        _lock_state.depth = 1
        try:
            yield None
        finally:
            _lock_state.depth = 0


@contextmanager
def universe_run_lock():
    """The universe refresh run's lock (#244): the writer lock for the whole run, plus the ``universe_run`` marker.

    Yields ``None`` when this thread holds both, else the holder's PID (``0`` when unknown); the caller then makes no
    network call and writes nothing. Phase B ("Fetch caps") holds only the writer lock, so the marker is how a board
    Fetch tells a universe run from it (``universe_run_holder``). Both are released on any exit.
    """
    with universe_write_lock() as holder:
        if holder is not None:
            yield holder
            return
        with job_lock.acquire(_RUN_LOCK_NAME, wait=0) as got:
            # The writer lock is exclusive, so the marker is free unless a holder crashed between the two.
            yield None if got else (universe_run_holder() or 0)


def universe_run_holder() -> int | None:
    """PID of the live process running a universe refresh, ``None`` when none does (``0``: it runs, PID unreadable).

    The single-board Fetches use it (#244): they are refused while a universe run is going, but not while Phase B
    holds only the writer lock.
    """
    return dict(job_lock.holders()).get(_RUN_LOCK_NAME)


_BOARD_FETCH_PREFIX = "board_fetch_"


@contextmanager
def board_fetch_lock(board: str):
    """A single-board Fetch's marker (#356): ``board_fetch_<board>``, held while that Fetch runs.

    Yields ``None`` when the Fetch may run. Otherwise it must make no network call and write nothing, and the yield
    says why: ``("universe", pid)`` while a universe run is going, ``("board", pid)`` while another Fetch of this
    board holds the marker (``pid`` is ``0`` when unknown). The marker is taken first and the universe run checked
    second; ``run_universe_refresh`` does the same the other way round, so of two that start together at least one
    sees the other. Released on any exit.
    """
    name = _BOARD_FETCH_PREFIX + board
    with job_lock.acquire(name, wait=0) as got:
        if not got:
            yield ("board", dict(job_lock.holders()).get(name, 0))
            return
        pid = universe_run_holder()
        yield None if pid is None else ("universe", pid)


def board_fetch_holders() -> list[tuple[str, int]]:
    """``(board, pid)`` of every single-board Fetch running now (#356). Checks; never takes a lock."""
    return sorted((name[len(_BOARD_FETCH_PREFIX):], pid) for name, pid in job_lock.holders()
                  if name.startswith(_BOARD_FETCH_PREFIX))


def _save_stats_meta(refreshed_by: str | None, generated_at: str) -> None:
    """Provenance for the Phase B stats pass — a SECOND key, not an overwrite.

    ``equity_universe_provenance`` is ``write: "replace"`` keyed on ``key``, so
    ``write_rows`` refreshes only the matching ``__stats__`` group and leaves
    membership's ``__meta__`` row intact (docs/data_health.md, "Feasibility
    review" §4). ``replace_all`` would wipe the whole table and lose ``__meta__``.
    """
    rawstore.write_rows("equity_universe_provenance", [{
        "key": _UNIVERSE_STATS_KEY,
        "generated_at": generated_at or "",
        "refreshed_by": refreshed_by or "",
    }])


def _load_stats_meta() -> dict:
    row = rawstore.get("equity_universe_provenance", _UNIVERSE_STATS_KEY)
    if not row:
        return {}
    return {"generated_at": row.get("generated_at") or "",
            "refreshed_by": row.get("refreshed_by") or ""}


def backfill_universe_caps(tickers: list[str] | None = None, chunk: int = 200,
                           refreshed_by: str | None = None,
                           limit: int | None = None) -> dict:
    """Phase B under the universe lock (see ``_backfill_caps`` for what it does).

    When another run holds the lock this makes no Yahoo call and writes nothing: it returns zero counts plus
    ``"locked": <holder pid>``. The lock covers the whole pass because a concurrent upsert could otherwise
    overwrite a cap the pass just filled.
    """
    with universe_write_lock() as holder:
        if holder is not None:
            logger.warning("cap backfill skipped: pid %s holds the universe lock", holder)
            return {"requested": 0, "filled": 0, "skipped": 0, "failed": 0, "deferred": 0,
                    "rate_limited": False, "locked": holder}
        return _backfill_caps(tickers, chunk, refreshed_by, limit)


def _backfill_caps(tickers: list[str] | None = None, chunk: int = 200,
                   refreshed_by: str | None = None,
                   limit: int | None = None) -> dict:
    """Phase B: fill ``market_cap`` for rows still NULL after Phase A.

    Work list is ``equity_universe`` rows with ``market_cap IS NULL`` (optionally
    intersected with ``tickers``, and capped to the first ``limit`` rows when
    given). For each, read ``fast_info.shares`` and
    ``fast_info.last_price`` — never ``Ticker.info`` and never
    ``fast_info.market_cap`` (which falls back to ``.info`` in yfinance 1.5.2).
    A missing/NaN/``<=0`` price, or a missing share count, leaves the cap NULL
    (counted ``skipped``). Persist only those columns via the UPDATE-only helper,
    so no other row or field is ever touched. Each filled cap is also this session's
    ``yfinance`` row in ``quote_snapshot`` (#294), written with the same chunk; a stop
    on the rate limit is stamped in ``source_checks`` for Data Health.

    Yahoo rate limits (``YFRateLimitError``) are retried per ticker with the
    ``_CAP_BACKFILL_BACKOFF`` sleeps; once ``_RATE_LIMIT_STREAK_STOP`` consecutive
    tickers exhaust their retries the run **stops early** rather than grinding
    through the list marking everything ``failed``. Names not resolved stay NULL
    and the work list is null-only, so a later run is free and resumable.

    At ``_CAP_BACKFILL_SLEEP`` with ``_CAP_BACKFILL_WORKERS`` workers a full
    ~1.7k-ticker pass takes ~10-20 minutes. Returns
    ``{"requested", "filled", "skipped", "failed", "deferred", "rate_limited"}``
    (``deferred`` = rows left NULL, ``rate_limited`` = stopped early) and always
    writes the ``__stats__`` provenance row when it runs.
    """
    import time
    import yfinance as yf

    try:
        from yfinance.exceptions import YFRateLimitError as _YFRateLimitError
    except Exception:  # pragma: no cover - import path differs by version
        _YFRateLimitError = None

    def _is_rate_limit(exc: BaseException) -> bool:
        if _YFRateLimitError is not None and isinstance(exc, _YFRateLimitError):
            return True
        return any("RateLimit" in cls.__name__ for cls in type(exc).__mro__)

    if tickers is None:
        work = rawstore.query(
            "SELECT ticker FROM equity_universe WHERE market_cap IS NULL")
    elif not tickers:
        work = []
    else:
        marks = ",".join(["?"] * len(tickers))
        work = rawstore.query(
            f"SELECT ticker FROM equity_universe WHERE market_cap IS NULL "
            f"AND ticker IN ({marks})", list(tickers))
    work_tickers = [r["ticker"] for r in work]
    if limit is not None:
        work_tickers = work_tickers[:limit]
    requested = len(work_tickers)
    filled = skipped = failed = 0

    def _read(t: str):
        if _CAP_BACKFILL_SLEEP:
            time.sleep(_CAP_BACKFILL_SLEEP)
        for attempt in range(_CAP_BACKFILL_ATTEMPTS):
            try:
                fi = yf.Ticker(t).fast_info
                shares = getattr(fi, "shares", None)
                price = getattr(fi, "last_price", None)
                return "ok", t, shares, price
            except Exception as exc:  # noqa: BLE001 - per-ticker retry policy
                if not _is_rate_limit(exc):
                    logger.debug("cap backfill failed for %s: %s", t, exc)
                    return "failed", t, None, None
                if attempt < _CAP_BACKFILL_ATTEMPTS - 1:
                    time.sleep(_CAP_BACKFILL_BACKOFF[attempt])
                    continue
                logger.debug("cap backfill rate-limited for %s", t)
                return "rate_limited", t, None, None
        return "rate_limited", t, None, None  # pragma: no cover - loop is exhaustive

    updates: list[dict] = []
    fills: list[dict] = []            # the same values, for this session's Yahoo rows in quote_snapshot (#294)
    quotes_started = quote_store.snapshot_session()   # a fill landing in another session writes no quote row
    rate_limited_streak = 0
    stopped_early = False

    def _flush() -> None:
        rawstore.update_columns("equity_universe", "ticker", updates, ["market_cap", "price"])
        updates.clear()
        quote_store.record_fast_info(fills, started=quotes_started)
        fills.clear()

    def _record(t: str, shares, price) -> None:
        nonlocal filled, skipped
        if shares is None or price is None:
            skipped += 1
            return
        try:
            shares_f, price_f = float(shares), float(price)
        except (TypeError, ValueError):
            skipped += 1
            return
        if not math.isfinite(shares_f) or not math.isfinite(price_f) or price_f <= 0:
            skipped += 1
            return
        updates.append({"ticker": t, "market_cap": shares_f * price_f, "price": price_f})
        fills.append({"ticker": t, "shares": shares_f, "price": price_f})
        filled += 1
        if len(updates) >= chunk:
            _flush()

    if work_tickers:
        workers = min(_CAP_BACKFILL_WORKERS, len(work_tickers))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            pending: dict = {}
            next_i = 0

            def _submit_next() -> None:
                nonlocal next_i
                if next_i < len(work_tickers):
                    t = work_tickers[next_i]
                    next_i += 1
                    pending[ex.submit(_read, t)] = t

            # Keep only ``workers`` requests in flight, topping up as each lands,
            # so the run can genuinely stop submitting tickers mid-list.
            for _ in range(workers):
                _submit_next()
            while pending:
                fut = next(as_completed(list(pending)))
                t = pending.pop(fut)
                status, _, shares, price = fut.result()
                if status == "rate_limited":
                    rate_limited_streak += 1
                    if rate_limited_streak >= _RATE_LIMIT_STREAK_STOP:
                        stopped_early = True
                        break
                else:
                    rate_limited_streak = 0
                    if status == "failed":
                        failed += 1
                    else:
                        _record(t, shares, price)
                _submit_next()

    if updates:
        _flush()
    if stopped_early:
        quote_store.record_rate_limit_stop()

    _save_stats_meta(refreshed_by,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"))
    deferred = requested - filled - skipped - failed
    return {"requested": requested, "filled": filled, "skipped": skipped,
            "failed": failed, "deferred": deferred, "rate_limited": stopped_early}


def _sec_member_list() -> tuple[list[dict], str | None]:
    """The universe member list (#264): a FRESH ``sec.exchange_map()`` pull, never the identity cache.

    Returns ``(members, None)``: the SEC rows on Nasdaq, NYSE or CBOE, in file order (OTC and no-exchange rows
    are out). Returns ``([], reason)`` when the map failed, holds no such row or fewer than
    ``MIN_FULL_UNIVERSE`` of them: SEC defines membership, so a short list would replace a stored universe
    with just those few rows (the screener's own count is no guard for it).
    """
    from RevenueChaser.core.identity.sources import sec

    res = sec.exchange_map()
    if not res.ok:
        return [], res.error or "SEC ticker map failed"
    members = [r for r in res.rows
               if str(r.get("exchange") or "").strip().casefold() in SEC_LIST_EXCHANGES]
    if not members:
        return [], "SEC ticker map has no Nasdaq / NYSE / CBOE rows"
    if len(members) < MIN_FULL_UNIVERSE:
        return [], (f"SEC ticker map lists only {len(members)} Nasdaq / NYSE / CBOE rows "
                    f"(floor {MIN_FULL_UNIVERSE})")
    return members, None


def _join_members(members: list[dict], screener_rows: list[dict]) -> tuple[list[dict], int, int]:
    """``(rows, sec_only, not_on_list)``: the member list with Yahoo's market data joined on by ticker.

    Screener rows come first, in pull order (Swing and Fundamentals shortlist the first rows in store order),
    then the listed names the screener did not return (NULL cap, price and quote, the SEC name). A screener
    row that is not on the list is dropped.
    """
    listed = {m["ticker"]: m for m in members}
    joined = []
    for r in screener_rows:
        m = listed.get(r["ticker"])
        if m is None:
            continue
        joined.append(dict(r, name=r["name"] or m.get("name") or ""))
    quoted = {r["ticker"] for r in joined}
    sec_only = [{"ticker": m["ticker"], "market_cap": None, "sector": "", "price": None,
                 "name": m.get("name") or "", "quote": None}
                for m in members if m["ticker"] not in quoted]
    return joined + sec_only, len(sec_only), len(screener_rows) - len(joined)


def fetch_universe_stats(uw=None, min_mc: float = UNIVERSE_MIN_MC, max_mc: float = UNIVERSE_MAX_MC,
                         page_size: int = 250, max_pages: int = 60, retries: int = 5,
                         refresh: bool = False, refreshed_by: str | None = None,
                         no_fetch: bool = False) -> tuple[list[dict], dict]:
    """Ticker universe — cache-first (the ``equity_universe`` table).

    ``(rows, stats)`` döndürür; ``stats["raw_total"]`` ham quote sayısı,
    ``stats["out_of_universe"]`` ise ABD/adi hisse/USD filtresine ya da SEC listesine takılan ticker
    sayısı (cache'den gelince None). Cache varsa ve ``refresh`` False ise ağa hiç
    gitmez.

    A fresh pull (#264) takes the member list from the SEC exchange file (Nasdaq / NYSE / CBOE rows) and joins
    the Yahoo screener's market data to it by ticker. A failed or short SEC map (fewer than ``MIN_FULL_UNIVERSE``
    listed rows) keeps the stored universe and writes nothing (``stats["sec_error"]`` names the failure).

    The Yahoo side is *full* only when every market-cap chunk ran to the total the screener reported AND the
    screener returned at least ``FULL_PULL_MIN_SHARE`` of the stored names that carry a Yahoo quote (D6, #146). A
    full pull replaces the table, re-applies the identity master and runs Phase B. Anything else is *partial*: it
    upserts the quotes it got (stored cap / price / sector carried forward) and the listed names not stored yet,
    re-applies the identity master, records ``__fetch__ = partial`` and skips Phase B (Yahoo is already failing;
    ``/run/universe-stats`` is the resumable caller). A partial pull deletes nothing. ``stats`` carries
    ``partial``, ``chunks_complete`` / ``chunks_total``, ``stored_before``, ``stored_quoted``, ``fetched`` and
    ``not_seen`` (stored quoted names this pull did not return), so a run of partial pulls that never refreshes a
    name is visible.

    The pull runs under ``universe_write_lock``: when another run holds it this returns the stored universe with
    ``stats["locked"]`` set to the holder's pid, calls no network and writes nothing.

    ``no_fetch=True`` (a Refresh or a plain-load self-heal) is store-only (#145): it returns the stored
    universe, or ``([], {"empty_store": True, ...})`` when there is none, and never calls yfinance —
    ``refresh`` is ignored. Loading the universe the first time is a Fetch.

    ``refreshed_by`` — bu çağrı bir taze fetch tetiklerse hangi board'ın yaptığı
    (``stats["universe_refreshed_by"]`` / ``stats["universe_generated_at"]`` ile
    tüm board'lara taşınır; cache'den dönünce son yazanın bilgisi gelir).
    """
    meta = _load_universe_meta()
    cached = _load_cached_universe()
    if cached and (no_fetch or not refresh):
        return cached, {"raw_total": len(cached), "out_of_universe": None, "cached": True,
                        "partial": bool(load_fetch_state().get("partial")),
                        "universe_generated_at": meta.get("generated_at"),
                        "universe_refreshed_by": meta.get("refreshed_by")}
    if no_fetch:
        return [], {"raw_total": 0, "out_of_universe": None, "cached": False, "empty_store": True,
                    "partial": False, "universe_generated_at": None, "universe_refreshed_by": None}

    with universe_write_lock() as holder:
        if holder is not None:
            logger.warning("universe fetch skipped: pid %s holds the universe lock", holder)
            return cached, {"raw_total": len(cached), "out_of_universe": None, "cached": True, "locked": holder,
                            "partial": bool(load_fetch_state().get("partial")),
                            "universe_generated_at": meta.get("generated_at"),
                            "universe_refreshed_by": meta.get("refreshed_by")}
        return _pull_and_store(cached, meta, min_mc, max_mc, page_size, max_pages, retries, refreshed_by)


def _pull_and_store(cached, meta, min_mc, max_mc, page_size, max_pages, retries,
                    refreshed_by) -> tuple[list[dict], dict]:
    """Phase A under the universe lock: the SEC member list, every market-cap chunk, then a full or partial write (D6)."""
    members, sec_error = _sec_member_list()
    if sec_error:
        logger.warning("SEC ticker map unusable (%s); keeping the stored universe (%d)", sec_error, len(cached))
        return cached, {"raw_total": 0, "out_of_universe": None, "cached": False, "sec_error": sec_error,
                        "partial": bool(load_fetch_state().get("partial")),
                        "universe_generated_at": meta.get("generated_at"),
                        "universe_refreshed_by": meta.get("refreshed_by")}

    import time
    import yfinance as yf
    # Read under the lock: the D6 comparison must not use a count another run is about to change.
    stored = rawstore.query("SELECT ticker, quote IS NOT NULL AS quoted FROM equity_universe")
    stored_tickers = {r["ticker"] for r in stored}
    stored_quoted = {r["ticker"] for r in stored if r["quoted"]}
    rows = []
    raw_total = 0
    out_of_universe = 0
    chunks_total = chunks_complete = 0
    quotes_started = quote_store.snapshot_session()   # the session of the first screener call (#294)

    for lo, hi in MC_CHUNKS:
        if max_mc is not None and lo >= max_mc:
            continue
        if min_mc is not None and hi <= min_mc:
            continue
        chunks_total += 1
        offset = 0
        total = None
        for _ in range(max_pages):
            resp = None
            for attempt in range(retries + 1):
                try:
                    resp = yf.screen(_screener_query(lo, hi), offset=offset, size=page_size)
                except Exception as exc:  # pragma: no cover - network
                    logger.warning("yahoo screener %s-%s offset %s: %s", lo, hi, offset, exc)
                    resp = None
                quotes = resp.get("quotes", []) if isinstance(resp, dict) else []
                if quotes or (attempt == retries):
                    break
                time.sleep(2 * (attempt + 1))
            if not isinstance(resp, dict) or not resp.get("quotes", []):
                if isinstance(resp, dict) and total is None:
                    total = resp.get("total")  # a genuinely empty slice reports total 0
                break
            quotes = resp["quotes"]
            if total is None:
                total = resp.get("total")
            for q in quotes:
                raw_total += 1
                if str(q.get("quoteType", "")).upper() != "EQUITY":
                    out_of_universe += 1
                    continue
                exch = str(q.get("exchange") or "").strip().upper()
                if exch and exch not in US_EXCHANGES:
                    out_of_universe += 1
                    continue
                if str(q.get("currency", "")).upper() != "USD":
                    out_of_universe += 1
                    continue
                t = norm_ticker(q.get("symbol"))   # the SEC map's spelling: BRK-B, BF-B, ...
                if not t:
                    out_of_universe += 1
                    continue
                rows.append({
                    "ticker": t,
                    "market_cap": to_float(q.get("marketCap")),
                    "sector": q.get("sector") or "",
                    "price": to_float(q.get("regularMarketPrice")),
                    "name": q.get("shortName") or q.get("longName") or "",
                    "quote": q,
                })
            offset += len(quotes)
            if total and offset >= total:
                break
            time.sleep(1.0)  # chunk içi yumuşatma (rate-limit)
        # Complete = the loop ended because everything the screener reported was read. An empty page after the
        # retries, a missing total or running out of ``max_pages`` leaves the chunk incomplete.
        if isinstance(total, (int, float)) and offset >= total:
            chunks_complete += 1

    # dedupe (dilim sınırları örtüşebilir / ticker tekrarı)
    seen = set()
    uniq = []
    for r in rows:
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            uniq.append(r)
    screener_rows = uniq

    rows, sec_only, not_on_list = _join_members(members, screener_rows)
    out_of_universe += not_on_list
    returned = {r["ticker"] for r in rows if r.get("quote") is not None}     # listed names Yahoo quoted this pull
    all_complete = chunks_total > 0 and chunks_complete == chunks_total
    big_enough = not stored_quoted or len(returned & stored_quoted) >= FULL_PULL_MIN_SHARE * len(stored_quoted)
    full = bool(returned) and all_complete and big_enough
    stats = {"raw_total": raw_total, "out_of_universe": out_of_universe, "cached": False,
             "sec_listed": len(members), "sec_only": sec_only, "not_on_list": not_on_list,
             "partial": not full, "chunks_complete": chunks_complete, "chunks_total": chunks_total,
             "stored_before": len(stored_tickers), "stored_quoted": len(stored_quoted), "fetched": len(returned),
             "not_seen": len(stored_quoted - returned),
             "universe_generated_at": meta.get("generated_at"),
             "universe_refreshed_by": meta.get("refreshed_by")}
    # This session's Yahoo quotes (#294), full or partial pull alike: each is a value Yahoo served now, unless the
    # session changed since the first screener call (then none). Never raises.
    stats["quote_snapshot"] = quote_store.record_yahoo([r for r in rows if r.get("quote") is not None],
                                                       started=quotes_started)

    def _stamp(gen_at: str | None, by: str | None) -> None:
        stats["universe_generated_at"] = gen_at
        stats["universe_refreshed_by"] = by

    gen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if full:
        _save_universe(rows)
        _apply_identity_master()
        _save_universe_meta(refreshed_by, gen_at)
        _save_fetch_state(False, gen_at)
        _stamp(gen_at, refreshed_by)
        # Phase B: fill caps the screener quote omitted, then return the store's
        # view so the rows carry the filled values.
        stats["cap_backfill"] = backfill_universe_caps(refreshed_by=refreshed_by)
        return _load_cached_universe(), stats

    # Partial: the quotes it got, plus the listed names not stored yet. A stored name the screener missed keeps its
    # row (quote, cap, price, sector) untouched, and no stored name is deleted.
    fresh = [r for r in rows if r.get("quote") is not None or r["ticker"] not in stored_tickers]
    logger.warning("yahoo screener partial (%d/%d chunks complete, %d quoted names vs %d stored with a quote); "
                   "upserting %d rows, keeping %d stored names it did not return",
                   chunks_complete, chunks_total, len(returned), len(stored_quoted), len(fresh), stats["not_seen"])
    if not fresh:
        return cached, stats          # nothing came back and nothing is new: the store, and its flag, stay as they are
    _save_universe(fresh, replace=False)
    _apply_identity_master()
    if not stored_tickers:
        # The first load: nothing to protect, so record who loaded it. Otherwise the membership provenance still
        # describes the last full pull and stays as it is.
        _save_universe_meta(refreshed_by, gen_at)
        _stamp(gen_at, refreshed_by)
    _save_fetch_state(True, gen_at)
    return _load_cached_universe(), stats


def fetch_universe(uw=None, min_mc: float = UNIVERSE_MIN_MC, max_mc: float = UNIVERSE_MAX_MC,
                   page_size: int = 250, max_pages: int = 60, retries: int = 5,
                   refresh: bool = False, refreshed_by: str | None = None) -> list[dict]:
    """ABD adi hisse evreni (sadece satırlar) — cache-first."""
    return fetch_universe_stats(uw, min_mc, max_mc, page_size, max_pages, retries, refresh,
                                refreshed_by)[0]
