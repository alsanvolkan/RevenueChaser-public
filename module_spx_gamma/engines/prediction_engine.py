"""Predictive scenario & directional engine for the SPX Gamma report.

Maps the normalized snapshot onto a deterministic scenario payload:

- ``S_dir`` composite directional score (spec §4.1) built from dollar-scaled
  spot-exposures data (gamma_oi / charm_vol / vanna_vol), each term normalized
  to [-1, 1] so the weights stay interpretable.
- Scenario extraction (spec §4.2): primary magnet / pin (0-1 DTE), upside
  short-gamma squeeze trigger, downside GEX support floor + vanna bounce floor.
  The pin's $10B test and the trigger's $200M test size a strike by its 0-1 DTE
  dollar OI gamma (``zero_one_dte_dollars``, from the market store; review S4).
- OI vs volume divergence flags (``WALL_DECAYING`` / ``WALL_FORTIFYING``) per
  key strike from dollar gamma_oi vs gamma_vol.

Units note: the strike-expiry grid (``strike_expiry_rows``) carries per-unit
charm/vanna and per-contract net GEX, while ``spot_exposures`` are
dollar-denominated. The score therefore uses the dollar spot-exposures series
(charm/vanna modes already default to directionalized volume in this module),
and the grid supplies the pin/flip/trigger geometry.
"""
from __future__ import annotations

from ..config import (
    CONF_PIN_AT_FULL_DISTANCE,
    CONF_PIN_DOMINANCE_AT_FULL,
    CONF_PIN_WEIGHT,
    CONF_MARGIN_DISTANCE,
    CONSOLIDATION_BAND_PTS,
    PIN_ATM_WINDOW_PTS,
    PIN_MAGNET_MIN_DOLLARS,
    S_DIR_BEARISH,
    S_DIR_BULLISH,
    S_DIR_W_CHARM,
    S_DIR_W_GAMMA,
    S_DIR_W_VANNA,
    TRIGGER_ATM_WINDOW_PTS,
    UPSIDE_TRIGGER_MIN_MAGNITUDE,
    net_sum,
    to_float,
)


def _rows(snapshot: dict, key: str) -> list[dict]:
    return snapshot.get(key) or []


def _near_dte_rows(snapshot: dict, max_dte: float = 1.0) -> list[dict]:
    """Grid rows with DTE <= max_dte (0DTE + 1DTE for pin/trigger geometry).

    DTE is None for the same-day (0DTE) bucket rows that carry no explicit dte;
    they are treated as DTE 0.
    """
    out = []
    for r in _rows(snapshot, "strike_expiry_rows"):
        dte = r.get("dte")
        if dte is None:
            dte = 0.0
        if dte <= max_dte and r.get("net_gamma") is not None:
            out.append(r)
    return out


def _sum_above(rows: list[dict], spot: float, field: str, abs_sum: bool = False) -> float:
    """Signed (or |value|) sum of ``field`` for strikes strictly above spot."""
    total = 0.0
    for r in rows:
        strike = r.get("strike")
        v = to_float(r.get(field))
        if strike is None or v is None or not (strike > spot):
            continue
        total += abs(v) if abs_sum else v
    return total


def _total_abs(rows: list[dict], field: str) -> float:
    return sum(abs(to_float(r.get(field)) or 0.0) for r in rows)


def _dominance(values: list[float]) -> float | None:
    """top / 2nd of a magnitude-ranked series; None when degenerate."""
    vals = sorted((abs(v) for v in values if v is not None), reverse=True)
    if not vals or vals[0] <= 0:
        return None
    if len(vals) < 2 or vals[1] <= 0:
        return None
    return vals[0] / vals[1]


