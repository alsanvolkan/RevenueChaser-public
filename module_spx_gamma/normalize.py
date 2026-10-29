"""Normalization: raw UW payloads -> the SPX critical thresholds schema."""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import gex_profile

from .charm_pin import (
    MODE_DIRECTIONALIZED_VOLUME,
    MODE_OPEN_INTEREST,
    normalize_spot_exposure_rows,
    qualify_charm,
    vanna_peaks,
)
from .config import TICKER, net_sum, to_float
from .chains import normalize_option_chain
from .engines.iron_butterfly import build_iron_butterfly
from .engines.prediction_engine import generate_prediction_matrix
from .expected_move import expected_move, pick_iv
from .expiry_table import normalize_expiry_rows
from .levels import (
    band_walls,
    cumulative_gex_crossing,
    cumulative_gex_envelope,
    intraday_concentration,
    intraday_put_concentration,
    map_core_levels,
    map_flip_levels,
    map_volume_levels,
)
from .summary import charm_live_sum, vanna_at_spot


def normalize_strike_rows(payload: dict | None) -> list[dict]:
    """Per-strike static greek exposure from /greek-exposure/strike."""
    if not payload or "data" not in payload:
        return []
    rows = []
    for r in payload["data"]:
        rows.append({
            "strike": to_float(r.get("strike")),
            "call_gex": to_float(r.get("call_gex")),
            "put_gex": to_float(r.get("put_gex")),
            "net_gamma": net_sum(r.get("call_gex"), r.get("put_gex")),
            "call_delta": to_float(r.get("call_delta")),
            "put_delta": to_float(r.get("put_delta")),
            "net_delta": net_sum(r.get("call_delta"), r.get("put_delta")),
        })
    return rows


def enrich_strike_gamma_dollars(strike_rows: list[dict], spot_exposure_rows: list[dict]) -> list[dict]:
    """Attach dollar-denominated OI/vol gamma to strike rows (spot-exposures scale).

    The predictive engine scores divergence (OI vs volume walls) in dollar terms,
    which only /spot-exposures/strike provides. Strikes without a spot-exposure
    match keep None dollar fields.
    """
    lookup = {}
    for r in spot_exposure_rows:
        strike = r.get("strike")
        if strike is not None:
            lookup.setdefault(strike, r)
    out = []
    for row in strike_rows:
        se = lookup.get(row.get("strike"))
        out.append({
            **row,
            "call_gamma_oi": se.get("call_gamma_oi") if se else None,
            "put_gamma_oi": se.get("put_gamma_oi") if se else None,
            "call_gamma_vol": se.get("call_gamma_vol") if se else None,
            "put_gamma_vol": se.get("put_gamma_vol") if se else None,
            "dollar_gamma_oi": net_sum((se or {}).get("call_gamma_oi"), (se or {}).get("put_gamma_oi")),
            "dollar_gamma_vol": net_sum((se or {}).get("call_gamma_vol"), (se or {}).get("put_gamma_vol")),
        })
    return out


def normalize_strike_expiry_rows(strike_expiry_payloads: dict | list | None, asof_date: str | None = None) -> list[dict]:
    """Per-(strike, expiry) rows from /greek-exposure/strike-expiry.

    ``dte`` is taken from the payload when present, else derived from the expiry
    date against ``asof_date`` (when provided).
    """
    if isinstance(strike_expiry_payloads, dict):
        payloads = list(strike_expiry_payloads.values())
    else:
        payloads = strike_expiry_payloads or []
    rows = []
    seen: set[tuple[str | None, float | None]] = set()
    for payload in payloads:
        if not payload or "data" not in payload:
            continue
        for r in payload["data"]:
            expiry = r.get("expiry")
            strike = to_float(r.get("strike"))
            # The same (expiry, strike) can arrive twice: the dedicated 0DTE
            # endpoint and the per-expiry file for the same date are identical.
            # Keep the first row so the grid is never double-counted.
            key = (expiry, strike)
            if key in seen:
                continue
            seen.add(key)
            dte = to_float(r.get("dte"))
            if dte is None and expiry and asof_date:
                try:
                    dte = float((date.fromisoformat(expiry) - date.fromisoformat(asof_date)).days)
                except ValueError:
                    dte = None
            rows.append({
                "expiry": expiry,
                "dte": dte,
                "strike": strike,
                "call_gex": to_float(r.get("call_gex")),
                "put_gex": to_float(r.get("put_gex")),
                "net_gamma": net_sum(r.get("call_gex"), r.get("put_gex")),
            })
    return rows


