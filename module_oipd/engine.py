"""Implied Probability (OIPD) engine — stored chain -> SVI surface -> risk-neutral PDF.

G9 (build plan PR 19): the chain is the options store's ``option_eod`` (``rcdata.read.option_chain``), which the
builder's ``uw/option_list`` fills every evening for SPX and the option watch list. Any other name shows "not on the
option watch list" and gets no fit. Fetch and Refresh fit the same stored chain with **zero network calls**; a Fetch
also records the run (``history.py``) and, in the runner, queues ``uw/option_list`` (D1).

The fit itself is pure numpy/scipy (``opd_math``) and deterministic, so a Refresh reproduces the Fetch board exactly.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

import numpy as np

from RevenueChaser.core import calendar
from RevenueChaser.core.config import to_float

from . import config, opd_math, uw_chain

logger = logging.getLogger(__name__)

DATASET = "uw/option_list"
SOURCE = "options store (uw/option_list)"
REQUESTED_BY = "oipd"
NOT_ON_LIST = "not on the option watch list"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    """The latest complete session: the ``as_of`` of a board with no stored chain (#382)."""
    return calendar.latest_complete_session().isoformat()


# ── the stored chain ─────────────────────────────────────────────────────────────────────────────────────────────
def list_names() -> list[str]:
    """SPX and the option watch list (``rcdata/build/options/names.py``: the names ``uw/option_list`` fetches)."""
    from RevenueChaser.rcdata.build.options import names
    try:
        return names.list_names()
    except Exception as exc:  # noqa: BLE001 - an unreadable ops.sqlite still leaves SPX
        logger.warning("oipd: option watch list not read: %s", exc)
        return list(names.ALWAYS)


def chain_rows(ticker: str, contracts: list[dict], meta: dict) -> list[dict]:
    """The chain a run fitted, as its result history (``oipd_chain_history``) keeps it."""
    fetched = meta["fetched_at"]
    return [{
        "ticker": ticker,
        "option_symbol": c["option_symbol"],
        "expiry": c["expiry"], "option_type": c["option_type"],
        "strike": c["strike"], "bid": c["bid"], "ask": c["ask"],
        "last_price": c["last_price"], "volume": c["volume"],
        "open_interest": c["open_interest"], "iv": c["iv"],
        "delta": c["delta"], "tape_time": c.get("tape_time"),
        "fetched_at": fetched,
    } for c in contracts]


def _contract(r: dict) -> dict:
    """One ``option_eod`` row in the shape the fit reads (``option_type`` ``C``/``P``, ISO expiry)."""
    bid, ask, last = to_float(r.get("nbbo_bid")), to_float(r.get("nbbo_ask")), to_float(r.get("last_price"))
    return {
        "option_symbol": r.get("option_symbol"),
        "expiry": str(r.get("expiry"))[:10], "option_type": "C" if r.get("option_type") == "call" else "P",
        "strike": to_float(r.get("strike")), "bid": bid, "ask": ask, "last_price": last,
        "mid": uw_chain.mid_price(bid, ask, last),
        "iv": to_float(r.get("implied_volatility")), "open_interest": to_float(r.get("open_interest")),
        "volume": to_float(r.get("volume")), "delta": to_float(r.get("delta")),
        "tape_time": r.get("last_tape_time"),
    }


def _pick_expiries(contracts: list[dict], asof: str) -> set[str]:
    """The expiries to fit: DTE in ``[MIN_DTE, MAX_DTE]``, the ``MAX_EXPIRIES`` with the most open interest."""
    oi: dict[str, float] = {}
    for c in contracts:
        oi[c["expiry"]] = oi.get(c["expiry"], 0.0) + (c["open_interest"] or 0.0)
    rows = [{"expiry": e, "open_interest": v} for e, v in oi.items()]
    return {p["expiry"] for p in uw_chain.expiries_from_breakdown(
        rows, asof, config.MIN_DTE, config.MAX_DTE, config.MAX_EXPIRIES)}


def store_chain(ticker: str) -> tuple[list[dict], dict | None]:
    """``(contracts, meta)`` of ``ticker``'s newest stored session; ``([], None)`` when the store has none.

    The spot is that session's ``option_daily`` close (None when it is missing: the forward then comes from the
    chain). ``fetched_at`` is the newest row's, as ISO."""
    from RevenueChaser.rcdata import read
    rows = read.option_chain(ticker)
    if not rows:
        return [], None
    session = rows[0]["session_date"]
    asof = session.isoformat() if isinstance(session, date) else str(session)[:10]
    daily = read.option_daily(ticker, session, session)
    spot = to_float(daily[0].get("close")) if daily else None
    contracts = [c for c in map(_contract, rows) if c["strike"] and c["strike"] > 0]
    keep = _pick_expiries(contracts, asof)
    contracts = [c for c in contracts if c["expiry"] in keep]
    stamps = [to_float(r.get("fetched_at")) for r in rows if to_float(r.get("fetched_at")) is not None]
    fetched = (datetime.fromtimestamp(max(stamps), timezone.utc).isoformat(timespec="seconds")
               if stamps else None)
    meta = {"ticker": ticker, "as_of": asof, "spot": spot, "risk_free": config.RISK_FREE_RATE,
            "expiry_count": len({c["expiry"] for c in contracts}), "horizon_days": config.MAX_DTE,
            "fetched_at": fetched}
    return contracts, meta


def last_ticker() -> str | None:
    """The ticker the board last showed (its JSON), for a Refresh with no ticker in the box."""
    try:
        return (json.loads(config.OIPD_JSON.read_text(encoding="utf-8")).get("ticker") or None)
    except (OSError, ValueError, AttributeError):
        return None


def builder_request() -> str:
    """Queue one ``uw/option_list`` unless one is queued or running; the board's one-line note.

    Before the dataset's first run (the server thread's: it ``starts_by_hand``) nothing is queued, so a board press
    is never the store's first write. A locked or broken ``ops.sqlite`` is a note, never a failed Fetch.
    Default chosen, change if avo prefers: the Fetch asks for the whole list's run (the builder skips the names it
    already has for the session and sends UW nothing outside the evening window), not for the picked name alone."""
    from RevenueChaser.rcdata import ops
    try:
        if not ops.ever_built(DATASET):
            state = "not queued: it has not had its first run yet (the server thread starts it after a backup)"
        else:
            state = "queued" if ops.enqueue_once(DATASET, None, by=REQUESTED_BY) is not None else "already queued"
    except Exception as exc:  # noqa: BLE001 - the board renders what is stored either way
        logger.warning("oipd: builder request for %s not queued: %s", DATASET, exc)
        state = f"not queued ({type(exc).__name__}: {exc})"[:200]
    return f"Builder request: {DATASET} {state}."


# ── fit ─────────────────────────────────────────────────────────────────────
def _curve_view(curve: opd_math.ProbCurve, n: int = 121) -> dict:
    """Down-sample the density/CDF to a display window around the bulk of mass."""
    lo = curve.quantile(0.005)
    hi = curve.quantile(0.995)
    mask = (curve.S >= lo) & (curve.S <= hi)
    S, pdf, cdf = curve.S[mask], curve.price_pdf[mask], curve.cdf[mask]
    if S.size > n:
        idx = np.linspace(0, S.size - 1, n).round().astype(int)
        S, pdf, cdf = S[idx], pdf[idx], cdf[idx]
    return {
        "S": [round(float(v), 4) for v in S],
        "pdf": [round(float(v), 8) for v in pdf],
        "cdf": [round(float(v), 6) for v in cdf],
    }


def _smile_view(contracts: list[dict], params: dict, F: float, T: float, r: float,
                n: int = 121) -> dict:
    k_mkt, iv_mkt = [], []
    for c in contracts:
        if c["strike"] <= 0:
            continue
        # The marker is the fit's own solved IV (the price's, not the vendor's) so the dots and
        # the fitted curve answer the same question (#382).
        iv = uw_chain.contract_iv(c, F, T, r)
        if iv is None or not (0.0 < iv < 5.0):
            continue
        k_mkt.append(float(np.log(c["strike"] / F)))
        iv_mkt.append(float(iv))
    if k_mkt:
        lo, hi = min(k_mkt), max(k_mkt)
    else:
        lo, hi = -0.5, 0.5
    k_fit = np.linspace(lo, hi, n)
    svi = {kk: params[kk] for kk in ("a", "b", "rho", "m", "sigma")}
    w = opd_math.svi_w(k_fit, **svi)
    iv_fit = np.sqrt(np.clip(w, 0.0, None) / T) if T > 0 else np.zeros_like(k_fit)
    return {
        "k_mkt": [round(v, 5) for v in k_mkt],
        "iv_mkt": [round(v, 5) for v in iv_mkt],
        "k_fit": [round(float(v), 5) for v in k_fit],
        "iv_fit": [round(float(v), 5) for v in iv_fit],
    }


def _fit_expiry(expiry: str, contracts: list[dict], spot: float, r: float,
                asof: str, svi: dict | None = None) -> tuple[dict | None, str | None]:
    d = uw_chain.dte(asof, expiry)
    if d is None or d <= 0:
        return None, f"{expiry}: no valid DTE"
    T = d / 365.0
    F = uw_chain.infer_forward(contracts, T, r, spot)
    if not np.isfinite(F) or F <= 0:
        return None, f"{expiry}: no forward (no quoted put/call pairs)"

    ks, ws = [], []
    for c in contracts:
        if c["strike"] <= 0:
            continue
        iv = uw_chain.contract_iv(c, F, T, r)
        if iv is None:
            continue
        ks.append(float(np.log(c["strike"] / F)))
        ws.append(iv * iv * T)
    if len(ks) < config.MIN_CONTRACTS:
        return None, f"{expiry}: only {len(ks)} usable strikes (< {config.MIN_CONTRACTS})"

    params = opd_math.fit_svi(ks, ws, g_floor=config.G_FLOOR,
                              penalty_weight=config.PENALTY_WEIGHT)
    if params is None:
        return None, f"{expiry}: SVI fit failed"
    if svi is not None:                        # the fitted parameters, for the result history (#382)
        svi[expiry] = {k: float(params[k]) for k in ("a", "b", "rho", "m", "sigma")}
    # A negative g_min is not fatal — the board renders and flags it in the
    # skew KPI — so the expiry is kept rather than dropped.
    curve = opd_math.ProbCurve(expiry, T, F, params, r)
    st = curve.stats()
    atm = opd_math.atm_iv(params, T)
    greeks = opd_math.atm_greeks(params, T, r, F)
    # With no spot (no stored close for the session) the fit stands on the
    # chain's forward; only the spot-relative probabilities are unknown.
    if spot is not None:
        up10, dn10 = spot * 1.10, spot * 0.90
        spot_probs = {
            "prob_below_spot": round(curve.prob_below(spot), 4),
            "prob_above_spot": round(curve.prob_above(spot), 4),
            "p_up10": round(curve.prob_above(up10), 4),
            "p_down10": round(curve.prob_below(dn10), 4),
        }
    else:
        spot_probs = dict.fromkeys(("prob_below_spot", "prob_above_spot", "p_up10", "p_down10"))
    entry = {
        "date": expiry, "dte": d, "T": round(T, 6),
        "forward": round(F, 4), "n": len(ks),
        "atm_iv": round(atm, 4) if atm else None,
        "g_min": round(params["g_min"], 5),
        "g_min_window": round(curve.g_min_window, 5),
        "clipped_pct": round(100.0 * curve.clipped_mass, 3),
        "min_var": round(curve.min_var, 6),
        **{f"density_{k}": v for k, v in opd_math.density_check(
            params["g_min"], curve.g_min_window, 100.0 * curve.clipped_mass, curve.min_var,
            config.CLIP_FLAG_PCT).items()},
        "rmse": round(params["rmse"], 6),
        "mean": round(st["mean"], 4),
        "quantiles": {kk: round(st[kk], 4) for kk in
                      ("p05", "p10", "p25", "median", "p75", "p90", "p95")},
        **spot_probs,
        "greeks": {kk: round(float(v), 6) for kk, v in greeks.items()},
        "curve": _curve_view(curve),
        "smile": _smile_view(contracts, params, F, T, r),
    }
    return entry, None


def _fit_payload(ticker: str, contracts: list[dict], meta: dict,
                 warnings: list[str], svi: dict | None = None) -> dict:
    spot = meta.get("spot")
    r = meta.get("risk_free") or config.RISK_FREE_RATE
    asof = meta.get("as_of") or _today()

    by_expiry: dict[str, list[dict]] = {}
    for c in contracts:
        by_expiry.setdefault(c["expiry"], []).append(c)

    entries, curves = [], []
    for expiry in sorted(by_expiry):
        entry, err = _fit_expiry(expiry, by_expiry[expiry], spot, r, asof, svi)
        if err:
            warnings.append(err)
            continue
        entries.append(entry)

    payload = {
        "ticker": ticker,
        "asof": asof,
        "spot": spot,
        "risk_free": r,
        "source": SOURCE,
        "on_list": True,
        "generated_at": _now(),
        "fetched_at": meta.get("fetched_at"),
        "horizon_days": meta.get("horizon_days"),
        "n_contracts": len(contracts),
        "expiries": entries,
        "fan": None,
        "warnings": warnings,
        "error": None,
    }
    if entries:
        # Fan chart from the fitted curves (quantiles across expiries).
        fan = {"expiries": [e["date"] for e in entries],
               "dte": [e["dte"] for e in entries],
               "quantiles": {
                   q: [e["quantiles"][k] for e in entries]
                   for q, k in (("0.05", "p05"), ("0.25", "p25"),
                                ("0.50", "median"), ("0.75", "p75"),
                                ("0.95", "p95"))},
               "spot_ref": [e["forward"] for e in entries]}
        payload["fan"] = fan
    elif not payload["error"]:
        payload["error"] = (
            f"No expiry could be fitted for {ticker}."
            + ((" " + "; ".join(warnings)) if warnings else "")
        )
    return payload


def _empty_payload(ticker: str, message: str, on_list: bool | None = None) -> dict:
    return {
        "ticker": ticker, "asof": _today(), "spot": None,
        "risk_free": config.RISK_FREE_RATE, "source": SOURCE, "on_list": on_list,
        "generated_at": _now(), "fetched_at": None, "horizon_days": None,
        "n_contracts": 0, "expiries": [], "fan": None,
        "warnings": [], "error": message,
    }


def run(uw=None, ticker: str | None = None, no_fetch: bool = False, capture: dict | None = None) -> dict:
    """Fit the risk-neutral surface for ``ticker`` from the options store (Fetch and Refresh alike, no network).

    ``uw`` is unused (G9: the builder fetches the chain). A Refresh with no ticker shows the board's last one. A
    Fetch that passes ``capture`` (the result history, #382) gets what the fit stood on: ``status`` ``ok``, no
    ``errors``, the ``chain`` rows, the ``meta``, each fitted expiry's ``svi`` parameters and ``candle_error`` None.
    A Refresh never fills it.
    """
    ticker = (ticker or (last_ticker() if no_fetch else None) or config.DEFAULT_TICKER).strip().upper()
    names = list_names()
    if ticker not in names:
        return _empty_payload(
            ticker, f"{ticker} is {NOT_ON_LIST} (" + ", ".join(names) + "), so it gets no fit. Add it with the "
            f"option-list button; its chain is fetched with the next {DATASET} run, after the close.", False)
    contracts, meta = store_chain(ticker)
    if not contracts:
        return _empty_payload(
            ticker, f"No stored option chain for {ticker} yet: it fills with the next {DATASET} run, after the "
            "close.", True)
    warnings: list[str] = []
    if meta["spot"] is None:
        warnings.append("No spot price available; the forward falls back to the chain.")
    svi: dict | None = None
    if capture is not None and not no_fetch:            # a Refresh never records
        svi = {}
        capture.update(status="ok", errors=[], meta=dict(meta), svi=svi,
                       chain=chain_rows(ticker, contracts, meta), candle_error=None)
    return _fit_payload(ticker, contracts, meta, warnings, svi)