def directional_score(snapshot: dict) -> dict:
    """S_dir per spec §4.1; returns None-safe breakdown when data is thin."""
    spot = snapshot.get("current_level")
    spot_rows = _rows(snapshot, "spot_exposures")
    if spot is None or not spot_rows:
        return {
            "s_dir": None,
            "bias": None,
            "components": None,
            "note": "Insufficient spot-exposure data to score direction.",
        }

    # The gamma term is the regime (spot against the gamma flip), not the sign of the summed
    # /greek-exposure series, which is in other units and often disagreed with the flip (G4).
    # net_gamma_total stays in the components as information only.
    net_gex = snapshot.get("net_gamma_total")
    state = (snapshot.get("regime_detail") or {}).get("state")
    gamma_term = {"long": 1.0, "short": -1.0, "at_flip": 0.0}.get(state)

    charm_above = _sum_above(spot_rows, spot, "call_charm_vol") + _sum_above(spot_rows, spot, "put_charm_vol")
    charm_abs = _total_abs(spot_rows, "call_charm_vol") + _total_abs(spot_rows, "put_charm_vol")
    charm_term = (charm_above / charm_abs) if charm_abs > 0 else None

    vanna_above = _sum_above(spot_rows, spot, "call_vanna_vol") + _sum_above(spot_rows, spot, "put_vanna_vol")
    vanna_abs = _total_abs(spot_rows, "call_vanna_vol") + _total_abs(spot_rows, "put_vanna_vol")
    vanna_term = (vanna_above / vanna_abs) if vanna_abs > 0 else None

    components = {
        "net_gex": net_gex,
        "gamma_term": gamma_term,
        "charm_above": charm_above,
        "charm_term": charm_term,
        "vanna_above": vanna_above,
        "vanna_term": vanna_term,
    }
    if gamma_term is None or charm_term is None or vanna_term is None:
        return {
            "s_dir": None,
            "bias": None,
            "components": components,
            "note": "Insufficient gamma/charm/vanna data to score direction.",
        }

    s_dir = (S_DIR_W_GAMMA * gamma_term
             + S_DIR_W_CHARM * charm_term
             + S_DIR_W_VANNA * vanna_term)
    if s_dir >= S_DIR_BULLISH:
        bias = "BULLISH DRIFT"
    elif s_dir <= S_DIR_BEARISH:
        bias = "BEARISH BREAKDOWN"
    else:
        bias = "CONSOLIDATION / PINNING"
    return {
        "s_dir": round(s_dir, 4),
        "bias": bias,
        "components": components,
        "note": None,
    }


def _dollar_gamma_at(snapshot: dict, strike: float) -> float | None:
    """0-1 DTE dollar OI gamma at a strike: ``call_gamma_oi + put_gamma_oi`` summed over the 0 and 1 DTE expiries of
    ``spx_gamma_expiry_strike`` (``store_reads.zero_one_dte_dollars``, review S4). None when the slice is not stored;
    0.0 for a strike a stored slice has no row for (no 0-1 DTE OI gamma there, review SF1); never the all-expiry
    number."""
    dollars = snapshot.get("zero_one_dte_dollars")
    if not dollars:
        return None
    return dollars.get(strike, 0.0)


def _all_expiry_dollars_at(snapshot: dict, strike: float) -> float | None:
    """All-expiry dollar OI gamma at a strike from spot-exposures: the old basis, kept as context only (A2)."""
    for r in _rows(snapshot, "spot_exposures"):
        if r.get("strike") == strike:
            return net_sum(r.get("call_gamma_oi"), r.get("put_gamma_oi"))
    return None


def _atm_rows(rows: list[dict], spot: float, window_pts: float) -> list[dict]:
    """Grid rows whose strike sits within ``spot ± window_pts``."""
    return [r for r in rows
            if r.get("strike") is not None and abs(r["strike"] - spot) <= window_pts]


