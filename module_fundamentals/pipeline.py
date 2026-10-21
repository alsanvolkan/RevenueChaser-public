"""Fundamental Analysis pipeline — universe -> EDGAR fundamentals + price -> screen -> payload.

Two-phase:
  1. Reuse the Asymmetric Growth Hunter's universe (the SEC exchange file's
     Nasdaq / NYSE / CBOE rows with Yahoo screener quotes joined on), then the
     7-A prefilter (price and dollar volume on D04's quotes, ``core.screen_common.prefilter``).
  2. D01 bars (levels nominal, returns adjusted, ``screen_common.load_bars``) + SEC EDGAR
     (annual 10-K + quarterly 10-Q) per kept ticker -> 3 screeners (Rule of 40 / Quality /
     Momentum) -> the rules each candidate passed (ranked by count, then Rule of 40) -> every
     candidate in the payload. No shortlist cap and no top-N cut (#383 R3 C12, card C).

Market-cap filtering is applied client-side (universal filters).
"""
from __future__ import annotations

import json
import logging
from datetime import date as _date
from datetime import datetime, timezone

from RevenueChaser.core.index_registry import annotate_cards
from RevenueChaser.core.liquidity import liquid_assets
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.core.reporting_common import write_artifact
# load_ohlc: the SPY fallback only (U1); a scored name reads D01 (``screen_common.load_bars``).
from RevenueChaser.module_swing_potential.indicators import compute_indicators, load_ohlc, load_ticker_info
from RevenueChaser.core import fiscal_year
from RevenueChaser.core import screen_common
from RevenueChaser.core import universe as _universe
from RevenueChaser.core.taxonomy import assign_themes

from RevenueChaser.core.scan_quality import ohlc_drop_reason

from . import config
from .screeners import evaluate_momentum, evaluate_quality, evaluate_rule_of_40, is_distorted

logger = logging.getLogger(__name__)


