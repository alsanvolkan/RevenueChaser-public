"""R3 — Financial Quality (Sector-Adaptive). Fixed: coverage, proxies, flags, falsey checks."""
import numpy as np
from datetime import date
from typing import Tuple, Optional
from RevenueChaser.core.config import (
    SCORE_RANGE,
    R3_OM_MAG_DOWN_BEAR as OM_MAG_DOWN_BEAR,
    R3_OM_MAG_DOWN_MILD as OM_MAG_DOWN_MILD,
    R3_OM_MAG_UP_BULL as OM_MAG_UP_BULL,
    R3_GM_HELD_YOY_BP as GM_HELD_YOY_BP,
    R3_SGA_Z_SPIKE as SGA_Z_SPIKE,
    R3_MIN_OM_QUARTERS as _MIN_OM_QUARTERS,
)
from RevenueChaser.module_earnings_radar.models import RuleResult
from RevenueChaser.module_earnings_radar.classifier import get_cluster_red_flags


YOY_MIN_DAYS, YOY_MAX_DAYS = 350, 380

_KNOWN_CLUSTERS = frozenset({
    "C1_saas", "C2_hardware", "C3_fintech_lending", "C4_crypto_mining",
    "C5_marketplace_consumer", "C6_energy_utilities",
})
# Clusters whose own scorer already votes on the gross / operating margin level.
_CLUSTER_SCORES_GM = frozenset({
    "C2_hardware", "C3_fintech_lending", "C4_crypto_mining",
    "C5_marketplace_consumer", "C6_energy_utilities",
})
_CLUSTER_SCORES_OM = frozenset({
    "C3_fintech_lending", "C5_marketplace_consumer", "C6_energy_utilities",
})