def scenario_extraction(snapshot: dict) -> dict:
    """Spec §4.2 ruleset: magnet/pin, upside trigger, downside GEX + vanna floors.

    All geometry is extracted from an ATM window around ``current_level`` so a
    far-away 0-1 DTE GEX peak can never masquerade as the active pin or an
    actionable trigger. The active pin (max positive 0-1 DTE net GEX) is only
    accepted inside ``PIN_ATM_WINDOW_PTS``; when the ATM is short-gamma (no
    positive magnet near spot) the consolidation anchor falls back to the
    equilibrium level so the neutral band stays tied to live spot. The bearish
    breakdown keys off the nearest strong put wall below spot rather than an
    OTM pin the spot has already lost.
    """
    spot = snapshot.get("current_level")
    near_rows = _near_dte_rows(snapshot, max_dte=1.0)
    grid_rows = _rows(snapshot, "strike_expiry_rows")
    spot_rows = _rows(snapshot, "spot_exposures")
    if spot is None or not near_rows:
        return {
            "active_pin": None,
            "pin_source": None,
            "pin_magnet": None,
            "pin_dominance": None,
            "pin_distance_pct": None,
            "expiring_pin_flip": False,
            "pin_flag": None,
            "upside_trigger": None,
            "upside_trigger_net_gex": None,
            "downside_trigger": None,
            "downside_trigger_net_gex": None,
            "gex_floor": None,
            "gex_floor_net_gex": None,
            "vanna_floor": None,
            "vanna_floor_net_vanna": None,
            "note": "Insufficient strike-expiry grid to extract scenarios.",
        }

    # 1. Primary magnet / pin: max positive net GEX in the 0-1 DTE grid, but
    #    only inside the ATM window around spot (global-max picks up far-OTM
    #    peaks that are meaningless to the current price action).
    atm_rows = _atm_rows(near_rows, spot, PIN_ATM_WINDOW_PTS)
    positives = [r for r in atm_rows if (r.get("net_gamma") or 0.0) > 0]
    magnet = max(positives, key=lambda r: r["net_gamma"]) if positives else None

    # Dollar magnitude for the $10B flag: the magnet strike's 0-1 DTE OI gamma (review S4). The all-expiry number
    # rides along as context; a slice that is not stored raises no flag.
    magnet_dollars = _dollar_gamma_at(snapshot, magnet["strike"]) if magnet is not None else None
    magnet_all_expiry = _all_expiry_dollars_at(snapshot, magnet["strike"]) if magnet is not None else None

    # EXPIRING_PIN_FLIP: 1DTE net GEX < 0 at the same strike.
    expiring_flip = False
    if magnet is not None:
        one_dte = [r for r in grid_rows
                   if r.get("dte") == 1 and r.get("strike") == magnet["strike"]]
        if one_dte and (one_dte[0].get("net_gamma") or 0.0) < 0:
            expiring_flip = True

    pin_flag = None
    if magnet is not None:
        if magnet_dollars is not None and abs(magnet_dollars) >= PIN_MAGNET_MIN_DOLLARS:
            pin_flag = "ACTIVE_PIN"
        if expiring_flip:
            pin_flag = "EXPIRING_PIN_FLIP"

    # Consolidation anchor: the ATM magnet when one exists, else the nearest
    # equilibrium/spot level so the neutral band stays on top of price action.
    equilibrium = (snapshot.get("levels") or {}).get("equilibrium_point")
    if magnet is not None:
        anchor = magnet["strike"]
        pin_source = "grid_magnet"
    elif equilibrium is not None and abs(equilibrium - spot) <= PIN_ATM_WINDOW_PTS:
        anchor = equilibrium
        pin_source = "equilibrium_fallback"
    else:
        anchor = round(spot / CONSOLIDATION_BAND_PTS) * CONSOLIDATION_BAND_PTS
        pin_source = "spot_fallback"

    # Pin dominance / proximity (confidence inputs). Dominance only counts for a
    # genuine ATM magnet, never for a fallback anchor.
    near_net = [r["net_gamma"] for r in near_rows]
    pin_dominance = _dominance(near_net) if magnet is not None else None
    pin_distance_pct = (abs(anchor - spot) / spot) if anchor is not None and spot else None

    # 2. Upside acceleration zone: lowest strike above spot with negative GEX
    #    and a 0-1 DTE dollar magnitude > $200M, bounded to the ATM window.
    upside_trigger = None
    upside_net_gex = None
    upside_all_expiry = None
    # avo, 28 Oct 2026: a day whose 0-1 DTE slice is not stored has no squeeze trigger at all; a strike a stored
    # slice has no row for holds $0 and never fires (review SF1). There is no per-unit fallback any more.
    slice_stored = bool(snapshot.get("zero_one_dte_dollars"))
    above = [r for r in atm_rows if r.get("strike") > spot] if slice_stored else []
    for r in sorted(above, key=lambda x: x["strike"]):
        strike = r["strike"]
        if (r["net_gamma"] or 0.0) >= 0:
            continue
        dollars = _dollar_gamma_at(snapshot, strike)
        if abs(dollars) > UPSIDE_TRIGGER_MIN_MAGNITUDE:
            upside_trigger = strike
            upside_net_gex = dollars
            upside_all_expiry = _all_expiry_dollars_at(snapshot, strike)
            break

    # 2b. Downside breakdown trigger: strongest negative-GEX put wall below spot
    #     (dollar OI), skipping strikes still within the consolidation band of
    #     spot itself. This is the level the bearish row keys off.
    downside_trigger = None
    downside_trigger_net_gex = None
    below_walls = [r for r in spot_rows
                   if r.get("strike") is not None
                   and spot - TRIGGER_ATM_WINDOW_PTS <= r["strike"] < spot - CONSOLIDATION_BAND_PTS]
    if below_walls:
        best = min(below_walls, key=lambda r: net_sum(r.get("call_gamma_oi"), r.get("put_gamma_oi")))
        downside_trigger = best["strike"]
        downside_trigger_net_gex = net_sum(best.get("call_gamma_oi"), best.get("put_gamma_oi"))

    # 3a. Downside primary positive-GEX support floor below spot (dollar OI).
    gex_floor = None
    gex_floor_net_gex = None
    supports = [r for r in spot_rows
                if r.get("strike") is not None and r["strike"] < spot
                and (net_sum(r.get("call_gamma_oi"), r.get("put_gamma_oi")) or 0.0) > 0]
    if supports:
        best = max(supports, key=lambda r: net_sum(r.get("call_gamma_oi"), r.get("put_gamma_oi")))
        gex_floor = best["strike"]
        gex_floor_net_gex = net_sum(best.get("call_gamma_oi"), best.get("put_gamma_oi"))

    # 3b. Vanna bounce floor: lower strike cluster where net vanna holds its peak positive value.
    vanna_floor = None
    vanna_floor_net_vanna = None
    positive_vanna = [r for r in spot_rows
                      if r.get("strike") is not None and r["strike"] < spot
                      and (net_sum(r.get("call_vanna_vol"), r.get("put_vanna_vol")) or 0.0) > 0]
    if positive_vanna:
        best = max(positive_vanna, key=lambda r: net_sum(r.get("call_vanna_vol"), r.get("put_vanna_vol")))
        vanna_floor = best["strike"]
        vanna_floor_net_vanna = net_sum(best.get("call_vanna_vol"), best.get("put_vanna_vol"))

    return {
        "active_pin": anchor,
        "pin_source": pin_source,
        "pin_magnet": magnet["strike"] if magnet is not None else None,
        "pin_net_gex": magnet["net_gamma"] if magnet is not None else None,
        "pin_dollars": magnet_dollars,
        "pin_dollars_all_expiry": magnet_all_expiry,
        "pin_dominance": pin_dominance,
        "pin_distance_pct": pin_distance_pct,
        "expiring_pin_flip": expiring_flip,
        "pin_flag": pin_flag,
        "upside_trigger": upside_trigger,
        "upside_trigger_net_gex": upside_net_gex,
        "upside_trigger_dollars_all_expiry": upside_all_expiry,
        "downside_trigger": downside_trigger,
        "downside_trigger_net_gex": downside_trigger_net_gex,
        "gex_floor": gex_floor,
        "gex_floor_net_gex": gex_floor_net_gex,
        "vanna_floor": vanna_floor,
        "vanna_floor_net_vanna": vanna_floor_net_vanna,
        "note": None,
    }


