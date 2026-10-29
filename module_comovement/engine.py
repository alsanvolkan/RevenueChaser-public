"""Co-movement engine — returns → clusters → dated hierarchy.

Both paths read the prices store's adjusted closes (``rcdata.read.ticker_bars``, through
``ohlc.price_frame``; PR 8) and never touch the network: the prices build
writes the store, a Fetch persists a dated run and a model-only (``no_fetch``) run
re-clusters or serves one. Results are persisted as a dated run (``comov_runs`` / ``comov_nodes``
/ ``comov_members``) so a Refresh can re-render with zero network.
"""
from __future__ import annotations

import hashlib
import json
import functools
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from RevenueChaser.core import calendar
from RevenueChaser.core import funnel as funnel_mod
from RevenueChaser.core import ledger
from RevenueChaser.core import rawstore
from RevenueChaser.core import universe as universe_mod

from . import cluster as cluster_mod
from . import config
from . import label as label_mod
from . import noise
from . import ohlc
from . import persistence
from . import returns as returns_mod
from . import settings as settings_mod

logger = logging.getLogger(__name__)

# #379 COM-3: a store-only run (Refresh, window switch, Settings recompute) reads its window of the prices store
# and takes the whole-history facts from SQL (``_window_prices``). False restores the full read: the parity
# tests' baseline, and a rollback that needs no revert.
WINDOWED_REFRESH = True