def normalize_flow_strikes(payload: dict | None, spot: float | None,
                           band_pct: float = 0.03) -> list[dict]:
    """Strike-level intraday flow (volume/premium), filtered to ``spot ± band_pct``.

    ``/flow-per-strike`` returns ~500 strikes; near-the-money rows are what the
    Confluence/Fakeout signal engine needs, so far-OTM strikes are dropped to keep
    the snapshot compact. Returns [] on missing data.
    """
    if not payload or spot is None:
        return []
    data = payload.get("data", []) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get("data", [])
    rows = []
    lo = spot * (1 - band_pct)
    hi = spot * (1 + band_pct)
    for r in data:
        strike = to_float(r.get("strike"))
        if strike is None or not (lo <= strike <= hi):
            continue
        cv = to_float(r.get("call_volume"))
        pv = to_float(r.get("put_volume"))
        cva = to_float(r.get("call_volume_ask_side"))
        cvb = to_float(r.get("call_volume_bid_side"))
        rows.append({
            "strike": strike,
            "call_volume": cv,
            "put_volume": pv,
            "call_premium": to_float(r.get("call_premium")),
            "put_premium": to_float(r.get("put_premium")),
            "call_volume_ask_side": cva,
            "call_volume_bid_side": cvb,
            "call_trades": to_float(r.get("call_trades")),
            "put_trades": to_float(r.get("put_trades")),
            "call_otm_volume": to_float(r.get("call_otm_volume")),
            "put_otm_volume": to_float(r.get("put_otm_volume")),
            "ask_side_ratio": (cva / cv) if (cv and cva is not None) else None,
            "bid_side_ratio": (cvb / cv) if (cv and cvb is not None) else None,
        })
    rows.sort(key=lambda r: r["strike"])
    return rows


def normalize_candles(ohlc_payload: dict | None) -> list[dict]:
    if not ohlc_payload:
        return []
    data = ohlc_payload.get("data", []) if isinstance(ohlc_payload, dict) else ohlc_payload
    rows = []
    for c in data:
        rows.append({
            "date": c.get("date"),
            "open": to_float(c.get("open")),
            "high": to_float(c.get("high")),
            "low": to_float(c.get("low")),
            "close": to_float(c.get("close")),
            "volume": to_float(c.get("volume")),
        })
    rows.sort(key=lambda r: r["date"] or "")
    return rows


def normalize_max_pain(payload: dict | None, spot: float | None) -> list[dict]:
    """Max pain per expiry with distance from spot (pct)."""
    if not payload or "data" not in payload:
        return []
    rows = []
    for r in payload["data"]:
        mp = to_float(r.get("max_pain"))
        rows.append({
            "expiry": r.get("expiry"),
            "max_pain": mp,
            "distance_pct": ((mp - spot) / spot * 100) if (mp is not None and spot) else None,
        })
    return rows


def normalize_iv_rank(payload: dict | None) -> dict | None:
    """Latest IV rank + ATM IV from /iv-rank (series, oldest-first)."""
    if not payload or "data" not in payload:
        return None
    rows = [r for r in payload["data"] if to_float(r.get("iv_rank_1y")) is not None]
    if not rows:
        return None
    latest = rows[-1]
    return {
        "date": latest.get("date"),
        "iv_rank_1y": to_float(latest.get("iv_rank_1y")),
        "volatility": to_float(latest.get("volatility")),
    }


def normalize_options_volume(payload: dict | None) -> dict | None:
    """Latest options volume + P/C ratio from /options-volume."""
    if not payload or "data" not in payload:
        return None
    rows = [r for r in payload["data"] if to_float(r.get("call_volume")) is not None]
    if not rows:
        return None
    latest = rows[-1]
    cv = to_float(latest.get("call_volume"))
    pv = to_float(latest.get("put_volume"))
    return {
        "date": latest.get("date"),
        "call_volume": cv,
        "put_volume": pv,
        "pc_ratio": (pv / cv) if (cv and pv is not None) else None,
        "net_call_premium": to_float(latest.get("net_call_premium")),
        "net_put_premium": to_float(latest.get("net_put_premium")),
    }