def divergence_analysis(snapshot: dict, limit: int = 6) -> list[dict]:
    """OI vs volume divergence per key strike (spec §4.2.4).

    WALL_DECAYING:   GEX_OI(K) < 0 and GEX_Vol(K) > 0 (incoming positive flow).
    WALL_FORTIFYING: GEX_OI(K) < 0 and GEX_Vol(K) < 0.

    Rows are ranked by proximity to spot (closest first), so the actionable
    near-the-money walls surface ahead of far-away long-dated concentrations.
    """
    spot = snapshot.get("current_level")
    rows = []
    for r in _rows(snapshot, "spot_exposures"):
        strike = r.get("strike")
        if strike is None:
            continue
        g_oi = net_sum(r.get("call_gamma_oi"), r.get("put_gamma_oi"))
        g_vol = net_sum(r.get("call_gamma_vol"), r.get("put_gamma_vol"))
        if g_oi is None or g_vol is None or g_oi >= 0:
            continue
        if g_vol > 0:
            flag = "WALL_DECAYING"
        elif g_vol < 0:
            flag = "WALL_FORTIFYING"
        else:
            continue
        dist = abs(strike - spot) if spot else 1e18
        rows.append({
            "strike": strike,
            "side": ("above" if spot is not None and strike > spot else
                     "below" if spot is not None and strike < spot else "at"),
            "gex_oi": g_oi,
            "gex_vol": g_vol,
            "flag": flag,
            "_dist": dist,
        })
    rows.sort(key=lambda r: (r["_dist"], abs(r["gex_oi"])))
    for r in rows:
        r.pop("_dist", None)
    return rows[:limit]