def _to_float(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today():
    """The run's calendar date (a seam: tests monkeypatch it for the stale-filer gate, #386 F1)."""
    return _date.today()


def _ret(close, n: int) -> float | None:
    if close is None or len(close) < n + 1 or n <= 0:
        return None
    base = _to_float(close.iloc[-1 - n])
    last = _to_float(close.iloc[-1])
    if not base or not last:
        return None
    return last / base - 1.0


# Consecutive fiscal quarters end ~91 days apart; a few days either way is calendar slack (#148 U3).
_QUARTER_GAP_DAYS = (80, 102)


def _is_derived(rec: dict, field: str) -> bool:
    """A figure the extractor derived (a Q4 as FY minus the first three quarters, a cash flow from YTD) is marked in
    the record's ``derived`` list by field or by period end. Derived quarters are not reported ones, so they never
    form a TTM run: the Rule of 40 stays on the fiscal-year basis until the YTD consumer lands (#158). A record
    without the list is unchanged."""
    marks = rec.get("derived") or []
    return field in marks or str(rec.get("period"))[:10] in marks


def _quarter_run(quarterly: list[dict], field: str, n: int) -> tuple[list[float], list[str]] | None:
    """The newest ``n`` quarters that have a ``field`` figure, as ``(values, period ends)``, only when those
    quarters are consecutive (period ends ~91 days apart). ``None`` otherwise: a gap (no Q4 on a 10-Q only run, a
    missing quarter) is never summed as if it were a trailing year (#148 U3)."""
    have = [(q.get("period"), _to_float(q.get(field))) for q in quarterly
            if _to_float(q.get(field)) is not None and not _is_derived(q, field)]
    if len(have) < n:
        return None
    tail = have[-n:]
    try:
        ends = [_date.fromisoformat(str(p)[:10]) for p, _v in tail]
    except (ValueError, TypeError):
        return None
    lo, hi = _QUARTER_GAP_DAYS
    if any(not lo <= (b - a).days <= hi for a, b in zip(ends, ends[1:])):
        return None
    return [v for _p, v in tail], [str(p)[:10] for p, _v in tail]


def _ttm_sum(quarterly: list[dict], field: str, end: str) -> float | None:
    """Sum of the newest four consecutive ``field`` quarters, only when they end on ``end``."""
    run = _quarter_run(quarterly, field, 4)
    return sum(run[0]) if run and run[1][-1] == end else None


# The Rule of 40 inputs (the TTM is built for these three together) and the ones that cannot be negative
# (``sec_edgar._NON_NEGATIVE_FLOWS``: a negative revenue or capex is an extraction or restatement artefact, D1).
_RO40_FIELDS = ("revenue", "operating_cash_flow", "capex")
_NON_NEGATIVE = frozenset({"revenue", "capex"})
_NEXT_START_DAYS = (1, 7)    # a fiscal year starts 1-7 days after the last one ended (sec_edgar.py:501-503)
_YEAR_DAYS = (350, 380)      # sec_edgar._YEAR_DAYS: the same period one fiscal year earlier


def _iso_date(s):
    try:
        return _date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _days(a, b) -> int | None:
    da, db = _iso_date(a), _iso_date(b)
    return None if da is None or db is None else (db - da).days


def _fy_inputs(rec: dict, fields) -> tuple[dict, str, str] | None:
    """``({field: value}, start, end)`` of one annual record when every field has a value, a ``period_starts`` and a
    ``period_ends`` entry and all fields share one start and one end; else None."""
    starts, ends, vals = rec.get("period_starts") or {}, rec.get("period_ends") or {}, {}
    s_set, e_set = set(), set()
    for f in fields:
        v = _to_float(rec.get(f))
        if v is None or not starts.get(f) or not ends.get(f):
            return None
        vals[f] = v
        s_set.add(starts[f])
        e_set.add(ends[f])
    if len(s_set) != 1 or len(e_set) != 1:
        return None
    return vals, s_set.pop(), e_set.pop()


def _ytd(rec: dict, field: str):
    """``(start, months, value)`` of the cumulative fact the extractor stored for ``field`` on this record."""
    e = (rec.get("ytd") or {}).get(field)
    if not e:
        return None
    v = _to_float(e.get("val"))
    return None if v is None else (e.get("start"), e.get("months"), v)


def _ytd_point(quarterly: list[dict], fields, start: str, months: int, near_end: str):
    """``(end, {field: value})`` of the record that ends one fiscal year before ``near_end`` and whose ``ytd`` holds,
    for every field, the ``months`` fact that starts on ``start``; else None."""
    for q in reversed(quarterly):
        end = str(q.get("period"))[:10]
        gap = _days(end, near_end)
        if gap is None or not _YEAR_DAYS[0] <= gap <= _YEAR_DAYS[1]:
            continue
        vals = {}
        for f in fields:
            y = _ytd(q, f)
            if not y or y[0] != start or y[1] != months:
                break
            vals[f] = y[2]
        else:
            return end, vals
    return None


def _ttm_point(annual: list[dict], quarterly: list[dict], i: int):
    """The Rule of 40 inputs on ``annual[i]`` plus the newest YTD after it, minus the same YTD a year earlier.

    ``(point, reason)``: ``point`` is a dict (current end, prior-year end, months, the pieces, ``ttm``) or None, with
    the reason it is None. ``latest_is_fy``: no 10-Q after the 10-K, the normal case right after a filing."""
    fy0 = _fy_inputs(annual[i], _RO40_FIELDS)
    if fy0 is None:
        return None, "fy_incomplete"
    vals0, s0, e0 = fy0
    cur_end = cur_start = None
    for q in reversed(quarterly):
        end = str(q.get("period"))[:10]
        if end <= e0:
            break
        for f in _RO40_FIELDS:
            y = _ytd(q, f)
            gap = _days(e0, y[0]) if y else None
            if gap is not None and _NEXT_START_DAYS[0] <= gap <= _NEXT_START_DAYS[1]:
                cur_end, cur_start = end, y[0]
                break
        if cur_end:
            break
    if cur_end is None:
        later = any(str(q.get("period"))[:10] > e0 for q in quarterly)
        return None, ("ytd_incomplete_current" if later else "latest_is_fy")
    rec = next(q for q in quarterly if str(q.get("period"))[:10] == cur_end)
    cur, months = {}, set()
    for f in _RO40_FIELDS:
        y = _ytd(rec, f)
        if not y or y[0] != cur_start:
            return None, "ytd_incomplete_current"
        cur[f] = y[2]
        months.add(y[1])
    if len(months) != 1 or next(iter(months)) not in (3, 6, 9):
        return None, "ytd_incomplete_current"
    m = months.pop()
    prior = _ytd_point(quarterly, _RO40_FIELDS, s0, m, cur_end)
    if prior is None:
        return None, "ytd_incomplete_prior"
    p_end, prev = prior
    return {"end": cur_end, "prior_end": p_end, "months": m, "fy": vals0, "fy_start": s0, "fy_end": e0,
            "ytd": cur, "ytd_prior": prev, "ttm": {f: vals0[f] + cur[f] - prev[f] for f in _RO40_FIELDS}}, None


def _negative_fields(p: dict) -> list[str]:
    """Non-negative fields whose FY, YTD, prior YTD or FY - prior YTD (the rest of last year) is below zero."""
    return [f for f in _RO40_FIELDS if f in _NON_NEGATIVE
            and (min(p["fy"][f], p["ytd"][f], p["ytd_prior"][f]) < 0 or p["fy"][f] - p["ytd_prior"][f] < 0)]


def _ro40_point(quarterly: list[dict], annual: list[dict]):
    """``(point, growth, reason)`` for the TTM Rule of 40, or ``(None, None, reason)`` and the basis is the year's.

    Growth is the TTM revenue against the TTM revenue one year earlier (D3): the prior fiscal year plus last year's
    YTD minus the YTD two years back, on the same months."""
    quarterly = sorted(quarterly, key=lambda q: str(q.get("period")))
    i = fiscal_year.anchor(annual)
    if i is None:
        return None, None, "no_annual"
    p, why = _ttm_point(annual, quarterly, i)
    if p is None:
        return None, None, why
    bad = _negative_fields(p)
    if bad:
        return None, None, "negative:" + "+".join(bad)
    if p["ttm"]["revenue"] <= 0:
        return None, None, "nonpositive_revenue"
    j = fiscal_year.prior(annual, i)
    if j is None:
        return None, None, "prior_fy_missing"
    fy1 = _fy_inputs(annual[j], ("revenue",))
    if fy1 is None:
        return None, None, "prior_fy_incomplete"
    v1, s1, e1 = fy1
    gap = _days(e1, p["fy_start"])
    if gap is None or not _NEXT_START_DAYS[0] <= gap <= _NEXT_START_DAYS[1]:
        return None, None, "prior_fy_not_adjacent"
    p2 = _ytd_point(quarterly, ("revenue",), s1, p["months"], p["prior_end"])
    if p2 is None:
        return None, None, "ytd_incomplete_prior2"
    y2 = p2[1]["revenue"]
    if min(v1["revenue"], y2) < 0 or v1["revenue"] - y2 < 0:
        return None, None, "negative_prior:revenue"
    prev_ttm = v1["revenue"] + p["ytd_prior"]["revenue"] - y2
    if prev_ttm <= 0:
        return None, None, "nonpositive_prior_ttm"
    p = {**p, "ttm_prior_revenue": prev_ttm}
    return p, (p["ttm"]["revenue"] - prev_ttm) / prev_ttm, None


def _ttm_metrics(quarterly: list[dict], annual: list[dict]) -> dict:
    out = {"ttm_revenue": None, "rev_growth_ttm": None, "fcf_margin": None,
           "ttm_net_income": None, "ro40": None, "ro40_basis": None}

    # ttm_revenue (the DISTORTED base) and ttm_net_income (P/E) keep their own rule: four consecutive reported
    # quarters, else the year (avo, D2 B 2026-10-08). Only the Rule of 40 and its growth move to the TTM point.
    rev4 = _quarter_run(quarterly, "revenue", 4)
    ttm_rev = sum(rev4[0]) if rev4 else None
    if ttm_rev:
        out["ttm_revenue"] = ttm_rev
        out["ttm_net_income"] = _ttm_sum(quarterly, "net_income", rev4[1][-1])

    i = fiscal_year.anchor(annual)
    a = annual[i] if i is not None else {}
    point, growth, _why = _ro40_point(quarterly, annual)
    if point:
        out["ro40_basis"] = "TTM"
        out["rev_growth_ttm"] = growth
        out["fcf_margin"] = (point["ttm"]["operating_cash_flow"] - point["ttm"]["capex"]) / point["ttm"]["revenue"]
    elif annual:
        # One basis for the whole Rule of 40: the newest year with revenue and the year exactly before it (#148).
        out["ro40_basis"] = "FY"
        j = fiscal_year.prior(annual, i)
        r0 = _to_float(a.get("revenue"))
        r1 = _to_float(annual[j].get("revenue")) if j is not None else None
        if r0 is not None and r1:
            out["rev_growth_ttm"] = (r0 - r1) / r1
        o0, c0 = _to_float(a.get("operating_cash_flow")), _to_float(a.get("capex"))
        if r0 and o0 is not None and c0 is not None:
            out["fcf_margin"] = (o0 - c0) / r0
        elif r0 and (o0 is None or c0 is None):
            # #386 F2: the free-cash-flow margin needs operating cash flow and capex; without either the Rule of 40
            # reads the anchor year's EBITDA margin instead (review: a capex backfill alone must not turn a shown
            # EBITDA Rule of 40 into nothing when cash flow is still missing, scripts/tests/test_reextract_dryrun_246.py) ((ebit + depreciation) / revenue, ebit / revenue without depreciation). The
            # basis says so and the Rule-of-40 rule is unknown: the value is shown, never decides (screeners.py).
            ebit, dep = _to_float(a.get("ebit")), _to_float(a.get("depreciation"))
            ebitda = (ebit + dep) if (ebit is not None and dep is not None) else ebit
            if ebitda is not None:
                out["ro40_basis"] = "FY EBITDA"
                out["fcf_margin"] = ebitda / r0

    if out["ttm_revenue"] is None:
        out["ttm_revenue"] = _to_float(a.get("revenue"))
    if out["ttm_net_income"] is None:
        out["ttm_net_income"] = _to_float(a.get("net_income"))

    if out["rev_growth_ttm"] is not None and out["fcf_margin"] is not None:
        out["ro40"] = (out["rev_growth_ttm"] + out["fcf_margin"]) * 100.0
    return out


def _quality_metrics(annual: list[dict]) -> dict:
    out = {"gross_margin": None, "roic": None, "net_debt_ebitda": None,
           "net_income": None, "total_equity": None}
    i = fiscal_year.anchor(annual)          # #386 F2: Quality reads the anchor year, not annual[-1]
    if i is None:
        return out
    a = annual[i]
    rev = _to_float(a.get("revenue"))
    gp = _to_float(a.get("gross_profit"))
    ebit = _to_float(a.get("ebit"))
    dep = _to_float(a.get("depreciation"))
    debt = _to_float(a.get("total_debt"))
    cash = _to_float(a.get("cash"))
    eq = _to_float(a.get("total_equity"))

    out["net_income"] = _to_float(a.get("net_income"))
    out["total_equity"] = eq

    if rev and gp is not None:
        out["gross_margin"] = gp / rev

    ebitda = (ebit + dep) if (ebit is not None and dep is not None) else ebit
    liquid, _basis = liquid_assets(a)  # cash + short-term investments of this record (#187)
    if ebitda is not None and ebitda > 0 and debt is not None and liquid is not None:
        out["net_debt_ebitda"] = (debt - liquid) / ebitda

    if ebit is not None and debt is not None and eq is not None and cash is not None:
        nopat = ebit * (1 - config.TAX_RATE)
        ic = debt + eq - cash
        out["roic"] = nopat / ic if ic > 0 else None
    return out


def _q_label(iso: str) -> str:
    try:
        d = _date.fromisoformat(iso[:10])
        q = (d.month - 1) // 3 + 1
        return f"Q{q}/{d.year}"
    except (ValueError, TypeError):
        return iso


def _period_label(quarterly: list[dict], annual: list[dict]) -> str:
    """The periods the Rule of 40 compares, on the basis ``_ttm_metrics`` used."""
    point, _g, _why = _ro40_point(quarterly, annual)
    if point:
        return f"{_q_label(point['end'])} vs {_q_label(point['prior_end'])}"
    i = fiscal_year.anchor(annual)
    year = fiscal_year.fy(annual[i]) if i is not None else None
    if year is None:
        return "latest"
    return f"FY{year} vs FY{year - 1}" if fiscal_year.prior(annual, i) is not None else f"FY{year}"


def data_as_of() -> str | None:
    """The D01 date of the names this board last scored (the chip, ``core/provenance.py``; K7, #383 R3)."""
    from RevenueChaser.core import provenance
    return provenance.data_as_of("module_fundamentals")


def fetch_universe_stats(refreshed_by: str | None = None,
                         refresh: bool = False,
                         no_fetch: bool = False):
    from RevenueChaser.core.universe_fetch import fetch_universe_stats as _fus
    return _fus(refreshed_by=refreshed_by, refresh=refresh, no_fetch=no_fetch)


def prefilter(rows: list[dict]) -> screen_common.Prefiltered:
    """The 7-A prefilter with this board's thresholds (``core.screen_common.prefilter``)."""
    return screen_common.prefilter(rows, config)


def _compute_row(row: dict, spy_df, no_fetch: bool = False, spine: dict | None = None,
                 drops: list | None = None) -> dict | None:
    """One ticker's row, or None. ``drops`` (a list) collects the reason for each None (#205)."""
    def _drop(reason: str):
        if drops is not None:
            drops.append(reason)
        return None

    ticker = row["ticker"]
    # D01 first (store only, C12): a name with no usable bars costs no EDGAR or ``.info`` call.
    df, adjusted = screen_common.load_bars(ticker)
    if df.empty:
        return _drop("no_bars")
    reason = ohlc_drop_reason(df)
    if reason:
        return _drop(reason)

    # #386 F1: the spine sector, read before EDGAR so an excluded name costs no EDGAR read (card 4 (b), Common C17).
    gics = _universe.gics_of(ticker, _universe.spine_gics([ticker]) if spine is None else spine)
    if gics.sector in config.EXCLUDED_SECTORS:
        return _drop("sector_excluded")

    try:
        annual, quarterly, emeta = sec_edgar.get_fundamentals(ticker, no_fetch=no_fetch)
    except Exception as exc:  # pragma: no cover - network
        logger.debug("fundamental edgar %s: %s", ticker, exc)
        return _drop("edgar_fetch_failed")     # the call failed: a data gap, not "EDGAR has nothing" (#386 F1)
    if not annual:
        # sec_edgar swallows a failed download and answers empty; its meta says so (#386 review).
        return _drop("edgar_fetch_failed" if (emeta or {}).get("fetch_failed") else "no_edgar_annual")
    # A filer whose newest annual report ended longer ago than fiscal_year.STALE_DAYS is stale. Staleness reads the
    # NEWEST record, never the revenue anchor (an anchor can be old only because a newer year has no revenue figure;
    # core/fiscal_year.py:15-17, the #380 measurement: 341 of 384 anchor-"stale" names were fresh filers, #386 F1).
    if fiscal_year.latest_is_stale(annual, _today()):
        return _drop("stale_annual")

    info = load_ticker_info(ticker, no_fetch=no_fetch)
    info_stale = bool(info.get("_stale"))
    if info_stale:
        info = {k: v for k, v in info.items() if k != "_stale"}

    ind = compute_indicators(df, spy_df, returns_df=adjusted)
    price = ind.get("price")
    if price is None:
        return _drop("no_price")

    ttm = _ttm_metrics(quarterly, annual)
    # #386 F1: an absurd Rule of 40 is a data problem, not a pick.
    if ttm["ro40"] is not None and ttm["ro40"] > config.RO40_BARRIER:
        return _drop("ro40_barrier")
    qual = _quality_metrics(annual)
    market_cap = _to_float(row.get("market_cap")) or _to_float(info.get("marketCap"))

    pe = (market_cap / ttm["ttm_net_income"]) if (market_cap and ttm["ttm_net_income"] and ttm["ttm_net_income"] > 0) else None
    pb = (market_cap / qual["total_equity"]) if (market_cap and qual["total_equity"] and qual["total_equity"] > 0) else None

    rs_6m = None
    if spy_df is not None and not spy_df.empty:
        # A return: the dividend-adjusted closes (D3), against SPY's adjusted ones.
        own_6m, spy_6m = _ret(adjusted["Close"], 126), _ret(spy_df["Close"], 126)
        # A series under 127 bars has no 126-bar return: no value, never a TypeError (#147 U5).
        rs_6m = own_6m - spy_6m if own_6m is not None and spy_6m is not None else None

    metrics = {
        "rev_growth_ttm": ttm["rev_growth_ttm"],
        "fcf_margin": ttm["fcf_margin"],
        "ro40": ttm["ro40"],
        "ro40_basis": ttm["ro40_basis"],
        "ttm_revenue": ttm["ttm_revenue"],
        "gross_margin": qual["gross_margin"],
        "roic": qual["roic"],
        "net_debt_ebitda": qual["net_debt_ebitda"],
        "pe": pe,
        "pb": pb,
        "dist_52w_pct": ind.get("within_52w_high_pct"),
        "rs_6m": rs_6m,
        "above_200d": (price > ind["sma200"]) if (price is not None and ind.get("sma200") is not None) else None,
    }

    vendor_industry = info.get("industry") or ""
    # R5 (#88), #266: the sector and industry are the spine's (UW's two levels); the vendor ``.info`` ones never
    # fill the row's own fields. The theme keyword matcher keeps the vendor's free-text industry. ``gics`` is read
    # above, before EDGAR (#386 F1: the sector gate).
    name = info.get("longName") or info.get("shortName") or row.get("name") or ""
    themes = assign_themes(gics.sector, vendor_industry, name,
                           info.get("longBusinessSummary") or "", ticker=ticker)

    # #386 F1: the anchor year's period end, the date the Quality/Quality-growth figures are read from.
    ai = fiscal_year.anchor(annual)
    anchor_end = fiscal_year.period_end(annual[ai]) if ai is not None else None

    return {
        "ticker": ticker, "name": name, "price": price, "market_cap": market_cap,
        "sector": gics.sector, "industry": gics.industry,
        "themes": themes,
        "earnings_date": _to_float((row.get("quote") or {}).get("earningsTimestampStart")),
        "period": _period_label(quarterly, annual),
        "anchor_period_end": anchor_end.isoformat() if anchor_end else None,
        "metrics": metrics,
        "ohlc_stale": bool(df.attrs.get("stale")),
        "short_refetch_refused": bool(df.attrs.get("short_refetch_refused")),
        "info_stale": info_stale,
        "edgar_extract_version": (emeta or {}).get("extract_version"),     # the result history checks it (#278)
    }


# The F1 data-health drops (#386): ``run_screen`` counts every unscored name as ``data_short`` ("data<252d"), so the
# summary takes these out and names them (#386 review).
HEALTH_DROPS = ("sector_excluded", "edgar_fetch_failed", "stale_annual", "ro40_barrier")


def _health_drops(run_stats: dict) -> dict:
    dropped = run_stats.get("dropped") or {}
    return {k: int(dropped.get(k) or 0) for k in HEALTH_DROPS}


def _data_short(run_stats: dict):
    short = run_stats.get("data_short")
    return None if short is None else max(0, short - sum(_health_drops(run_stats).values()))


def build_payload(results: list[dict], top_n: int | None = None, run_stats: dict = None) -> dict:
    """The board payload. Every candidate is kept (card 6/7, #383 R3 C12); ``top_n`` cuts only when given."""
    run_stats = run_stats or {}
    results = results or []
    run_stats["stale_info"] = sum(1 for r in results if r.get("info_stale"))
    run_stats["short_refetch_refused"] = sum(1 for r in results if r.get("short_refetch_refused"))

    passing = {"rule_of_40": 0, "quality": 0, "momentum": 0}
    distorted_count = 0
    candidates = []
    for r in results:
        m = r["metrics"]
        gates = {
            "rule_of_40": evaluate_rule_of_40(m),
            "quality": evaluate_quality(m),
            "momentum": evaluate_momentum(m),
        }
        # #386 F2: a candidate passes at least one rule; each carries the rules it passed (fixed order) rather than
        # an opaque 3x/2x/1x multiplier.
        rules_passed = [k for k in passing if gates[k]["passed"]]
        if not rules_passed:
            continue
        passed_count = len(rules_passed)
        for k in rules_passed:
            passing[k] += 1
        distorted = is_distorted(m)
        if distorted:
            distorted_count += 1
        candidates.append({
            "ticker": r["ticker"], "name": r["name"], "price": r["price"],
            "market_cap": r["market_cap"], "sector": r["sector"], "industry": r["industry"],
            "themes": r["themes"], "earnings_date": r["earnings_date"], "period": r["period"],
            "rules_passed": rules_passed,
            "passed_count": passed_count,
            "bucket": "+".join(rules_passed),
            "quality_score": gates["quality"]["score"],
            "momentum_score": gates["momentum"]["score"],
            "ro40": m.get("ro40"), "ro40_basis": m.get("ro40_basis"),
            "rev_growth_ttm": m.get("rev_growth_ttm"), "fcf_margin": m.get("fcf_margin"),
            "anchor_period_end": r.get("anchor_period_end"),
            "pe": m.get("pe"), "pb": m.get("pb"), "roic": m.get("roic"),
            "distorted": distorted,
            "info_stale": bool(r.get("info_stale")),
            "gates": gates,
        })

    # #386 F2: rules passed desc, then DISTORTED last at an equal count, then Rule of 40 desc (None last), then ticker.
    candidates.sort(key=lambda c: (-c["passed_count"], c["distorted"],
                                   -c["ro40"] if c["ro40"] is not None else float("inf"), c["ticker"]))
    run_stats["passed_screens"] = len(candidates)       # before any top_n cut (the C16 funnel)
    if top_n:
        candidates = candidates[:top_n]
    annotate_cards(candidates)

    regime = run_stats.get("regime")
    return {
        "generated_at": _now(),
        "data_as_of": run_stats.get("data_as_of"),
        "regime": regime,
        "universe_total": run_stats.get("raw_universe", len(results)),
        "universe_generated_at": run_stats.get("universe_generated_at"),
        "universe_refreshed_by": run_stats.get("universe_refreshed_by"),
        "universe_partial": bool(run_stats.get("universe_partial")),
        "passing": passing,
        "selection_version": config.SELECTION_VERSION,
        "distorted_count": distorted_count,
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
            "data_short": _data_short(run_stats),
            "no_edgar": run_stats.get("no_edgar"),
            "health_drops": _health_drops(run_stats),
            "passing": passing,
            "distorted_count": distorted_count,
            "top_n": len(candidates),
        },
        "candidates": candidates,
    }