def _median(vals: list[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _om_row(row: dict) -> Optional[float]:
    om = _get(row, "operating_margin")
    return float(om) if om is not None else None


def _parse_date(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _yoY_index(rows: list[dict], latest_fd: str) -> Optional[int]:
    """Index of the same fiscal quarter one year earlier.

    Matches the row whose ``fiscal_date`` lies 350-380 days before ``latest_fd``
    (closest to 365), so 52/53-week calendars (quarter ends that drift by a few
    days) still find their partner. None when no such row exists.
    """
    latest = _parse_date(latest_fd)
    if latest is None:
        return None
    best_i, best_gap = None, None
    for i, r in enumerate(rows):
        fd = _parse_date(_get(r, "fiscal_date"))
        if fd is None:
            continue
        days = (latest - fd).days
        if not (YOY_MIN_DAYS <= days <= YOY_MAX_DAYS):
            continue
        gap = abs(days - 365)
        if best_gap is None or gap < best_gap:
            best_i, best_gap = i, gap
    return best_i


def _yoy_prior(history: list[dict]) -> Optional[dict]:
    """The latest quarter's year-ago row (``history[0]`` is the newest), else None.

    Growth and margin deltas compare against the same quarter a year earlier, so a
    seasonal quarter is not read as a trend; an undated or short history gets no
    partner and the signal stays unavailable instead of falling back to QoQ.
    """
    if not history:
        return None
    i = _yoY_index(history, str(_get(history[0], "fiscal_date") or ""))
    return history[i] if i is not None else None


def _universal_om(history: list[dict], cluster: str) -> Tuple[Optional[dict], list[str], list[str]]:
    """Universal reported-OM vote + opex tags.

    Returns ``(info, reasons, flags)`` where ``info`` is None when there are not
    enough usable quarters or no YoY partner — the cluster score then stands
    unchanged (a short or undated history never fabricates a GAAP vote).
    """
    rows = [r for r in history if _om_row(r) is not None]
    if len(rows) < _MIN_OM_QUARTERS:
        return None, [], []
    latest = rows[0]
    yoy_i = _yoY_index(rows, str(_get(latest, "fiscal_date") or ""))
    if yoy_i is None:
        return None, [], []

    om = _om_row(latest)
    om_yoy = _om_row(rows[yoy_i])
    if om is None or om_yoy is None:
        return None, [], []

    om_delta_pts = (om - om_yoy) * 100.0
    recent_om = [_om_row(r) for r in rows[:4]]
    prior_om = [_om_row(r) for r in rows[4:8]]
    recent_med = _median(recent_om)
    prior_med = _median(prior_om) if len(prior_om) >= 2 else None

    # GM gate (latest vs same-quarter YoY) — tag only, never the vote.
    gm = _get(latest, "gross_margin")
    gm_yoy = _get(rows[yoy_i], "gross_margin")
    gm_held = None
    if gm is not None and gm_yoy is not None:
        gm_held = (gm - gm_yoy) >= GM_HELD_YOY_BP

    # SG&A z (baseline excludes the latest quarter so the spike can't shrink z).
    sga_latest = _get(latest, "sga")
    prior_sga = [float(_get(r, "sga")) for r in rows[1:] if _get(r, "sga") is not None]
    sga_z = None
    core_om = None
    oi_latest = _get(latest, "operating_income")
    rev_latest = _get(latest, "revenue")
    if sga_latest is not None and len(prior_sga) >= 3:
        med = _median(prior_sga)
        var = sum((x - med) ** 2 for x in prior_sga) / max(len(prior_sga) - 1, 1)
        sd = float(np.sqrt(var)) if var > 0 else 0.0
        if sd > 0:
            sga_z = (sga_latest - med) / sd
        if med is not None and oi_latest is not None and rev_latest:
            add_back = max(0.0, float(sga_latest) - med)
            core_om = (float(oi_latest) + add_back) / float(rev_latest) if float(rev_latest) != 0 else None

    reasons: list[str] = []
    flags: list[str] = []
    info = {
        "om_pts": round(om_delta_pts, 2),
        "om": om, "om_yoy": om_yoy,
        "recent_median": round(recent_med, 4) if recent_med is not None else None,
        "prior_median": round(prior_med, 4) if prior_med is not None else None,
        "gm_held": gm_held, "sga_z": round(sga_z, 2) if sga_z is not None else None,
        "core_om": round(core_om, 4) if core_om is not None else None,
    }

    # Vote: downside magnitude wins. Upside needs the last-4 median up too.
    vote = "mixed"
    if om_delta_pts <= -OM_MAG_DOWN_BEAR:
        vote = "bear"
        reasons.append(f"GAAP op. margin {om*100:.1f}% vs YoY {om_yoy*100:.1f}% ({om_delta_pts:.1f} pts)")
    elif om_delta_pts <= -OM_MAG_DOWN_MILD:
        vote = "bear"
        reasons.append(f"op. margin down {om_delta_pts:.1f} pts YoY ({om*100:.1f}% vs {om_yoy*100:.1f}%)")
    elif om_delta_pts >= OM_MAG_UP_BULL and recent_med is not None and prior_med is not None and recent_med > prior_med:
        vote = "bull"
        reasons.append(f"op. margin up {om_delta_pts:.1f} pts YoY and last-4 median improving")
    elif om_delta_pts < -OM_MAG_DOWN_MILD * 0.5:
        # small but real decline with GM held reads mixed-with-tag, not bullish.
        reasons.append(f"op. margin {om*100:.1f}% vs YoY {om_yoy*100:.1f}% ({om_delta_pts:.1f} pts)")
    else:
        reasons.append("op. margin stable vs YoY")
    info["vote"] = vote

    # Tags (never flip the vote).
    if vote == "bear":
        if gm_held is True:
            info["opex_event"] = True
            reasons.append("opex event — GM held while op. margin fell")
        elif gm_held is False:
            info["franchise"] = True
            reasons.append("franchise margin broke — GM fell with op. margin")
        if sga_z is not None and sga_z > SGA_Z_SPIKE:
            info["sga_spike"] = True
            if core_om is not None:
                reasons.append(f"SG&A z={sga_z:.1f} — core OM ≈ {core_om*100:.0f}% if add-back")
            else:
                reasons.append(f"SG&A z={sga_z:.1f} (spike)")
    elif vote == "bull":
        if sga_z is not None and sga_z > SGA_Z_SPIKE:
            reasons.append(f"SG&A elevated (z={sga_z:.1f}) despite OM up")
    if recent_med is not None and prior_med is not None and recent_med < prior_med and vote == "mixed":
        info["median_drift_down"] = True

    return info, reasons, flags


def _get(row, key: str, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def score(ticker: str, cluster: str, fundamentals_history: list[dict],
          btc_price: Optional[float] = None) -> RuleResult:
    if not fundamentals_history or not fundamentals_history[0].get("revenue"):
        return RuleResult(score=None, available=False, coverage=0, reasons=["no fundamental data"])

    latest = fundamentals_history[0]
    pct_score = 0.0
    reasons: list[str] = []
    flags: list[str] = []
    signals_avail = 0
    signals_total = 0

    # Route to cluster-specific scorer
    if cluster == "C1_saas":
        s, r, f, a, t = _score_saas(latest, fundamentals_history)
    elif cluster == "C2_hardware":
        s, r, f, a, t = _score_hardware(latest, fundamentals_history)
    elif cluster == "C3_fintech_lending":
        s, r, f, a, t = _score_fintech(latest, fundamentals_history)
    elif cluster == "C4_crypto_mining":
        s, r, f, a, t = _score_crypto(latest, fundamentals_history, btc_price)
    elif cluster == "C6_energy_utilities":
        s, r, f, a, t = _score_energy(latest, fundamentals_history)
    elif cluster == "C5_marketplace_consumer":
        s, r, f, a, t = _score_marketplace(latest, fundamentals_history)
    else:
        s, r, f, a, t = _score_unmapped(latest, fundamentals_history)

    pct_score += s
    reasons.extend(r)
    flags.extend(f)
    signals_avail += a
    signals_total += t

    # Gross / operating margin supplements (cluster-agnostic): only for clusters
    # whose own scorer does not already score that margin, so a margin never
    # counts twice. Unmapped clusters and C3 score both themselves.
    own_gm = cluster in _CLUSTER_SCORES_GM or cluster not in _KNOWN_CLUSTERS
    own_om = cluster in _CLUSTER_SCORES_OM or cluster not in _KNOWN_CLUSTERS
    gm = _get(latest, "gross_margin")
    if gm is not None and not own_gm:
        signals_avail += 1; signals_total += 1
        if gm > 0.50: pct_score += 10
        elif gm < 0.10: pct_score -= 10

    om = _get(latest, "operating_margin")
    if om is not None and not own_om:
        signals_avail += 1; signals_total += 1
        if om > 0.15: pct_score += 10
        elif om < 0: flags.append("negative_operating_margin")

    # ── Universal GAAP OM vote (Phase 1) ──
    # A rich (>=4Q, YoY-partnered) history layers the reported-OM read on top of
    # the cluster score. It never overrides a cluster's own signal, but a
    # >8-pt OM decline floors the result negative — no cluster score can flip it.
    om_info, om_reasons, om_flags = _universal_om(fundamentals_history, cluster)
    if om_info is not None:
        reasons.extend(om_reasons)
        flags.extend(om_flags)
        if om_info["om_pts"] <= -OM_MAG_DOWN_BEAR:
            pct_score = min(pct_score, -30.0)
        elif om_info["om_pts"] <= -OM_MAG_DOWN_MILD and pct_score > -15:
            pct_score = min(pct_score, -15.0)
        elif om_info["vote"] == "bull" and pct_score < 15:
            # the vote's own test: OM up YoY AND the last-4 median above the prior-4 (redesign.md R3; review S2)
            pct_score = max(pct_score, 15.0)

    # Red flag matching — use exact set intersection
    cluster_flags = set(get_cluster_red_flags(cluster))
    matched = [f for f in flags if f in cluster_flags]
    if matched:
        reasons.append("RED FLAGS: " + "; ".join(matched))
        pct_score -= len(matched) * 10

    # A scorer that counts more signals than its denominator never reads above full coverage (review S1).
    coverage = min(1.0, signals_avail / max(signals_total, 1))

    # Minimum coverage gate
    if signals_avail < 2:
        return RuleResult(
            score=None, available=False, coverage=round(coverage, 2),
            reasons=["insufficient financial coverage"],
            flags=matched,
        )

    pct_score = max(SCORE_RANGE[0], min(SCORE_RANGE[1], pct_score))

    return RuleResult(
        score=round(pct_score, 1), available=True, coverage=round(coverage, 2),
        reasons=reasons if reasons else ["neutral financial quality"], flags=matched,
    )


def _zdivergence(vals_q: list[float], ref_q: list[float]) -> Optional[float]:
    if len(vals_q) < 4 or len(ref_q) < 4: return None
    v_deltas = [(vals_q[i] - vals_q[i-1]) / abs(vals_q[i-1]) for i in range(1, len(vals_q)) if vals_q[i-1] != 0]
    r_deltas = [(ref_q[i] - ref_q[i-1]) / abs(ref_q[i-1]) for i in range(1, len(ref_q)) if ref_q[i-1] != 0]
    n = min(len(v_deltas), len(r_deltas), 8)
    v_deltas, r_deltas = v_deltas[-n:], r_deltas[-n:]
    if len(v_deltas) < 3: return None
    mu_v, std_v = np.mean(v_deltas), max(np.std(v_deltas, ddof=1) if len(v_deltas) > 1 else 1, 0.001)
    mu_r, std_r = np.mean(r_deltas), max(np.std(r_deltas, ddof=1) if len(r_deltas) > 1 else 1, 0.001)
    return (v_deltas[-1] - mu_v) / std_v - (r_deltas[-1] - mu_r) / std_r


def _score_saas(latest: dict, history: list[dict]) -> Tuple[float, list, list, int, int]:
    s, r, f, a, t = 0.0, [], [], 0, 3
    prior = _yoy_prior(history)
    rev = _get(latest, "revenue")
    prev_rev = _get(prior, "revenue")

    if rev is not None and prev_rev is not None and prev_rev != 0:
        rev_g = (rev - prev_rev) / abs(prev_rev)
        a += 1
        if rev_g > 0.15: s += 15; r.append(f"rev growth {rev_g:.0%}")
        elif rev_g < -0.10: s -= 15

    # Deferred revenue trend vs revenue trend
    dr = _get(latest, "deferred_revenue")
    prev_dr = _get(prior, "deferred_revenue")
    if dr is not None and prev_dr is not None and prev_dr != 0 and rev is not None and prev_rev is not None and prev_rev != 0:
        a += 1
        dr_g = (dr - prev_dr) / abs(prev_dr)
        if dr_g > rev_g: s += 25; r.append(f"Deferred rev growth ({dr_g:.0%}) > revenue ({rev_g:.0%})")
        elif dr_g < rev_g: f.append("deferred_revenue_deceleration"); s -= 15

    # RPO / backlog (only if explicitly available)
    rpo = _get(latest, "rpo")
    prev_rpo = _get(prior, "rpo")
    if rpo is not None and prev_rpo is not None and prev_rpo != 0:
        a += 1
        rpo_g = (rpo - prev_rpo) / abs(prev_rpo)
        if rev is not None and prev_rev is not None and prev_rev != 0 and rpo_g > rev_g:
            s += 20; r.append("RPO growing faster than revenue")
        elif rev is not None and prev_rev is not None and prev_rev != 0:
            f.append("rpo_growth_below_revenue_growth"); s -= 15

    return s, r, f, a, t


def _score_hardware(latest: dict, history: list[dict]) -> Tuple[float, list, list, int, int]:
    s, r, f, a, t = 0.0, [], [], 0, 4
    prior = _yoy_prior(history)
    # Aligned (date, inventory, revenue) triples in chronological order: history is
    # newest-first, and _zdivergence reads the last delta as the latest quarter.
    pairs = [(h.get("fiscal_date") or "", _get(h, "inventory"), _get(h, "revenue"))
             for h in history if _get(h, "inventory") is not None and _get(h, "revenue") is not None]
    if pairs and all(d for d, _, _ in pairs):
        pairs.sort(key=lambda p: p[0])
    else:
        pairs.reverse()
    inv_vals = [p[1] for p in pairs]
    rev_vals = [p[2] for p in pairs]

    if len(inv_vals) >= 4 and len(rev_vals) >= 4:
        a += 1
        divergence = _zdivergence(inv_vals, rev_vals)
        if divergence is not None:
            if divergence > 1.5: f.append("inventory_growth_exceeds_revenue_growth"); s -= 25; r.append(f"Inv-Rev Z-div: {divergence:.1f}σ")
            elif divergence > 0.5: s -= 10
            elif divergence < -0.5: s += 15

    rev = _get(latest, "revenue"); prev_rev = _get(prior, "revenue")
    gm = _get(latest, "gross_margin"); prev_gm = _get(prior, "gross_margin")
    if gm is not None and prev_gm is not None and rev is not None and prev_rev is not None and prev_rev != 0:
        a += 1
        gm_delta = gm - prev_gm
        rev_delta = (rev - prev_rev) / abs(prev_rev)
        elasticity = gm_delta / max(abs(rev_delta), 0.001)
        if elasticity < -0.5: f.append("negative_gross_margin_elasticity"); s -= 20; r.append(f"GM elasticity {elasticity:.1f}")
        elif elasticity > 0.5: s += 15; r.append(f"GM elasticity {elasticity:.1f}")

    om = _get(latest, "operating_margin"); prev_om = _get(prior, "operating_margin")
    if om is not None and prev_om is not None:
        a += 1
        delta_om = om - prev_om
        if delta_om < -0.02: s -= 10
        elif delta_om > 0.02: s += 10

    # Inventory/revenue ratio (working capital risk, not backlog proxy)
    inv_now = _get(latest, "inventory")
    if inv_now is not None and rev is not None and rev != 0:
        a += 1
        ratio = inv_now / rev
        if ratio > 0.40: s -= 15; f.append("high_inventory_to_revenue")
        elif ratio < 0.10: s += 10

    return s, r, f, a, t


def _score_fintech(latest: dict, history: list[dict]) -> Tuple[float, list, list, int, int]:
    s, r, f, a, t = 0.0, [], [], 0, 3
    nim = _get(latest, "gross_margin")
    if nim is not None:
        a += 1
        if nim > 0.50: s += 15; r.append(f"strong gross margin {nim:.0%}")
        elif nim < 0.20: f.append("nim_compression"); s -= 15

    om = _get(latest, "operating_margin")
    if om is not None:
        a += 1
        if om > 0.40: s += 15; r.append(f"efficiency ratio strong ({om:.0%})")
        elif om < 0.10: f.append("deteriorating_efficiency_ratio"); s -= 15

    receivables = _get(latest, "receivables")
    rev = _get(latest, "revenue")
    if receivables is not None and rev is not None and rev != 0:
        a += 1
        ratio = receivables / rev
        if ratio > 0.3: f.append("high_receivables"); s -= 15
        elif ratio < 0.1: s += 10; r.append("low credit exposure")

    return s, r, f, a, t


def _score_crypto(latest: dict, history: list[dict], btc_price: Optional[float]) -> Tuple[float, list, list, int, int]:
    s, r, f, a, t = 0.0, [], [], 0, 3
    prior = _yoy_prior(history)
    rev = _get(latest, "revenue")
    prev_rev = _get(prior, "revenue")
    if rev is not None and prev_rev is not None and prev_rev != 0:
        a += 1
        rev_g = (rev - prev_rev) / abs(prev_rev)
        if rev_g > 0.20: s += 20; r.append(f"hash-rate driven rev {rev_g:.0%}")
        elif rev_g < -0.20: f.append("revenue_decline_despite_btc"); s -= 20

    gm = _get(latest, "gross_margin")
    if gm is not None:
        a += 1
        if gm > 0.60: s += 15; r.append("efficient mining ops")
        elif gm < 0.30: f.append("cost_to_mine_rises_faster_than_btc"); s -= 20

    cash = _get(latest, "cash_from_operations")
    ta = _get(latest, "total_assets")
    if cash is not None and ta is not None and ta != 0:
        a += 1
        coverage = cash / ta
        if coverage > 0.30: s += 15; r.append(f"strong BTC/cash reserve ({coverage:.0%})")
        elif coverage < 0.10: f.append("declining_cash_btc_reserve_coverage"); s -= 15

    return s, r, f, a, t


def _score_marketplace(latest: dict, history: list[dict]) -> Tuple[float, list, list, int, int]:
    s, r, f, a, t = 0.0, [], [], 0, 3
    prior = _yoy_prior(history)
    rev = _get(latest, "revenue")
    prev_rev = _get(prior, "revenue")
    gm = _get(latest, "gross_margin")
    prev_gm = _get(prior, "gross_margin")

    if gm is not None:
        a += 1
        if gm > 0.60: s += 20; r.append(f"high take rate/GM {gm:.0%}")
        elif gm < 0.20: f.append("take_rate_compression"); s -= 20
    if gm is not None and prev_gm is not None:
        a += 1
        delta = gm - prev_gm
        if delta < -0.02: f.append("margin_compression"); s -= 10
        elif delta > 0.02: s += 10; r.append(f"GM expansion +{delta:.1%}")

    om = _get(latest, "operating_margin")
    if om is not None:
        a += 1
        if om > 0.15: s += 15; r.append(f"strong OM {om:.0%}")

    return s, r, f, a, t


def _score_energy(latest: dict, history: list[dict]) -> Tuple[float, list, list, int, int]:
    # five signals: revenue growth, GM, GM elasticity, OM, cash conversion (review S1: OM was scored uncounted
    # over a denominator of three, so coverage read 1.33)
    s, r, f, a, t = 0.0, [], [], 0, 5
    prior = _yoy_prior(history)
    rev = _get(latest, "revenue")
    prev_rev = _get(prior, "revenue")
    gm = _get(latest, "gross_margin")
    prev_gm = _get(prior, "gross_margin")
    om = _get(latest, "operating_margin")
    cfo = _get(latest, "cash_from_operations")

    if rev is not None and prev_rev is not None and prev_rev != 0:
        a += 1
        rev_g = (rev - prev_rev) / abs(prev_rev)
        if rev_g > 0.10: s += 15; r.append(f"revenue growth {rev_g:.0%}")
        elif rev_g < -0.10: f.append("revenue_contraction"); s -= 15

    if gm is not None:
        a += 1
        if gm > 0.35: s += 15; r.append(f"strong energy margins {gm:.0%}")
        elif gm < 0.10: f.append("low_energy_margins"); s -= 10

    if gm is not None and prev_gm is not None and rev is not None and prev_rev is not None and prev_rev != 0:
        a += 1
        elasticity = (gm - prev_gm) / max(abs((rev - prev_rev) / prev_rev), 0.001)
        if elasticity < -0.5: f.append("negative_gm_elasticity"); s -= 15
        elif elasticity > 0.5: s += 10; r.append(f"GM elasticity {elasticity:.1f}")

    if om is not None:
        a += 1
        if om > 0.20: s += 10; r.append(f"strong operating margin {om:.0%}")
        elif om < 0.05: f.append("thin_operating_margin"); s -= 10

    if cfo is not None and rev is not None and rev != 0:
        a += 1
        cf_ratio = cfo / rev
        if cf_ratio > 0.15: s += 15; r.append(f"strong cash conversion {cf_ratio:.0%}")

    return s, r, f, a, t


def _score_unmapped(latest: dict, history: list[dict]) -> Tuple[float, list, list, int, int]:
    s, r, f, a, t = 0.0, [], [], 0, 3
    prior = _yoy_prior(history)
    gm = _get(latest, "gross_margin")
    if gm is not None:
        a += 1
        if gm > 0.40: s += 10; r.append(f"strong GM {gm:.0%}")
        elif gm < 0.10: s -= 10; f.append("low_gross_margin")

    rev = _get(latest, "revenue")
    prev_rev = _get(prior, "revenue")
    if rev is not None and prev_rev is not None and prev_rev != 0:
        a += 1
        rev_g = (rev - prev_rev) / abs(prev_rev)
        if rev_g > 0.15: s += 15; r.append(f"strong rev growth {rev_g:.0%}")
        elif rev_g < -0.15: f.append("revenue_contraction"); s -= 15

    om = _get(latest, "operating_margin")
    if om is not None:
        a += 1
        if om > 0.15: s += 10

    return s, r, f, a, t
