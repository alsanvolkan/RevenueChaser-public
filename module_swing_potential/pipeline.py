"""Swing pipeline — broad US universe -> indicators -> screeners -> ranking -> payload.

Two-phase:
  1. Reuse the Asymmetric Growth Hunter's universe (the SEC exchange file's
     Nasdaq / NYSE / CBOE rows with Yahoo screener quotes joined on, cached), then the
     7-A prefilter (price and dollar volume on D04's quotes, ``core.screen_common.prefilter``).
  2. Two years of D01 bars per kept ticker (threaded; levels nominal, returns adjusted,
     ``screen_common.load_bars``) -> indicators -> 3 screeners -> RS/quality ranking over every
     scored name -> every LONG setup -> JSON payload. No shortlist cap and no top-N cut (#383 R3 C12, card C).

Market-cap filtering is NOT applied here — it is applied client-side with the
same buckets as the Growth Hunter. Dynamic metrics (universe_total, per-gate
pass counts, avg_quality) are computed from the live run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from RevenueChaser.core.index_registry import annotate_cards
from RevenueChaser.core import screen_common
from RevenueChaser.core import universe as _universe
from RevenueChaser.core.reporting_common import write_artifact
from RevenueChaser.core.taxonomy import assign_themes

from . import config
from . import flow as flow_mod
from . import settings as swing_settings
from RevenueChaser.core.scan_quality import ohlc_drop_reason
from .indicators import compute_indicators, load_ohlc, load_ticker_info  # load_ohlc: the SPY fallback only (U1)
from .ranking import composite, execution_levels, ma_alignment, percentile_ranks, quality_403030
from .screeners import evaluate_direction, evaluate_minervini, evaluate_qullamaggie, evaluate_weinstein

_METHOD_KEYS = ("minervini", "weinstein", "qullamaggie")

logger = logging.getLogger(__name__)


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_as_of() -> str | None:
    """The D01 date of the names this board last scored (the chip, ``core/provenance.py``; K7, #383 R3)."""
    from RevenueChaser.core import provenance
    return provenance.data_as_of("module_swing_potential")


def fetch_universe_stats(refreshed_by: str | None = None,
                         refresh: bool = False,
                         no_fetch: bool = False) -> tuple[list[dict], dict]:
    from RevenueChaser.core.universe_fetch import fetch_universe_stats as _fus
    return _fus(refreshed_by=refreshed_by, refresh=refresh, no_fetch=no_fetch)


def prefilter(rows: list[dict], eff=None) -> screen_common.Prefiltered:
    """The 7-A prefilter with this board's thresholds (``core.screen_common.prefilter``).

    The price and dollar-volume floors are the effective settings (#384 SW-S4): a saved override applies to
    the next Fetch, a default is still read from ``config`` at call time.
    """
    e = eff or swing_settings.effective()
    return screen_common.prefilter(rows, e.prefilter_cfg())


def _compute_row(row: dict, spy_df, no_fetch: bool = False, spine: dict | None = None,
                 drops: list | None = None) -> dict | None:
    """One ticker's row, or None. ``drops`` (a list) collects the reason for each None (#205)."""
    def _drop(reason: str):
        if drops is not None:
            drops.append(reason)
        return None

    t = row["ticker"]
    df, adjusted = screen_common.load_bars(t)          # D01: levels nominal, returns adjusted (C12, D3)
    if df.empty:
        return _drop("no_bars")
    reason = ohlc_drop_reason(df)
    if reason:
        return _drop(reason)
    info = load_ticker_info(t, no_fetch=no_fetch)
    info_stale = bool(info.get("_stale"))
    if info_stale:
        info = {k: v for k, v in info.items() if k != "_stale"}
    ind = compute_indicators(df, spy_df, returns_df=adjusted)
    if ind.get("price") is None:
        return _drop("no_price")

    name = info.get("longName") or info.get("shortName") or row.get("name") or ""
    vendor_industry = info.get("industry") or ""
    summary = info.get("longBusinessSummary") or ""
    # R5 (#88), #266: the sector and industry are the spine's (UW's two levels); the vendor ``.info`` ones never
    # fill the row's own fields. The theme keyword matcher keeps the vendor's free-text industry.
    gics = _universe.gics_of(t, _universe.spine_gics([t]) if spine is None else spine)
    themes = assign_themes(gics.sector, vendor_industry, name, summary, ticker=t)
    earnings_date = _to_float((row.get("quote") or {}).get("earningsTimestampStart"))

    return {
        "ticker": row["ticker"],
        "name": name,
        "market_cap": _to_float(row.get("market_cap")),
        "sector": gics.sector,
        "industry": gics.industry,
        "themes": themes,
        "earnings_date": earnings_date,
        "ind": ind,
        "ohlc_stale": bool(df.attrs.get("stale")),
        "short_refetch_refused": bool(df.attrs.get("short_refetch_refused")),
        "info_stale": info_stale,
    }


def _known_passes(gate: dict) -> int:
    """The number of KNOWN-passed rules in a gate (#384 SW-S1): an unknown rule never counts."""
    return sum(1 for r in gate.get("rules", []) if r.get("passed") is True)


def _slim_gate(gate: dict) -> dict:
    """A candidate's gate without the static text: the card renders it from ``GATE_SPEC`` (#384 SW-S2).

    Per rule only ``id`` / ``passed`` / ``actual`` stay; a Refresh of an old stored payload still renders its own
    label (``report.py`` falls back to the row's ``label`` when ``GATE_SPEC`` has no entry).
    """
    return {
        "passed": bool(gate["passed"]),
        "score": gate["score"],
        "rules": [{"id": r["id"], "passed": r["passed"], "actual": r.get("actual")}
                  for r in gate.get("rules", [])],
    }


def build_payload(results: list[dict], top_n: int | None = None, run_stats: dict = None, eff=None) -> dict:
    """The board payload. Every candidate is a LONG setup that passes the direction gate and one method (#384 SW-S1);
    ``top_n`` cuts only when given. ``eff`` is the run's effective settings (#384 SW-S4), resolved once here when
    the caller has none."""
    e = eff or swing_settings.effective()
    run_stats = run_stats or {}
    results = results or []
    run_stats["stale_info"] = sum(1 for r in results if r.get("info_stale"))
    run_stats["short_refetch_refused"] = sum(1 for r in results if r.get("short_refetch_refused"))

    rs_pct = percentile_ranks([(r["ticker"], r["ind"].get("rs_vs_spy_12m")) for r in results])
    mom_pct = percentile_ranks([(r["ticker"], r["ind"].get("mom_12_1")) for r in results])
    prox_pct = percentile_ranks([(r["ticker"], r["ind"].get("within_52w_high_pct")) for r in results])
    cons_pct = percentile_ranks([(r["ticker"], r["ind"].get("consistency_6m")) for r in results])

    passing = {"minervini": 0, "weinstein": 0, "qullamaggie": 0, "direction": 0}
    candidates = []
    direction_failed: list[str] = []          # passed a method, failed the direction rule: the funnel names them
    for r in results:
        t = r["ticker"]
        ind = r["ind"]
        rs_rating = rs_pct.get(t)
        gates = {
            "minervini": evaluate_minervini(ind, rs_rating, eff=e),
            "weinstein": evaluate_weinstein(ind, eff=e),
            "qullamaggie": evaluate_qullamaggie(ind, eff=e),
            "direction": evaluate_direction(ind),
        }
        for k in passing:
            if gates[k]["passed"]:
                passing[k] += 1

        methods = [k for k in _METHOD_KEYS if gates[k]["passed"]]
        direction_ok = bool(gates["direction"]["passed"])
        if methods and not direction_ok:
            direction_failed.append(t)
        if not direction_ok or not methods:
            continue                                    # direction required, at least one method (#384 SW-S1)

        quality = quality_403030(_known_passes(gates["minervini"]), _known_passes(gates["weinstein"]),
                                 _known_passes(gates["qullamaggie"]))
        momentum = composite(mom_pct.get(t), prox_pct.get(t), cons_pct.get(t), eff=e)
        levels = execution_levels(ind, eff=e)
        flow = {"status": "not_scanned"} if config.MANUAL_FLOW_ONLY else flow_mod.poll_flow(None, t)
        candidates.append({
            "ticker": t,
            "name": r.get("name", ""),
            "price": ind["price"],
            "change_pct": ind.get("change_pct"),
            "change_1w": ind.get("change_1w"),
            "change_1m": ind.get("change_1m"),
            "market_cap": r.get("market_cap"),
            "sector": r.get("sector"),
            "industry": r.get("industry", ""),
            "themes": r.get("themes") or [],
            "earnings_date": r.get("earnings_date"),
            "quality": quality,
            "momentum": momentum,
            "direction_ok": direction_ok,
            "methods": methods,
            "bucket": "+".join(methods),
            "rs_rating": rs_rating,
            "ma_alignment": ma_alignment(ind),
            "entry": levels["entry"],
            "target": levels["target"],
            "stop": levels["stop"],
            "rr": levels["rr"],
            "atr": levels.get("atr"),
            "gates": {k: _slim_gate(v) for k, v in gates.items()},
            "flow": flow,
        })

    candidates.sort(key=lambda c: (-c["quality"],
                                   -(c["momentum"] if c["momentum"] is not None else float("-inf")),
                                   c["ticker"]))
    run_stats["passed_screens"] = len(candidates)       # before any top_n cut (the C16 funnel)
    # The C16 funnel: names that passed a method but not the direction rule are their own step, not "passed none
    # of the board's three screens" (#384 review).
    run_stats["screen_drops"] = [("direction_failed", "Passed a method but not the direction rule (50-day MA ≥ "
                                  "200-day MA, 3-month return ≥ 0)", direction_failed)]
    if top_n:
        candidates = candidates[:top_n]
    annotate_cards(candidates)

    quals = [c["quality"] for c in candidates if c["quality"] is not None]
    avg_quality = round(sum(quals) / len(quals), 2) if quals else None

    regime = run_stats.get("regime")
    return {
        "generated_at": _now(),
        "data_as_of": run_stats.get("data_as_of"),
        "selection_version": config.SELECTION_VERSION,
        "regime": regime,
        "universe_total": run_stats.get("raw_universe", len(results)),
        "universe_generated_at": run_stats.get("universe_generated_at"),
        "universe_refreshed_by": run_stats.get("universe_refreshed_by"),
        "universe_partial": bool(run_stats.get("universe_partial")),
        "passing": passing,
        "avg_quality": avg_quality,
        "is_fixture": False,
        "no_data": False,
        "data_quality": {"stale_ohlc": run_stats.get("stale_ohlc", 0),
                         "stale_info": run_stats.get("stale_info", 0),
                         "short_refetch_refused": run_stats.get("short_refetch_refused", 0),
                         "spy_stale": bool(run_stats.get("spy_stale")),
                         "dropped": run_stats.get("dropped") or {}},
        "summary": {
            "stale_ohlc": run_stats.get("stale_ohlc", 0),
            "spy_stale": bool(run_stats.get("spy_stale")),
            "regime": regime,
            "spy_price": run_stats.get("spy_price"),
            "spy_sma200": run_stats.get("spy_sma200"),
            "spy_3m_return": run_stats.get("spy_3m_return"),
            "filtered_universe": run_stats.get("filtered_universe"),
            "out_of_universe": run_stats.get("out_of_universe"),
            "hygiene": run_stats.get("hygiene"),
            "excluded_types": run_stats.get("excluded_types", 0),
            "no_spine_gics": run_stats.get("no_spine_gics", 0),
            "data_short": run_stats.get("data_short"),
            "passing": passing,
            "avg_quality": avg_quality,
            "top_n": len(candidates),
        },
        "candidates": candidates,
    }


def demo_payload() -> dict:
    """[DEMO/FIXTURE] — empty scan fallback."""
    return {
        "generated_at": _now(),
        "data_as_of": None,
        "selection_version": config.SELECTION_VERSION,
        "regime": "RISK-ON",
        "universe_total": 0,
        "universe_generated_at": None,
        "universe_refreshed_by": None,
        "passing": {"minervini": 0, "weinstein": 0, "qullamaggie": 0, "direction": 0},
        "avg_quality": None,
        "is_fixture": True,
        "summary": {
            "regime": "RISK-ON",
            "spy_price": None, "spy_sma200": None, "spy_3m_return": None,
            "filtered_universe": 0, "out_of_universe": 0,
            "hygiene": 0, "data_short": 0,
            "passing": {"minervini": 0, "weinstein": 0, "qullamaggie": 0, "direction": 0},
            "avg_quality": None, "top_n": 0,
        },
        "candidates": [],
    }


def run_scan(uw=None, top_n: int = None, refresh_universe: bool = False,
             no_fetch: bool = False, rows_sink: dict | None = None) -> dict:
    """End-to-end scan; returns the JSON payload. ``uw`` is unused (manual flow).

    ``refresh_universe=True`` re-fetches the shared ticker universe; otherwise the
    cached universe is reused. ``no_fetch=True`` (a Refresh) scores nothing: it returns
    the payload the last Fetch recorded (K4, ``screen_common.stored_payload``), and no
    network is touched. The shared body is ``core.screen_common.run_screen``.
    ``rows_sink`` (a dict) receives the score and gate rows a Fetch persisted (``scores.persist``), for the result
    history; a Refresh persists nothing and leaves it empty.
    """
    from . import scores
    # Without a sink the one-argument call stays as it was (stand-ins of ``scores.persist`` keep working);
    # each of prefilter / build_payload / persist resolves the effective settings (#384 SW-S4) once itself.
    persist = scores.persist if rows_sink is None else (lambda results: scores.persist(results, sink=rows_sink))
    return screen_common.run_screen(
        board="swing", refreshed_by="Swing Potential", cfg=config,
        load_universe=fetch_universe_stats, prefilter=prefilter,
        load_spy=lambda: screen_common.load_spy(lambda: load_ohlc("SPY", ttl_days=0.5, no_fetch=no_fetch)),
        compute_row=_compute_row, build_payload=build_payload, persist=persist,
        top_n=top_n, refresh_universe=refresh_universe, no_fetch=no_fetch,
        history_board="module_swing_potential", saved=config.SWING_JSON)


def save_payload(payload: dict) -> str:
    write_artifact(config.SWING_JSON, json.dumps(payload, ensure_ascii=False))
    return str(config.SWING_JSON)