def demo_payload() -> dict:
    return {
        "generated_at": _now(),
        "regime": "RISK-ON",
        "universe_total": 0,
        "universe_generated_at": None,
        "universe_refreshed_by": None,
        "passing": {"rule_of_40": 0, "quality": 0, "momentum": 0},
        "selection_version": config.SELECTION_VERSION,
        "distorted_count": 0,
        "data_as_of": None,
        "is_fixture": True,
        "summary": {
            "regime": "RISK-ON", "spy_price": None, "spy_sma200": None, "spy_3m_return": None,
            "filtered_universe": 0, "out_of_universe": 0, "hygiene": 0, "data_short": 0,
            "passing": {"rule_of_40": 0, "quality": 0, "momentum": 0},
            "distorted_count": 0, "top_n": 0,
        },
        "candidates": [],
    }


def run_scan(uw=None, top_n: int = None, refresh_universe: bool = False,
             no_fetch: bool = False, rows_sink: dict | None = None) -> dict:
    """End-to-end scan; returns the JSON payload. The shared body is ``core.screen_common.run_screen``.

    ``rows_sink`` (a dict) receives the rows a Fetch persisted (``scores.persist``), for the result history; a
    Refresh persists nothing and leaves it empty. ``no_fetch=True`` (a Refresh) scores nothing: it returns the payload
    the last Fetch recorded (K4, ``screen_common.stored_payload``)."""
    from . import scores
    return screen_common.run_screen(
        board="fundamental", refreshed_by="Fundamentals", cfg=config,
        load_universe=fetch_universe_stats, prefilter=prefilter,
        load_spy=lambda: screen_common.load_spy(lambda: load_ohlc("SPY", ttl_days=0.5, no_fetch=no_fetch)),
        compute_row=_compute_row, build_payload=build_payload, persist=scores.persist if rows_sink is None else (lambda results: scores.persist(results, sink=rows_sink)),
        top_n=top_n, refresh_universe=refresh_universe, no_fetch=no_fetch,
        history_board="module_fundamentals", saved=config.FUNDAMENTALS_JSON)


def save_payload(payload: dict) -> str:
    write_artifact(config.FUNDAMENTALS_JSON, json.dumps(payload, ensure_ascii=False))
    return str(config.FUNDAMENTALS_JSON)