def confidence(s_dir: float | None, bias: str | None, scen: dict) -> float | None:
    """Heuristic, uncalibrated 0-100 confidence for the directional call.

    Margin share (0-75 pts): how far S_dir sits beyond the ±0.35 band.
    Pin share (0-25 pts): pin dominance ratio and proximity to spot.
    """
    if s_dir is None or bias is None or bias == "CONSOLIDATION / PINNING":
        return None
    margin = abs(s_dir) - S_DIR_BULLISH
    margin_share = min(max(margin / CONF_MARGIN_DISTANCE, 0.0), 1.0)

    pin_share = 0.0
    dom = scen.get("pin_dominance")
    dist = scen.get("pin_distance_pct")
    if dom is not None and dist is not None:
        dom_contrib = min(dom / CONF_PIN_DOMINANCE_AT_FULL, 1.0)
        dist_contrib = 1.0 - min(dist / CONF_PIN_AT_FULL_DISTANCE, 1.0)
        pin_share = (dom_contrib + dist_contrib) / 2.0

    return round(100.0 * ((1.0 - CONF_PIN_WEIGHT) * margin_share + CONF_PIN_WEIGHT * pin_share), 1)


def build_scenario_rows(spot: float | None, scen: dict, call_wall: float | None) -> list[dict]:
    """Three-zone scenario matrix (bullish / neutral / bearish).

    Every band is anchored to spot: the consolidation band is the ATM anchor ±
    ``CONSOLIDATION_BAND_PTS``, the bearish row keys off the nearest put wall
    below spot (never an OTM pin the spot has already lost), and the bullish
    target is the squeeze trigger + 15 pts.
    """
    anchor = scen.get("active_pin")
    trigger = scen.get("upside_trigger")
    down_trigger = scen.get("downside_trigger")
    # A bearish target must lie below the level whose loss triggers it; a floor at or above the
    # trigger is not a target (G11). The vanna floor is preferred, then the GEX floor.
    floor, floor_label = None, ""
    for value, label in ((scen.get("vanna_floor"), "vanna floor"), (scen.get("gex_floor"), "GEX floor")):
        if value is not None and (down_trigger is None or value < down_trigger):
            floor, floor_label = value, label
            break
    band = CONSOLIDATION_BAND_PTS
    target_up = trigger or call_wall or (anchor + band if anchor is not None else None)

    if spot is None:
        return []

    rows = [
        {
            "name": "Bullish Acceleration",
            "class": "row-bullish",
            "trigger": f"Spot holds > {target_up:,.0f}" if target_up is not None else "n/a",
            "hedging": "Short gamma forces dealer buying into strength",
            "target": f"{trigger + 15:,.0f}" if trigger is not None else "n/a",
            "tone": "bullish",
        },
        {
            "name": "Consolidation",
            "class": "row-neutral",
            "trigger": (f"{anchor - band:,.0f} < Spot < {anchor + band:,.0f}"
                        if anchor is not None else "n/a"),
            "hedging": "Positive gamma & charm absorption dampens range",
            "target": f"{anchor:,.0f} Pin" if anchor is not None else "n/a",
            "tone": "neutral",
        },
        {
            "name": "Bearish Breakdown",
            "class": "row-bearish",
            "trigger": f"Spot loses {down_trigger:,.0f}" if down_trigger is not None else "n/a",
            "hedging": "Flipped gamma accelerates the drop through the air pocket",
            "target": f"{floor:,.0f} ({floor_label})" if floor is not None else "n/a",
            "tone": "bearish",
        },
    ]
    return rows