def normalize_vol_term(payload: dict | None) -> list[dict]:
    """Vol term structure by expiry (vol + implied move)."""
    if not payload or "data" not in payload:
        return []
    rows = []
    for r in payload["data"]:
        rows.append({
            "expiry": r.get("expiry"),
            "dte": to_float(r.get("dte")),
            "volatility": to_float(r.get("volatility")),
            "implied_move_perc": to_float(r.get("implied_move_perc")),
        })
    rows.sort(key=lambda r: r["dte"] if r["dte"] is not None else 1e9)
    return rows


def normalize_vrp(payload: dict | None) -> dict | None:
    """Latest variance risk premium (rank + premium) — series oldest-first."""
    if not payload or "data" not in payload:
        return None
    rows = [r for r in payload["data"] if to_float(r.get("risk_premium")) is not None]
    if not rows:
        return None
    latest = rows[-1]
    return {
        "date": latest.get("date"),
        "risk_premium": to_float(latest.get("risk_premium")),
        "rank": to_float(latest.get("rank")),
    }


def normalize_realized_vol(payload: dict | None) -> dict | None:
    """Latest realized vol vs implied vol spread — series oldest-first."""
    if not payload or "data" not in payload:
        return None
    rows = [r for r in payload["data"]
            if to_float(r.get("realized_volatility")) is not None
            and to_float(r.get("implied_volatility")) is not None]
    if not rows:
        return None
    latest = rows[-1]
    iv = to_float(latest.get("implied_volatility"))
    rv = to_float(latest.get("realized_volatility"))
    return {
        "date": latest.get("date"),
        "implied_volatility": iv,
        "realized_volatility": rv,
        "iv_minus_rv": (iv - rv) if (iv is not None and rv is not None) else None,
    }


def normalize_skew(iv_payload: dict | None) -> dict | None:
    """Put bias (skew) derived from the interpolated-iv put/call blocks.

    UW's /interpolated-iv returns two identically-tenored blocks (18 rows =
    9 tenors x 2 blocks). We treat one as the call block and the other as the
    put block; the put/call assignment is NOT documented by UW, so the result
    is flagged ``derived_unverified: True``. Put bias = put block IV - call
    block IV at the 30d and 90d tenors.
    """
    if not iv_payload or "data" not in iv_payload:
        return None
    data = iv_payload["data"]
    if len(data) < 2:
        return None
    tenors: dict[float, list[float]] = {}
    for r in data:
        days = to_float(r.get("days"))
        vol = to_float(r.get("volatility"))
        if days is None or vol is None:
            continue
        tenors.setdefault(days, []).append(vol)

    def _pair(days):
        v = tenors.get(days) or []
        return (v[0], v[1]) if len(v) >= 2 else (None, None)

    call30, put30 = _pair(30)
    call90, put90 = _pair(90)
    skew30 = (put30 - call30) if (call30 is not None and put30 is not None) else None
    skew90 = (put90 - call90) if (call90 is not None and put90 is not None) else None
    if skew30 is None and skew90 is None:
        return None
    return {
        "put_bias_1m": skew30,
        "put_bias_3m": skew90,
        "derived_unverified": True,
        "note": "Derived from UW /interpolated-iv twin blocks; the put/call block "
                "assignment is not documented by UW and is unverified.",
    }


def normalize_vix(vix_payload: dict | None) -> dict | None:
    """VIX family snapshot (yfinance) with term mode."""
    if not vix_payload or "tickers" not in vix_payload:
        return None
    tk = vix_payload.get("tickers", {})
    return {
        "vix": to_float(tk.get("^VIX")),
        "vix9d": to_float(tk.get("^VIX9D")),
        "vix3m": to_float(tk.get("^VIX3M")),
        "vix6m": to_float(tk.get("^VIX6M")),
        "vix1y": to_float(tk.get("^VIX1Y")),
        "vvix": to_float(tk.get("^VVIX")),
        "term_mode": vix_payload.get("term_mode"),
    }


def _report_day(value) -> str | None:
    """The COT report date as a plain ISO day, whatever the source spelled it as.

    Socrata serves ``"2026-10-06T00:00:00.000"`` and the stored row (D29) gives ``"2026-10-06"``; the panel
    renders this string raw, so both sources must read the same on the page (review #103, check 2).
    """
    if value is None:
        return None
    text = str(value).strip()
    return text.split("T", 1)[0] if text else None