def _source(eff: settings_mod.Effective | None = None) -> dict:
    """Where the board's numbers come from ("About this data", ADR-3 disclosure)."""
    eff = eff or settings_mod.effective()
    return {
        "provider": "Prices store (Alpaca daily bars; the WRDS history comes with the history run)",
        "detail": "daily split/dividend-adjusted closes (rcdata.read.ticker_bars, basis=total_return)",
        "period": config.OHLC_PERIOD,
        "table": "prices.duckdb → price_bar_daily + adj_factor", "window": eff.window,
        "oos_holdout": eff.oos_holdout, "floor_mc": eff.floor,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _resolve(horizon: int | None = None,
             benchmark: str | None = None) -> tuple[int, str]:
    horizon = config.DEFAULT_HORIZON if horizon is None else int(horizon)
    benchmark = config.DEFAULT_BENCHMARK if benchmark is None else str(benchmark)
    if benchmark not in config.BENCHMARKS:
        benchmark = config.DEFAULT_BENCHMARK
    return horizon, benchmark


def config_hash(window: int, linkage: str, cuts,
                horizon: int | None = None, benchmark: str | None = None, *,
                min_obs: int | None = None, oos_holdout: int | None = None,
                winsor_k: float | None = None, min_periods: int | None = None,
                bench_stale_sessions: int | None = None,
                floor: float | None = None,
                benchmark_applied: bool | None = None) -> str:
    """Identity of everything that changes a run's numbers.

    window, linkage, cuts, horizon, benchmark, MIN_OBS, OOS_HOLDOUT, WINSOR_K,
    MIN_PERIODS, BENCH_STALE_SESSIONS, the market-cap floor and the engine's
    method (``config.METHOD``): a stored null or lagged test is reused only under
    exactly these inputs. An input left ``None``
    is resolved by ``settings.effective(window)`` (what a run at ``window``
    would use). ``benchmark_applied=False`` with a benchmark asked for is the
    raw-return fallback (no usable benchmark prices): its own hash, so its null
    and lagged test never stand in for a residual run's, nor the other way
    round (Co-movement review N2). ``None`` reads as applied.
    """
    horizon, benchmark = _resolve(horizon, benchmark)
    eff = settings_mod.effective(window)
    pick = lambda v, d: d if v is None else v  # noqa: E731
    raw = "|".join(str(x) for x in (
        window, linkage, tuple(cuts), horizon, benchmark,
        pick(min_obs, eff.min_obs), pick(oos_holdout, eff.oos_holdout),
        f"{float(pick(winsor_k, eff.winsor_k)):g}", pick(min_periods, eff.min_periods),
        pick(bench_stale_sessions, eff.bench_stale_sessions),
        f"{float(pick(floor, eff.floor)):g}", f"method={config.METHOD}"))
    if benchmark_applied is False and config.BENCHMARKS.get(benchmark):
        raw += "|returns=raw"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _medium_theme_labels(fields: dict) -> dict[str, list[str]]:
    """Medium NMF labels from ``filter_fields`` theme ids, not ``ticker_scores.themes``."""
    from RevenueChaser.core.gics_map import is_unclassified

    catalog = universe_mod.theme_catalog()
    out: dict[str, list[str]] = {}
    for ticker, row in fields.items():
        labels: list[str] = []
        for tid in (row or {}).get("theme_ids") or []:
            rec = catalog.get(tid) or {}
            if rec.get("level") != "medium":
                continue
            label = rec.get("label") or ""
            if not label or is_unclassified(label) or label in labels:
                continue
            labels.append(label)
        if labels:
            out[ticker] = labels
    return out


def _last_move_pct(ret, members: list[str], horizon: int) -> float | None:
    """Mean of each member's latest residual log return x 100 (log points, not a simple percent).

    ``ret`` is already built from overlapping ``horizon``-session log returns
    (``returns.log_returns``), so its last row *is* the move over the last
    ``horizon`` sessions. Averaging the last ``horizon`` rows would blend
    overlapping windows (wrong for horizon 3/5/10; identical at 1). ``horizon``
    is kept for the call signature only.

    This is the card's "last n days vs the benchmark" direction. Store-only: it
    reads the return matrix already built for the run — no prices, no network.
    """
    if ret is None or ret.empty:
        return None
    cols = [m for m in members if m in ret.columns]
    if not cols:
        return None
    vals = ret[cols].tail(1).to_numpy(dtype=float)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return None
    return float(vals.mean() * 100.0)


def _settings_keys(eff: settings_mod.Effective | None = None) -> dict:
    """The payload keys that describe the effective settings (Part B's UI)."""
    eff = eff or settings_mod.effective()
    return {
        "window": eff.window, "settings": eff.payload(),
        "horizon_locked": eff.horizon_locked,
        "short_window_note": settings_mod.short_note(eff.window) if eff.short_window else None,
    }


def _provider_health(fetch: dict | None) -> dict | None:
    """The banner's health from this run's fetch report (#80).

    Co-movement calls no provider itself (the prices build writes the closes). ``failed`` is the count the fetch
    report already carries; a clean report (``failed`` 0, which a store coverage check is) leaves the banner empty.
    """
    failed = int((fetch or {}).get("failed") or 0)
    if failed <= 0:
        return None
    calls = int((fetch or {}).get("requested") or failed)
    return {"provider": "daily_bars", "calls": calls, "failed": failed, "auth_error": None,
            "failures": [{"endpoint": "price_bars_daily", "error": f"{failed} failed"}],
            "breaker_open": False, "skipped_calls": 0}


def _empty_payload(tickers, fetch, dropped, n, cuts, horizon, benchmark,
                   prices_through=None, benchmark_status=None,
                   window_end=None, no_prices=None, n_factors=None,
                   flat_prints=None, eff=None) -> dict:
    return {
        "as_of": _now(), "generated_at": _now(), "universe_total": len(tickers),
        "eligible": n, "dropped": dropped, "cuts": list(cuts or []),
        "clusters": {}, "fetch": fetch, "config_hash": None,
        "horizon": horizon, "benchmark": benchmark, "benchmark_applied": False,
        "theme_catalog": {}, "funnel": {}, "funnel_steps": [], "source": _source(eff),
        "prices_through": prices_through, "benchmark_status": benchmark_status,
        "window_end": window_end, "no_prices": no_prices, "n_factors": n_factors,
        "flat_prints": flat_prints or [], "oos": {},
        "provider_health": _provider_health(fetch),
        **_settings_keys(eff),
    }


def _significant(mean_corr, baseline_r) -> bool | None:
    """``mean_corr > baseline_r`` when both are known, else ``None``.

    ``None`` means *not judged* (a run persisted before S8 has no baseline, and
    a Refresh with no persisted null for its config hash has none either).
    The board treats it as significant — it must not hide a group it cannot
    judge — so the distinction is deliberate, not a missing value.
    """
    if mean_corr is None or baseline_r is None:
        return None
    return bool(float(mean_corr) > float(baseline_r))


def _last_dates(prices) -> dict[str, str]:
    """Ticker -> last date with a close, ``YYYY-MM-DD``."""
    out = {}
    for col in prices.columns:
        idx = prices[col].last_valid_index()
        if idx is not None:
            out[col] = idx.strftime("%Y-%m-%d")
    return out


def _median_date(dates: list[str]) -> str | None:
    return sorted(dates)[len(dates) // 2] if dates else None


def _catchall(n, total) -> bool:
    """True when a group holds more than ``CATCHALL_SHARE`` of its cut (S12).

    Derived the same way on a run and on a reload (``n`` over the members at
    that cut), so no store column is needed.
    """
    try:
        return bool(total) and float(n) / float(total) > config.CATCHALL_SHARE
    except (TypeError, ValueError):
        return False


def _member_order(tickers, core: dict) -> list[str]:
    """Names already in the group a quarter ago first, then ticker.

    One order, used by ``run`` and ``load_payload`` both: a fresh run kept the
    cluster's own order while a reload sorted alphabetically, so the same group
    read differently after a Refresh (review C1).
    """
    return sorted(tickers, key=lambda t: (0 if core.get(t) else 1, str(t)))


def _persisted_source(chash: str) -> str | None:
    """``prices_through`` of the latest persisted Fetch run with this config hash.

    A Refresh reuses that run's null and lagged test; when its prices differ from
    the ones being re-clustered now, the board says whose verdicts it shows
    (review SP-F7) instead of presenting them as current.
    """
    try:
        row = rawstore.query(
            "SELECT prices_through FROM comov_runs WHERE config_hash = ? "
            "AND null_json IS NOT NULL ORDER BY as_of DESC LIMIT 1", [str(chash)])
    except Exception:  # noqa: BLE001 - store predates the column
        return None
    return row[0].get("prices_through") if row else None


def split_short_history(prices, dropped, window: int, horizon: int, first: dict | None = None
                        ) -> tuple[list[str], list[str]]:
    """Split the names dropped for a short history into ``(listed_recently,
    missing_bars)``.

    *Listed recently*: the ticker's first stored bar is inside the window (it
    listed after the window start), so no Fetch can give it more history — by
    design. *Missing bars*: listed before the window start yet still short of
    ``MIN_OBS`` returns in it, so the stored prices have gaps. A Fetch does not fill
    them, but a session can come back: the prices build re-reads the last sessions
    every run and the history run fills older ones. Reads only the price frame the run already holds (store-only). ``first`` (ticker -> first bar) is
    given when that frame holds only the window (#379 COM-3), whose first bar is not the listing.
    """
    if not dropped or prices is None or prices.empty:
        return [], sorted(dropped or [])
    start = prices.tail(int(window) + max(1, int(horizon))).index[0]
    recent, missing = [], []
    for t in sorted(dropped):
        if first is not None:
            listed = first.get(t)
        else:
            listed = prices[t].first_valid_index() if t in prices.columns else None
        (recent if listed is not None and listed > start else missing).append(t)
    return recent, missing


def _funnel(counts: dict, no_prices, dropped, flat_prints, eligible: int,
            short_split: tuple[list, list] | None = None,
            window: int | None = None, min_obs: int | None = None) -> dict:
    """The universe line every board states (ADR-2): all caps → floor → drops.

    ``short_history`` stays the sum of ``listed_recently`` + ``missing_bars``
    (legacy readers), which are recorded when ``short_split`` is given.
    """
    out = {**counts,
           "no_prices": len(no_prices or []), "short_history": len(dropped or []),
           "flat": len(flat_prints or []), "clustered": int(eligible)}
    if short_split is not None:
        out["listed_recently"] = len(short_split[0])
        out["missing_bars"] = len(short_split[1])
    if window is not None:
        out["window"] = int(window)
    if min_obs is not None:
        out["min_obs"] = int(min_obs)
    return out


_STEP_KEYS = ("scanned_total", "below_floor", "no_market_cap", "no_prices",
              "short_history", "flat", "clustered")


def _short_history_steps(f: dict, prices_fix: dict | None) -> list[dict]:
    """One ``short_history`` step, or — when the run recorded the split —
    ``listed_recently`` (by design, no fix) and ``missing_bars`` (gaps in the stored bars; no button fixes them, but
    the prices build re-reads the last sessions and the history run fills older ones, so the label says a session can
    come back)."""
    window = f.get("window") or settings_mod.effective().window
    min_obs = f.get("min_obs") or settings_mod.effective(window).min_obs
    if f.get("listed_recently") is None or f.get("missing_bars") is None:
        return [funnel_mod.step(
            "short_history", f"Short history (< {min_obs} of {window} sessions)",
            f["short_history"], "drop", by_design=False, fix=prices_fix)]
    return [
        funnel_mod.step(
            "listed_recently",
            f"Listed recently (first price inside the {window}-session window)",
            f["listed_recently"], "drop", by_design=True),
        funnel_mod.step(
            "missing_bars", f"Missing price bars (< {min_obs} of {window} sessions; a missing session can come back "
                            "with the next prices build or the history run)",
            f["missing_bars"], "drop", by_design=False, fix=prices_fix),
    ]


_UNKNOWN_TYPE_LINK = {"tab": "module_settings", "anchor": "identity",
                      "label": "Settings → Identity"}


def _type_steps(f: dict) -> list[dict]:
    """The security-type filter (Identity R4, #87): one by-design drop per type group,
    then a note when kept names have no established type.

    Empty for a funnel persisted before the filter (no group keys): it gets no type
    rows and no note. The note is shown, never counted (``core.funnel``).
    """
    keys = [key for key, _label, _types in universe_mod.TYPE_GROUPS]
    if any(f.get(k) is None for k in keys):
        return []
    steps = [funnel_mod.step(key, label, f[key], "drop", by_design=True)
             for key, label, _types in universe_mod.TYPE_GROUPS]
    if f.get("unknown_type"):
        steps.append(funnel_mod.step(
            "unknown_type", "Unknown security type — kept", f["unknown_type"], "note",
            link=_UNKNOWN_TYPE_LINK))
    return steps


def funnel_steps(f: dict) -> list[dict]:
    """The shared-format funnel (``core.funnel``) for a ``_funnel`` dict.

    ``[]`` when ``f`` lacks any count (a run persisted before the funnel was
    recorded): the board then reports "no funnel" rather than a guessed one. The
    security-type rows are added only when ``f`` carries all three group counts.
    """
    if not f or any(f.get(k) is None for k in _STEP_KEYS):
        return []
    caps = {"route": "/run/universe-stats", "label": "Fetch caps"}
    # A Fetch downloads no prices since #379 (the prices build fills the store), so a price drop has no button
    prices = None
    return [
        funnel_mod.step("scanned", "Scanned universe", f["scanned_total"], "start"),
        funnel_mod.step("below_floor",
                        f"Below the ${(f.get('floor') or universe_mod.DEFAULT_MC_MIN) / 1e6:,.0f}M "
                        "classification floor",
                        f["below_floor"], "drop", by_design=True),
        funnel_mod.step("no_market_cap", "No market cap", f["no_market_cap"],
                        "drop", by_design=False, fix=caps),
        *_type_steps(f),
        funnel_mod.step("no_prices", "No price history", f["no_prices"],
                        "drop", by_design=False, fix=prices),
        *_short_history_steps(f, prices),
        funnel_mod.step("flat", "Flat / halted prints", f["flat"], "drop",
                        by_design=True),
        funnel_mod.step("clustered", "Clustered", f["clustered"], "result"),
    ]


def _persisted_null(chash: str) -> dict[int, float]:
    """The shuffled-returns null of the latest persisted Fetch run with the same
    config hash (window, linkage, cuts, horizon, benchmark).

    A Refresh does not pay for ``NULL_PERMS`` extra clusterings. No such run, or
    a store without ``null_json``, yields ``{}``: groups are then not judged and
    stay on the board rather than being folded away on a guess.
    """
    try:
        row = rawstore.query(
            "SELECT null_json FROM comov_runs WHERE config_hash = ? "
            "AND null_json IS NOT NULL ORDER BY as_of DESC LIMIT 1", [str(chash)])
    except Exception:  # noqa: BLE001 - store predates the column
        return {}
    if not row or not row[0].get("null_json"):
        return {}
    try:
        return {int(k): float(v) for k, v in json.loads(row[0]["null_json"]).items()}
    except (TypeError, ValueError):
        return {}


def _persisted_lag(chash: str) -> dict[int, list[dict]]:
    """The lagged groups + held-out test of the latest persisted Fetch run with
    the same config hash, as ``persistence.lagged_test`` returns them.

    ``{}`` when there is none (or the store predates ``comov_lag``): cards then
    carry no persistence verdict rather than a guessed one.
    """
    try:
        row = rawstore.query(
            "SELECT r.as_of FROM comov_runs r WHERE r.config_hash = ? AND EXISTS "
            "(SELECT 1 FROM comov_lag l WHERE l.as_of = r.as_of) "
            "ORDER BY r.as_of DESC LIMIT 1", [str(chash)])
        if not row:
            return {}
        rows = rawstore.query(
            "SELECT cut, lag_id, ticker, oos_r, oos_base, held FROM comov_lag "
            "WHERE as_of = ?", [row[0]["as_of"]])
    except Exception:  # noqa: BLE001 - store predates the table
        return {}
    groups: dict[tuple, dict] = {}
    for r in rows:
        g = groups.setdefault((int(r["cut"]), r["lag_id"]), {
            "lag_id": r["lag_id"], "members": [], "oos_r": r.get("oos_r"),
            "oos_base": r.get("oos_base"),
            "held": None if r.get("held") is None else bool(r["held"])})
        g["members"].append(r["ticker"])
    out: dict[int, list[dict]] = {}
    for (cut, _lid), g in groups.items():
        out.setdefault(cut, []).append(g)
    return out


def _persist_cols(p: dict | None) -> dict:
    """``comov_nodes`` persistence columns for a ``persistence.match`` verdict."""
    p = p or {}
    return {"persist_jaccard": p.get("jaccard"), "persist_new": p.get("new"),
            "persist_held": p.get("held"), "oos_r": p.get("oos_r"),
            "oos_base": p.get("oos_base")}


def _persist_from_row(r: dict) -> dict | None:
    """The card's ``persist`` dict from a ``comov_nodes`` row (``None`` = none)."""
    if r.get("persist_jaccard") is None and r.get("persist_new") is None:
        return None
    return {"jaccard": r.get("persist_jaccard"),
            "new": None if r.get("persist_new") is None else bool(r["persist_new"]),
            "held": None if r.get("persist_held") is None else bool(r["persist_held"]),
            "oos_r": r.get("oos_r"), "oos_base": r.get("oos_base")}


def _stored_run(chash: str, prices_through: str | None,
                clustered: set[str], price_tickers: list[str]) -> str | None:
    """``as_of`` of the latest stored run a store-only run may serve, or ``None``.

    Same config hash (every input that changes the numbers), same
    ``prices_through``, and the same clustered names: a changed universe or a
    name that crossed MIN_OBS changes the clustering even when the median last
    date has not moved. The run must also be newer than the last store change for
    these tickers (``ohlc.last_written``: bars or actions): a new dividend or
    split revises the adjusted history without moving the last date. A run
    older than ``config.ENGINE_EPOCH`` is never served (bumped whenever a
    change alters the engine's numbers at the same config hash and prices):
    it recomputes under the fixed numbers instead.
    """
    if not prices_through:
        return None
    try:
        epoch = datetime.fromisoformat(str(config.ENGINE_EPOCH))
        written = ohlc.last_written(price_tickers)
        rows = rawstore.query(
            "SELECT as_of FROM comov_runs WHERE config_hash = ? AND prices_through = ? "
            "ORDER BY as_of DESC", [str(chash), str(prices_through)])
        for r in rows:
            as_of_dt = datetime.fromisoformat(str(r["as_of"]))
            if as_of_dt < epoch:
                break                       # this and every older run predate the epoch
            if as_of_dt.timestamp() < float(written):
                break                       # older runs predate it too
            names = rawstore.query(
                "SELECT DISTINCT ticker FROM comov_members WHERE as_of = ?", [r["as_of"]])
            if {m["ticker"] for m in names} == clustered:
                return r["as_of"]
    except Exception:  # noqa: BLE001 - store predates a table/column: recompute
        return None
    return None


def _serve_stored(as_of: str, eff: settings_mod.Effective, *, tickers, fetch,
                  dropped, no_prices, flat_prints, window_end, funnel) -> dict:
    """The stored run at ``as_of``, with what this run measured live on top.

    Membership, verdicts and the window dates are the stored run's. Each
    cluster's ``label`` is recomputed against the *current* industry / theme
    spine — not the one in effect when the run was stored — exactly as
    ``run`` computes it, and each cut is re-sorted on the refreshed labels; an
    industry reclassification since the run was stored shows up on it
    right away. The fetch report, drop lists, universe count and funnel are
    this run's own. The key set is exactly ``run``'s.
    """
    p = load_payload(as_of)
    members = sorted({m["ticker"] for items in p["clusters"].values()
                      for c in items for m in c["members"]})
    fields = universe_mod.filter_fields(members)
    industry_by = {t: (fields.get(t) or {}).get("industry") or "" for t in members}
    themes = _medium_theme_labels(fields)
    for items in p["clusters"].values():
        level = {c["id"]: [m["ticker"] for m in c["members"]] for c in items}
        named = label_mod.label_level(
            level, industry_by, themes, [t for mem in level.values() for t in mem])
        for c in items:
            c["label"] = named.get(c["id"]) or f"Cluster {c['id']}"
        items.sort(key=lambda x: (-x["n"], x["label"]))
    p.update({
        "fetch": fetch, "dropped": dropped, "no_prices": no_prices,
        "flat_prints": flat_prints, "universe_total": len(tickers),
        "window_end": window_end, "funnel": funnel,
        "funnel_steps": funnel_steps(funnel), "verdicts_from": None,
        "oos": None if eff.short_window else p.get("oos"),
        "oos_note": (f"Not evaluated: a {eff.window}-session window is too short "
                     "for the out-of-sample persistence test.")
        if eff.short_window else None,
        "provider_health": _provider_health(fetch),
    })
    return p


def precompute(windows=None, horizon: int | None = None,
               benchmark: str | None = None, *, run_id: str | None = None,
               as_ofs: dict[int, str] | None = None) -> dict[int, str]:
    """Compute and store the ``windows`` runs a board switch will ask for (#95).

    Store-only (``no_fetch``), at the current settings. A window whose stored run
    is still current (``_stored_run``) is served, not recomputed. Returns
    ``{window: "computed" | "reused" | "empty"}``; a run is reused when its
    ``as_of`` predates this call.

    Every run it stores carries ``run_id`` (the Fetch's, #379 D2; one fresh id
    for the whole call when none is given). ``as_ofs``, when given, receives
    ``{window: as_of}`` of each window's run, computed or reused, so the Fetch's
    header can list them. It records no header itself.
    """
    windows = config.PRECOMPUTE_WINDOWS if windows is None else tuple(windows)
    run_id = run_id or ledger.new_run_id()
    start = _now()
    out: dict[int, str] = {}
    for w in windows:
        p = run(no_fetch=True, persist=True, reuse=True, window=int(w),
                horizon=horizon, benchmark=benchmark, run_id=run_id)
        out[int(w)] = ("empty" if not p.get("config_hash") else
                       "reused" if (p.get("as_of") or "") < start else "computed")
        if as_ofs is not None and p.get("config_hash"):
            as_ofs[int(w)] = p["as_of"]
    return out


def _bench_cut(sessions, last: dict, tickers, bench_ticker, eff) -> pd.Timestamp | None:
    """The benchmark's last bar when it is stale, else ``None``.

    Stale = more than ``bench_stale_sessions`` sessions behind the median last date of ``tickers``. Counts
    sessions only up to that date: a few names with stray newer bars must not make a current benchmark look
    stale.
    """
    prices_through = _median_date([d for t, d in last.items() if t in tickers])
    if not (bench_ticker and bench_ticker in last and prices_through):
        return None
    b_last = pd.Timestamp(last[bench_ticker])
    behind = int(((sessions > b_last) & (sessions <= pd.Timestamp(prices_through))).sum())
    return b_last if behind > eff.bench_stale_sessions else None


def _flat(prices, tickers) -> list[str]:
    return sorted(t for t in returns_mod.flat_print_names(prices, run=config.FLAT_RUN,
                                                          max_share=config.FLAT_SHARE)
                  if t in tickers)


def _window_prices(price_tickers, tickers, bench_ticker, eff, horizon):
    """A store-only run's inputs from its window of the prices store (#379 COM-3): no number moves.

    The full read loaded every stored bar (two years of every name) only to use the last ``window + horizon``
    sessions. Three facts still need the whole history and come from one SQL read (``ohlc.history_facts``): each
    name's first and last bar (no prices, the median last date, listed recently vs missing bars), the session
    index (a stale benchmark, where the window starts), and the flat printers, judged on the whole history by
    the same ``flat_print_names`` over the few candidates SQL finds. The frame keeps every column the full
    read has (a name with bars only before the window is still dropped, not "no prices").

    Returns ``(prices, last, first, flat_prints, bench_stale)``.
    """
    facts = ohlc.history_facts(price_tickers, flat_run=config.FLAT_RUN)
    last = facts["last"]
    if not last:
        return pd.DataFrame(), {}, {}, [], False
    sessions = pd.DatetimeIndex(pd.to_datetime(facts["sessions"]))
    cut = _bench_cut(sessions, last, tickers, bench_ticker, eff)
    if cut is not None:
        sessions = sessions[sessions <= cut]
    need = max(eff.window + max(1, int(horizon)), eff.oos_holdout + 1)
    start = sessions[-need].strftime("%Y-%m-%d") if len(sessions) >= need else None
    prices = ohlc.price_frame(price_tickers, start=start)
    if prices.empty:
        prices = pd.DataFrame(index=pd.DatetimeIndex([], name="ts"))
    prices = prices.reindex(columns=pd.Index(sorted(last), name="ticker"))
    whole = ohlc.price_frame([t for t in facts["flat"] if t in tickers])
    if cut is not None:
        prices = prices.loc[:cut]
        if not whole.empty:
            whole = whole.loc[:cut]
    # A name's first bar stays its listing even when it falls after a stale benchmark's cut: it then listed after
    # the window began, not "missing bars" (Co-movement review N3).
    first = {t: pd.Timestamp(d) for t, d in facts["first"].items()}
    return prices, last, first, _flat(whole, tickers), cut is not None


def _daily_under(prices, ret, horizon: int):
    """The daily log returns under ``ret``'s ``horizon``-session ones (``horizon - 1`` rows more, same columns),
    which the h > 1 null shuffles and sums (Co-movement review S1); ``None`` at 1 day or when they do not line up."""
    if int(horizon) <= 1 or ret is None or ret.empty:
        return None
    p = prices.loc[:ret.index[-1], [c for c in ret.columns if c in prices.columns]].tail(len(ret) + int(horizon))
    if p.shape[1] != ret.shape[1] or len(p) != len(ret) + int(horizon) or not p.index[int(horizon):].equals(ret.index):
        return None
    return np.log(p / p.shift(1)).iloc[1:].replace([np.inf, -np.inf], np.nan)


def run(no_fetch: bool = False, tickers: list[str] | None = None,
        cuts: tuple[int, ...] | None = None, horizon: int | None = None,
        benchmark: str | None = None, persist: bool | None = None,
        window: int | None = None, reuse: bool = False,
        run_id: str | None = None) -> dict:
    """Compute the dated co-movement hierarchy for the liquid universe.

    ``horizon`` is the overlapping return window (1/3/5/10); ``benchmark`` names
    the one-factor market to residualize against (``off`` keeps raw returns).
    ``window`` is the number of sessions (a board control like ``horizon``);
    ``None`` means the Settings default. Every other input comes from
    ``settings.effective`` — a window under ``config.SHORT_WINDOW`` scales
    MIN_OBS / MIN_PERIODS, locks the horizon to 1 and skips the lagged test.
    ``persist`` defaults to ``not no_fetch``: only a Fetch records a dated run.

    A run that does not persist (or ``reuse=True``) serves the latest stored run
    with the same config hash, ``prices_through`` and clustered names instead of
    re-clustering (#95); its clusters' labels are still recomputed against the
    current industry/theme spine rather than kept from storage time, and a run
    older than ``config.ENGINE_EPOCH`` is never served. See ``_serve_stored``.

    A stored run carries ``run_id`` (the Fetch's; a fresh one when none is
    given), its session and its drop lists (#379 D2).
    """
    eff = settings_mod.effective(window)
    horizon, benchmark = _resolve(horizon, benchmark)
    if eff.horizon_locked:
        horizon = 1
    bench_ticker = config.BENCHMARKS.get(benchmark)
    if tickers is None:
        tickers = ohlc.universe_tickers()
    fetch = ohlc.fetch_bulk(tickers, no_fetch=no_fetch)

    price_tickers = list(tickers)
    if bench_ticker and bench_ticker not in price_tickers:
        price_tickers.append(bench_ticker)
    persisting = (not no_fetch) if persist is None else persist
    first = None
    if WINDOWED_REFRESH and not persisting:
        prices, last, first, flat_prints, bench_stale = _window_prices(
            price_tickers, tickers, bench_ticker, eff, horizon)
    else:
        prices = ohlc.price_frame(price_tickers)
        last = _last_dates(prices) if not prices.empty else {}
        cut = _bench_cut(prices.index, last, tickers, bench_ticker, eff)
        bench_stale = cut is not None
        if bench_stale:
            # Each name's listing, read before the cut: one listed after it is listed recently (N3).
            first = {t: prices[t].first_valid_index() for t in prices.columns}
            # Residuals need the market return on every date; cut the
            # window at the benchmark's last bar instead of NaN-ing the
            # newest sessions for every name (review F3).
            prices = prices.loc[:cut]
        # Flat printers are judged on the history actually used (after any stale
        # truncation), and get their own bucket: they have prices and history, so
        # they belong in neither ``no_prices`` nor ``dropped``.
        flat_prints = _flat(prices, tickers)
    prices_through = _median_date([d for t, d in last.items() if t in tickers])
    no_prices = sorted(t for t in tickers if t not in last)
    if flat_prints:
        prices = prices.drop(columns=[t for t in flat_prints if t in prices.columns])

    ret, dropped = returns_mod.log_returns(prices, window=eff.window,
                                           min_obs=eff.min_obs, horizon=horizon)
    dropped = [t for t in dropped if t in tickers]
    short_split = split_short_history(prices, dropped, eff.window, horizon, first=first)

    benchmark_applied = False
    benchmark_status = "missing" if bench_ticker else "off"
    universe_ret = ret
    market = None
    if bench_ticker and bench_ticker in ret.columns:
        market = ret[bench_ticker]
        universe_ret = ret.drop(columns=[bench_ticker])
        if int(market.notna().sum()) >= 3 and universe_ret.shape[1] >= 3:
            benchmark_applied = True
            benchmark_status = "applied"
            if bench_stale:
                benchmark_status = "stale"

    window_end = ret.index[-1].strftime("%Y-%m-%d") if len(ret) else None
    n = 0 if universe_ret.empty else int(universe_ret.shape[1])
    if n >= 3 and (reuse or not persisting):
        chash = config_hash(eff.window, config.LINKAGE,
                            config.cuts_for(n) if cuts is None else cuts,
                            horizon, benchmark, min_obs=eff.min_obs,
                            oos_holdout=eff.oos_holdout, winsor_k=eff.winsor_k,
                            min_periods=eff.min_periods,
                            bench_stale_sessions=eff.bench_stale_sessions,
                            floor=eff.floor, benchmark_applied=benchmark_applied)
        stored = _stored_run(chash, prices_through, set(universe_ret.columns),
                             price_tickers)
        if stored:
            funnel = _funnel(universe_mod.universe_counts(eff.floor), no_prices,
                             dropped, flat_prints, n, short_split=short_split,
                             window=eff.window, min_obs=eff.min_obs)
            return _serve_stored(stored, eff, tickers=tickers, fetch=fetch,
                                 dropped=dropped, no_prices=no_prices,
                                 flat_prints=flat_prints, window_end=window_end,
                                 funnel=funnel)
    if benchmark_applied:
        universe_ret = returns_mod.residualize(universe_ret, market)
    # The honest counterweight to 250 groups: how many common moves the window
    # holds beyond noise (eigenvalues above the Marchenko–Pastur edge).
    n_factors = noise.factors_above_noise(universe_ret, horizon=horizon)
    # S10: the hierarchy is built on the winsorised matrix (one print must not
    # decide a group), while ``universe_ret`` stays raw for the direction line.
    clustered_ret = returns_mod.winsorize_mad(universe_ret, eff.winsor_k)
    n = int(clustered_ret.shape[1]) if not clustered_ret.empty else 0
    if n < 3:
        return _empty_payload(tickers, fetch, dropped, n, cuts,
                              horizon, benchmark, prices_through=prices_through,
                              benchmark_status=benchmark_status,
                              window_end=window_end, no_prices=no_prices,
                              n_factors=n_factors, flat_prints=flat_prints, eff=eff)

    if cuts is None:
        cuts = config.cuts_for(n)
    cluster_fn = functools.partial(cluster_mod.clusters, min_periods=eff.min_periods)
    result = cluster_fn(clustered_ret, cuts)
    if result is None:
        return _empty_payload(tickers, fetch, dropped, n, cuts,
                              horizon, benchmark, prices_through=prices_through,
                              benchmark_status=benchmark_status,
                              window_end=window_end, no_prices=no_prices,
                              n_factors=n_factors, flat_prints=flat_prints, eff=eff)
    _Z, labels, corr, groups = result
    chash = config_hash(eff.window, config.LINKAGE, cuts, horizon, benchmark,
                        min_obs=eff.min_obs, oos_holdout=eff.oos_holdout,
                        winsor_k=eff.winsor_k, min_periods=eff.min_periods,
                        bench_stale_sessions=eff.bench_stale_sessions,
                        floor=eff.floor, benchmark_applied=benchmark_applied)

    # The significance null and the lagged persistence test are Fetch-only
    # costs — NULL_PERMS + 1 extra clusterings. A Refresh reuses the last
    # persisted run's null and lagged groups (same config hash); with no such
    # run, groups are not judged and carry no persistence verdict.
    if persisting:
        null = noise.selected_group_null(clustered_ret, cuts, cluster_fn,
                                         n_perm=noise.NULL_PERMS, horizon=horizon,
                                         daily=_daily_under(prices, clustered_ret, horizon),
                                         winsor_k=eff.winsor_k)
        # Out-of-sample persistence: one extra clustering on the window ending
        # a quarter ago, tested on that quarter only (persistence.py). A short
        # window has no room for it: skipped, and the payload says why.
        lag = ({} if eff.short_window else
               persistence.lagged_test(prices, cuts, horizon, bench_ticker,
                                       cluster_fn, apply_bench=benchmark_applied,
                                       eff=eff))
    else:
        null = _persisted_null(chash)
        lag = _persisted_lag(chash)
    src = None if persisting else _persisted_source(chash)
    verdicts_from = src if (src and src != prices_through and (null or lag)) else None
    fields = universe_mod.filter_fields(list(labels))
    themes = _medium_theme_labels(fields)

    by_ticker = {t: fields[t] for t in labels if t in fields}
    industry_by = {t: (by_ticker.get(t) or {}).get("industry") or "" for t in labels}

    as_of = _now()
    clusters: dict[str, list[dict]] = {}
    nodes, members = [], []
    for cut, per in groups.items():
        items = []
        cut_total = sum(len(m) for m in per.values())
        named = label_mod.label_level(
            per, industry_by, themes, [t for mem in per.values() for t in mem])
        for cid, mem in per.items():
            label = named.get(cid) or f"Cluster {cid}"
            mean_corr = cluster_mod.mean_intra_corr(corr, mem)
            baseline_r = noise.baseline_for(null, len(mem))
            significant = _significant(mean_corr, baseline_r)
            last_pct = _last_move_pct(universe_ret, mem, horizon)
            persist = persistence.match(mem, lag.get(int(cut)))
            core = persistence.core_members(mem, lag.get(int(cut)))
            ordered = _member_order(mem, core)
            nodes.append({"as_of": as_of, "cut": cut, "cluster_id": str(cid),
                          "label": label, "n": len(mem), "mean_corr": mean_corr,
                          "baseline_r": baseline_r,
                          "last_residual_pct": last_pct,
                          **_persist_cols(persist)})
            for t in ordered:
                members.append({"as_of": as_of, "cut": cut,
                                "cluster_id": str(cid), "ticker": t,
                                "core": core[t]})
            items.append({"id": str(cid), "label": label, "n": len(mem),
                          "mean_corr": mean_corr, "baseline_r": baseline_r,
                          "significant": significant,
                          "catchall": _catchall(len(mem), cut_total),
                          "last_residual_pct": last_pct,
                          "persist": persist,
                          "members": [{**by_ticker[t], "core": core[t]}
                                      for t in ordered if t in by_ticker]})
        items.sort(key=lambda x: (-x["n"], x["label"]))
        clusters[str(cut)] = items

    oos = None if eff.short_window else persistence.summary(lag)
    window_start = (clustered_ret.index[0].strftime("%Y-%m-%d")
                    if len(clustered_ret) else None)
    oos_start = (prices.index[-1 - eff.oos_holdout].strftime("%Y-%m-%d")
                 if len(prices) > eff.oos_holdout and not eff.short_window else None)
    funnel = _funnel(universe_mod.universe_counts(eff.floor), no_prices, dropped,
                     flat_prints, n, short_split=short_split, window=eff.window,
                     min_obs=eff.min_obs)
    if persisting:
        _persist(as_of, chash, n, nodes, members, horizon, benchmark, eff.window,
                 benchmark_status=benchmark_status, prices_through=prices_through,
                 n_factors=n_factors, null=null, lag=lag, oos=oos,
                 window_start=window_start, oos_start=oos_start, funnel=funnel,
                 run_id=run_id or ledger.new_run_id(),
                 dropped={"dropped": dropped, "no_prices": no_prices,
                          "flat_prints": flat_prints})
    return {
        "as_of": as_of, "generated_at": as_of, "universe_total": len(tickers),
        "eligible": n, "dropped": dropped, "cuts": list(cuts),
        "clusters": clusters, "fetch": fetch, "config_hash": chash,
        "horizon": horizon, "benchmark": benchmark,
        "benchmark_applied": benchmark_applied,
        "theme_catalog": universe_mod.theme_catalog(),
        "prices_through": prices_through, "benchmark_status": benchmark_status,
        "window_start": window_start, "oos_start": oos_start, "funnel": funnel,
        "funnel_steps": funnel_steps(funnel),
        "verdicts_from": verdicts_from, "source": _source(eff),
        "window_end": window_end, "no_prices": no_prices, "n_factors": n_factors,
        "flat_prints": flat_prints, "oos": oos,
        "oos_note": (f"Not evaluated: a {eff.window}-session window is too short "
                     "for the out-of-sample persistence test.")
        if eff.short_window else None,
        "provider_health": _provider_health(fetch),
        **_settings_keys(eff),
    }


def _loads_or_empty(text) -> dict:
    try:
        return json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}


def _session_date(as_of: str):
    """The New York session a run at ``as_of`` belongs to (rule B), or ``None``
    outside the holiday table (``calendar.session_for`` never guesses)."""
    try:
        return calendar.session_for(datetime.fromisoformat(as_of)).session_date
    except ValueError:
        return None


def _persist(as_of: str, chash: str, n: int, nodes: list[dict],
             members: list[dict], horizon: int, benchmark: str, window: int,
             benchmark_status: str | None = None,
             prices_through: str | None = None,
             n_factors: int | None = None,
             null: dict | None = None,
             lag: dict | None = None,
             oos: dict | None = None,
             window_start: str | None = None,
             oos_start: str | None = None,
             funnel: dict | None = None,
             run_id: str | None = None,
             dropped: dict | None = None) -> None:
    # One transaction: the run header, its clusters and its members land
    # together or not at all. The explicit DELETEs also clear a re-run whose
    # new batch is empty (write_rows' "replace" only clears groups it sees).
    def _write(tx):
        tx.write_rows("comov_runs", [{
            "as_of": as_of, "generated_at": as_of, "window_days": window,
            "n_tickers": n, "linkage": config.LINKAGE, "config_hash": chash,
            "horizon": horizon, "benchmark": benchmark,
            "benchmark_status": benchmark_status, "prices_through": prices_through,
            "n_factors": n_factors,
            "null_json": (json.dumps({str(k): v for k, v in null.items()})
                          if null else None),
            "oos_json": json.dumps(oos) if oos else None,
            "window_start": window_start, "oos_start": oos_start,
            "funnel_json": json.dumps(funnel) if funnel else None,
            "run_id": run_id, "session_date": _session_date(as_of),
            "dropped_json": json.dumps(dropped) if dropped is not None else None,
        }])
        for table in ("comov_nodes", "comov_members", "comov_lag"):
            tx.ensure_table(table)
            tx.execute(f"DELETE FROM {table} WHERE as_of = ?", [as_of])
        tx.write_rows("comov_nodes", nodes)
        tx.write_rows("comov_members", members)
        tx.write_rows("comov_lag", [
            {"as_of": as_of, "cut": int(cut), "lag_id": g["lag_id"], "ticker": t,
             "oos_r": g["oos_r"], "oos_base": g["oos_base"], "held": g["held"]}
            for cut, rows in (lag or {}).items() for g in rows for t in g["members"]])

    rawstore.atomic(_write)


def load_payload(as_of: str | None = None, horizon: int | None = None,
                 benchmark: str | None = None) -> dict:
    """Rebuild the board payload from the store (zero network).

    With no ``as_of``, returns the latest run matching ``horizon``/``benchmark``
    (unset filters match any run).
    """
    if as_of is None:
        where, params = [], []
        if horizon is not None:
            where.append("horizon = ?")
            params.append(int(horizon))
        if benchmark is not None:
            where.append("benchmark = ?")
            params.append(str(benchmark))
        sql = "SELECT as_of FROM comov_runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY as_of DESC LIMIT 1"
        row = rawstore.query(sql, params)
        if not row:
            return _empty_payload([], {}, [], 0, (), horizon, benchmark)
        as_of = row[0]["as_of"]
    run_row = rawstore.query("SELECT * FROM comov_runs WHERE as_of = ?", [as_of])
    if not run_row:
        return _empty_payload([], {}, [], 0, (), horizon, benchmark)
    # ``SELECT *`` so a store that predates a column (``last_residual_pct``,
    # then ``core``) still loads: the value is simply
    # absent, not a hard error.
    node_rows = rawstore.query(
        "SELECT * FROM comov_nodes WHERE as_of = ? ORDER BY cut, n DESC", [as_of])
    member_rows = rawstore.query(
        "SELECT * FROM comov_members WHERE as_of = ?", [as_of])

    member_map: dict[tuple, list[str]] = {}
    core_map: dict[tuple, dict] = {}
    for r in member_rows:
        key = (r["cut"], r["cluster_id"])
        member_map.setdefault(key, []).append(r["ticker"])
        core = r.get("core")
        core_map.setdefault(key, {})[r["ticker"]] = None if core is None else bool(core)

    fields = universe_mod.filter_fields(sorted({t for m in member_map.values() for t in m}))
    clusters: dict[str, list[dict]] = {}
    cut_total: dict = {}
    for (cut, _cid), mem in member_map.items():
        cut_total[cut] = cut_total.get(cut, 0) + len(mem)
    for r in node_rows:
        key = (r["cut"], r["cluster_id"])
        core = core_map.get(key, {})
        mem = _member_order(member_map.get(key, []), core)
        clusters.setdefault(str(r["cut"]), []).append({
            "id": r["cluster_id"], "label": r["label"], "n": r["n"],
            "mean_corr": r["mean_corr"],
            "baseline_r": r.get("baseline_r"),
            "significant": _significant(r.get("mean_corr"), r.get("baseline_r")),
            "catchall": _catchall(r["n"], cut_total.get(r["cut"])),
            "last_residual_pct": r.get("last_residual_pct"),
            "persist": _persist_from_row(r),
            "members": [{**(fields.get(t) or {"ticker": t, "name": t}),
                         "core": core.get(t)} for t in mem],
        })
    for items in clusters.values():
        items.sort(key=lambda x: (-x["n"], x["label"]))
    rr = run_row[0]
    persisted_funnel = _loads_or_empty(rr.get("funnel_json"))
    bench = rr.get("benchmark")
    eff = settings_mod.effective(rr.get("window_days"))
    return {
        "as_of": rr["as_of"], "generated_at": rr["generated_at"],
        "universe_total": rr["n_tickers"], "eligible": rr["n_tickers"],
        "cuts": sorted({int(k) for k in clusters}),
        "theme_catalog": universe_mod.theme_catalog(),
        "clusters": clusters, "fetch": None, "config_hash": rr["config_hash"],
        "dropped": None, "no_prices": None,       # not persisted: unknown on reload
        "flat_prints": None,
        "horizon": rr.get("horizon"), "benchmark": bench,
        "benchmark_status": rr.get("benchmark_status"),
        "benchmark_applied": rr.get("benchmark_status") in ("applied", "stale"),
        "prices_through": rr.get("prices_through"),
        "n_factors": rr.get("n_factors"),
        "oos": _loads_or_empty(rr.get("oos_json")),
        "window_start": rr.get("window_start"), "oos_start": rr.get("oos_start"),
        "funnel": {**persisted_funnel, **universe_mod.universe_counts()},
        # steps come from the run's own persisted counts, so they add up exactly
        # as they did when the run was made (the live universe may have moved on)
        "funnel_steps": funnel_steps(persisted_funnel),
        "verdicts_from": None, "source": _source(eff),
        **_settings_keys(eff),
    }