def generate_prediction_matrix(snapshot: dict) -> dict:
    """Executes the structural ruleset; returns the JSON scenario payload."""
    spot = snapshot.get("current_level")
    score = directional_score(snapshot)
    scen = scenario_extraction(snapshot)
    conf = confidence(score.get("s_dir"), score.get("bias"), scen)
    divergence = divergence_analysis(snapshot)

    call_wall = (snapshot.get("levels") or {}).get("upper_resistance_band")
    scenario_rows = build_scenario_rows(spot, scen, call_wall)

    note = (
        "Directional score uses dollar spot-exposures (gamma_oi / charm_vol / vanna_vol), "
        "each term normalized to [-1,1]; the 0-1 DTE strike-expiry grid supplies pin/flip/trigger "
        "geometry. Confidence is a heuristic from S_dir margin and pin dominance/proximity — "
        "NOT a calibrated forecast."
    )
    if score.get("note"):
        note = score["note"]
    elif scen.get("note"):
        note = scen["note"]

    return {
        "bias": score.get("bias"),
        "s_dir": score.get("s_dir"),
        "confidence": conf,
        "score_components": score.get("components"),
        "active_pin": scen.get("active_pin"),
        "pin_source": scen.get("pin_source"),
        "pin_flag": scen.get("pin_flag"),
        "expiring_pin_flip": scen.get("expiring_pin_flip"),
        # The pin's size on the 0-1 DTE basis and, as context only, on the old all-expiry basis (review S4, A2).
        "pin_magnet": scen.get("pin_magnet"),
        "pin_dollars": scen.get("pin_dollars"),
        "pin_dollars_all_expiry": scen.get("pin_dollars_all_expiry"),
        "upside_trigger": scen.get("upside_trigger"),
        "upside_trigger_net_gex": scen.get("upside_trigger_net_gex"),
        "upside_trigger_dollars_all_expiry": scen.get("upside_trigger_dollars_all_expiry"),
        "downside_trigger": scen.get("downside_trigger"),
        "downside_floor": scen.get("vanna_floor"),
        "gex_floor": scen.get("gex_floor"),
        "vanna_floor": scen.get("vanna_floor"),
        "scenarios": scenario_rows,
        "divergence": divergence,
        "note": note,
    }