def normalize_cftc(cftc_payload: dict | None) -> dict | None:
    """CFTC TFF E-mini row mapped to report shape."""
    if not cftc_payload:
        return None
    rows = []
    for key, label in (("asset_manager", "Asset Manager"),
                       ("leveraged_funds", "Leveraged Funds"),
                       ("dealer", "Dealer")):
        g = cftc_payload.get(key) or {}
        rows.append({
            "group": label,
            "long": g.get("long"),
            "short": g.get("short"),
            "net": g.get("net"),
            "change": g.get("change"),
        })
    return {
        "report_date": _report_day(cftc_payload.get("report_date")),
        "open_interest": cftc_payload.get("open_interest"),
        "rows": rows,
        # the market store's COT (PR 3): no weekly change across a missing report (review SF4)
        **({"change_gap": True} if cftc_payload.get("change_gap") else {}),
    }


def current_level(candles: list[dict]) -> float | None:
    closes = [c["close"] for c in candles if c.get("close") is not None]
    return closes[-1] if closes else None


def _sum_or_none(values) -> float | None:
    """Sum of the non-None values; None when there are none."""
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def normalize_snapshot(raw: dict, asof_date: str, ticker: str = TICKER, mode: str = MODE_OPEN_INTEREST) -> dict:
    asof_str = asof_date.isoformat() if isinstance(asof_date, date) else asof_date

    levels = map_core_levels((raw.get("gex_levels") or {}).get("data", {}))
    flips = map_flip_levels((raw.get("gex_levels") or {}).get("data", {}),
                            (raw.get("gex_levels_vol") or {}).get("data", {}))
    levels["gamma_flip_all"] = flips["gamma_flip_all"]
    levels["gamma_flip_0dte"] = flips["gamma_flip_0dte"]
    levels.update(map_volume_levels((raw.get("gex_levels_vol") or {}).get("data", {})))

    # Full strike-expiry grid (0DTE + all expiries) for the predictive engine:
    # near-dated (0-1 DTE) rows drive the pin/flip/trigger geometry.
    # One normalization over the 0DTE payload and the per-expiry payloads together: the same
    # (expiry, strike) arrives in both for the front expiry, and normalizing them apart and
    # concatenating counted every 0DTE strike twice (G5).
    zero_payloads = list((raw.get("strike_expiry") or {}).values())
    all_payloads = list((raw.get("strike_expiry_all") or {}).values())
    strike_expiry_rows = normalize_strike_expiry_rows(zero_payloads + all_payloads, asof_str)
    all_se_rows = [r for r in strike_expiry_rows if r.get("expiry") in (raw.get("strike_expiry_all") or {})]
    levels["zero_dte_concentration"] = intraday_concentration(strike_expiry_rows, asof_str)
    levels["zero_dte_put_concentration"] = intraday_put_concentration(strike_expiry_rows, asof_str)
    candles = normalize_candles(raw.get("ohlc"))
    # The snapshot records a live spot during an open session (the owner's choice, 2026-10-22); the stored
    # window's last close is the fallback, so a closed-market run is unchanged.
    recorded = (raw.get("spot") or {}).get("value")
    spot = recorded if recorded is not None else current_level(candles)
    levels["zero_dte_cumulative_crossing"] = cumulative_gex_crossing(strike_expiry_rows, asof_str, spot)

    spot_exposure_rows = normalize_spot_exposure_rows(raw.get("spot_exposure"))
    # Charm Peak/Pin is always derived from Mode A (directionalized volume):
    # it reflects active intraday flow, not legacy far-OTM open interest.
    charm_qualification = qualify_charm(spot_exposure_rows, spot, MODE_DIRECTIONALIZED_VOLUME)
    vanna = vanna_peaks(spot_exposure_rows, MODE_DIRECTIONALIZED_VOLUME)

    strike_rows = enrich_strike_gamma_dollars(normalize_strike_rows(raw.get("strike_exposure")),
                                              spot_exposure_rows)
    strike_rows_prev = normalize_strike_rows(raw.get("strike_exposure_prev"))
    expiry_rows = normalize_expiry_rows(raw.get("expiry_exposure"))
    # Sums over rows that may all be missing are None, not 0.0: no data is not a measurement (G12).
    net_gamma_total = _sum_or_none(r.get("net_gamma") for r in strike_rows)
    gex_envelope = cumulative_gex_envelope(strike_rows, spot)
    # Matrix GEX = active-trading gamma (DTE<=45), dropping far-dated/LEAPS noise. A 0DTE row
    # (dte 0) is active trading and stays in; only an unknown dte is left out (G6).
    matrix_gex = _sum_or_none(r.get("net_gex") for r in expiry_rows
                              if r.get("dte") is not None and r["dte"] <= 45)
    flow_rows = normalize_flow_strikes(raw.get("flow_per_strike"), spot)
    # The one regime: spot against the OI gamma flip. The OI/volume flips and the local cumulative
    # crossing are shown side by side; the sign of the summed greek-exposure series (other units)
    # no longer decides anything (G4).
    cumulative_flip = gex_profile.nearest_crossing(
        gex_profile.cumulative_crossings(
            [(r["strike"], net_sum(r.get("call_gamma_oi"), r.get("put_gamma_oi")))
             for r in spot_exposure_rows if r.get("strike") is not None]),
        spot)
    regime_detail = gex_profile.regime(spot, levels.get("regime_threshold"),
                                       levels.get("gamma_flip_vol"), cumulative_flip)
    source_chip = {"long": "POSITIVE", "short": "NEGATIVE"}.get(regime_detail["state"])

    # DTE-band walls (put wall standard = 181-365 day band, e.g. 7500)
    dte_band_walls = band_walls(all_se_rows, spot)
    put_wall_band = next((w for w in dte_band_walls if w["band"] == "181-365"), None)
    if put_wall_band and put_wall_band["put_wall"] is not None:
        levels["lower_support_band"] = put_wall_band["put_wall"]
    levels["put_wall_all"] = next((w["put_wall"] for w in dte_band_walls if w["band"] == "ALL"), None)

    iv_rows = (raw.get("interpolated_iv") or {}).get("data", [])
    annualized_iv = pick_iv(iv_rows)
    em = expected_move(spot, annualized_iv)

    # ── v2 external panels (all optional, degrade to None/[]) ──
    max_pain_rows = normalize_max_pain(raw.get("max_pain"), spot)
    iv_rank = normalize_iv_rank(raw.get("iv_rank"))
    options_volume = normalize_options_volume(raw.get("options_volume"))
    vol_term = normalize_vol_term(raw.get("vol_term_structure"))
    vrp = normalize_vrp(raw.get("variance_risk_premium"))
    realized_vol = normalize_realized_vol(raw.get("realized_vol"))
    skew = normalize_skew(raw.get("interpolated_iv"))
    vix = normalize_vix(raw.get("vix"))
    cftc = normalize_cftc(raw.get("cftc"))

    # The 0-1 DTE dollar gamma per strike the pin and squeeze tests size by (review S4), read from the market store
    # by run.build_day (store_reads.zero_one_dte_dollars). Absent or not stored: no dollars, no pin flag.
    zero_one = raw.get("zero_one_dte") if isinstance(raw.get("zero_one_dte"), dict) else None
    zero_one_dollars = (zero_one.get("dollars") or None) if zero_one and zero_one.get("stored") else None

    core = ("equilibrium_point", "upper_resistance_band", "lower_support_band", "regime_threshold")
    prediction = generate_prediction_matrix({
        "current_level": spot,
        "net_gamma_total": net_gamma_total,
        "regime_detail": regime_detail,
        "spot_exposures": spot_exposure_rows,
        "strike_exposures": strike_rows,
        "strike_expiry_rows": strike_expiry_rows,
        "levels": levels,
        "zero_one_dte_dollars": zero_one_dollars,
    })
    option_chain_rows = normalize_option_chain(raw.get("option_chain"), spot, asof_str)
    iron_butterfly = build_iron_butterfly({
        "current_level": spot,
        "asof_date": asof_str,
        "expected_move": em,
        "prediction": prediction,
        "option_chain": option_chain_rows,
    })
    return {
        "ticker": ticker,
        "asof_date": asof_str,
        "first_session_date": candles[0]["date"] if candles else None,
        "last_session_date": candles[-1]["date"] if candles else None,
        "api_snapshot_time": (raw.get("_meta") or {}).get("api_snapshot_time"),
        "timezone": "America/New_York",
        "candle_source": (raw.get("ohlc") or {}).get("source"),
        "spot_exposure_mode": mode,
        "charm_mode": MODE_DIRECTIONALIZED_VOLUME,
        "vanna_mode": MODE_DIRECTIONALIZED_VOLUME,
        "current_level": spot,
        "regime": regime_detail["label"],
        "regime_detail": regime_detail,
        "net_gamma_total": net_gamma_total,
        "gex_envelope": gex_envelope,
        "levels": levels,
        "dte_band_walls": dte_band_walls,
        "charm_qualification": charm_qualification,
        "vanna_peaks": vanna,
        "spot_exposures": spot_exposure_rows,
        "strike_exposures": strike_rows,
        "strike_exposures_prev": strike_rows_prev,
        "expiry_distribution": expiry_rows,
        "matrix_gex": matrix_gex,
        "source_chip": source_chip,
        "flow_strikes": flow_rows,
        "option_chain": option_chain_rows,
        "iron_butterfly": iron_butterfly,
        "expected_move": em,
        "max_pain": max_pain_rows,
        "iv_rank": iv_rank,
        "options_volume": options_volume,
        "vol_term": vol_term,
        "variance_risk_premium": vrp,
        "realized_vol": realized_vol,
        "skew": skew,
        "vix": vix,
        "cftc": cftc,
        "candles": candles,
        "net_dex": _sum_or_none(r.get("net_delta") for r in strike_rows),
        "net_vanna": (sum(((r.get("call_vanna_vol") or 0) + (r.get("put_vanna_vol") or 0))
                          for r in spot_exposure_rows) if spot_exposure_rows else None),
        "live_charm": (charm_qualification or {}).get("signed_net_charm"),
        # The numbers the Vanna and Charm cards show, so their 1D/1W deltas measure them (DF19).
        "vanna_at_spot": vanna_at_spot(spot_exposure_rows, spot),
        "charm_live_sum": charm_live_sum(spot_exposure_rows),
        "strike_expiry_rows": strike_expiry_rows,
        "prediction": prediction,
        # Where the market-store inputs came from (store_reads.read_day): stored or "not stored", label, source,
        # source_as_of and status per input. Absent for a raw dict built without the store (the goldens).
        "zero_one_dte": ({k: v for k, v in zero_one.items() if k != "dollars"} if zero_one else None),
        "store_inputs": raw.get("store_inputs"),
        "quality": {
            "core_levels_available": all(levels[k] is not None for k in core),
            "charm_available": charm_qualification["strike"] is not None,
            "vanna_available": vanna.get("call_peak") is not None or vanna.get("put_peak") is not None,
            "iv_available": annualized_iv is not None,
            "candle_data_available": bool(candles),
            "max_pain_available": bool(max_pain_rows),
            "vix_available": vix is not None and vix.get("vix") is not None,
            "cftc_available": cftc is not None,
        },
    }


def compute_positioning_deltas(current_val, metric_key, history_data, asof=None) -> dict:
    """Compute 1D (previous record) and 1W (last Friday close) deltas, relative to ``asof``.

    ``history_data`` maps ISO date -> record dicts (as written to
    ``spx_gamma_gex_history.json``). Only records dated before ``asof`` count, so a day's own
    row (written after its render on a Fetch, present on a Refresh) and records newer than the
    day shown never leak into its deltas (G17). 1D uses the newest earlier record; 1W the newest
    earlier record flagged ``is_friday_close``. Without ``asof`` the newest record is taken to be
    the current day.
    """
    sorted_dates = sorted(history_data.keys(), reverse=True)
    if asof is not None:
        earlier = [d for d in sorted_dates if d < str(asof)]
    else:
        earlier = sorted_dates[1:]

    prev_day_key = earlier[0] if earlier else None
    val_1d = history_data[prev_day_key].get(metric_key) if prev_day_key else None
    delta_1d = (current_val - val_1d) if (val_1d is not None and current_val is not None) else None

    friday_key = next((d for d in earlier if history_data[d].get("is_friday_close")), None)
    val_1w = history_data[friday_key].get(metric_key) if friday_key else None
    delta_1w = (current_val - val_1w) if (val_1w is not None and current_val is not None) else None

    return {"val": current_val, "delta_1d": delta_1d, "delta_1w": delta_1w}
