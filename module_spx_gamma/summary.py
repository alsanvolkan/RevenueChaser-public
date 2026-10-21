"""Summary cards: Delta/Gamma/Vanna/Charm signals + narratives + metrics (derived from data)."""
from __future__ import annotations

from .charm_pin import MODE_DIRECTIONALIZED_VOLUME, MODE_OPEN_INTEREST, net_charm, net_vanna


def _fmt_usd(v) -> str:
    if v is None:
        return "N/A"
    a = abs(v)
    sign = "+" if v >= 0 else "-"
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.2f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


def _sum(rows, key, spot=None, side=None):
    total = 0.0
    for r in rows:
        s = r.get("strike")
        v = r.get(key)
        if s is None or v is None:
            continue
        if side and spot is None:
            continue  # no spot: a side of it is unknown, not 0 (G2)
        if side == "above" and s <= spot:
            continue
        if side == "below" and s >= spot:
            continue
        total += v
    return total


def _sig(label, tone):
    return {"label": label, "tone": tone}


def delta_card(strike_rows, spot):
    total = _sum(strike_rows, "net_delta")
    above = _sum(strike_rows, "net_delta", spot, "above")
    below = _sum(strike_rows, "net_delta", spot, "below")
    nearest = min(
        (r for r in strike_rows if r.get("strike") is not None and r.get("net_delta") is not None),
        key=lambda r: abs(r["strike"] - spot) if spot is not None else 0.0, default=None)
    at_spot = nearest["net_delta"] if (nearest and spot is not None) else None
    if nearest is None:
        return {"title": "Delta", "tag": "TREND", "signal": _sig("No data", "amber"),
                "narrative": "No delta data for this day.",
                "rows": [("Net DEX", "N/A", ""), ("Above spot", "N/A", ""), ("Below spot", "N/A", "")]}
    if total > 0:
        signal, lean = _sig("Bullish", "green"), "bullish lean"
    elif total < 0:
        signal, lean = _sig("Bearish", "red"), "bearish lean"
    else:
        signal, lean = _sig("Neutral", "amber"), "neutral"
    return {
        "title": "Delta",
        "tag": "TREND",
        "signal": signal,
        "narrative": f"Net dealer Δ {_fmt_usd(total)}, {lean}",
        "rows": [
            ("Net DEX", f"{_fmt_usd(total)}", f"at spot {_fmt_usd(at_spot)}"),
            ("Above spot", f"{_fmt_usd(above)}", "upper strikes"),
            ("Below spot", f"{_fmt_usd(below)}", "lower strikes"),
        ],
    }


def gamma_card(strike_rows, strike_rows_prev, expiry_rows, levels, spot, regime_detail=None):
    """The regime is spot against the gamma flip (``regime_detail``); the summed greek-exposure
    gamma is shown as a number only, it does not decide the label (G4)."""
    net_gamma = _sum(strike_rows, "net_gamma") if strike_rows else None
    prev_gamma = _sum(strike_rows_prev, "net_gamma") if strike_rows_prev else None
    state = (regime_detail or {}).get("state", "unknown")
    regime = {"long": "Long Γ → Dampening regime", "short": "Short Γ → Amplifying regime",
              "at_flip": "At the gamma flip"}.get(state, "Regime unknown (spot or gamma flip missing)")
    magnet = levels.get("equilibrium_point")
    call_wall = levels.get("upper_resistance_band")
    put_wall = levels.get("lower_support_band")
    magnet_value = next((r["net_gamma"] for r in strike_rows if r.get("strike") == magnet), None)
    flip = levels.get("regime_threshold")
    flip_pct = ((flip - spot) / spot * 100) if (flip is not None and spot) else None

    gamma_vs_prev = net_gamma - prev_gamma if (net_gamma is not None and prev_gamma is not None) else None
    vs_prev_pct = (gamma_vs_prev / abs(prev_gamma) * 100) if (gamma_vs_prev is not None and prev_gamma) else None

    rolls_off = sum((r["net_gex"] for r in expiry_rows
                     if r.get("dte") is not None and r["dte"] <= 1 and r.get("net_gex") is not None), 0.0) \
        if expiry_rows else None

    rows = [
        ("Net dealer Γ", f"{_fmt_usd(net_gamma)}",
         {"long": "Long (spot above flip)", "short": "Short (spot below flip)",
          "at_flip": "At the flip"}.get(state, "N/A")),
        ("Γ vs prev close", f"{_fmt_usd(gamma_vs_prev)} ({vs_prev_pct:+.0f}%)" if vs_prev_pct is not None else "N/A", ""),
        ("Top wall", f"${magnet:,.0f}" if magnet else "N/A", f"{_fmt_usd(magnet_value)}"),
        ("Flip distance", f"{flip_pct:+.1f}%" if flip_pct is not None else "N/A", f"at ${flip:,.2f}" if flip else ""),
        ("0–1d rolls off", f"{_fmt_usd(rolls_off)}" if rolls_off is not None else "N/A", "by tomorrow"),
    ]
    if magnet is not None:
        if call_wall is not None and abs(magnet - call_wall) < 0.5:
            magnet_label, magnet_mechanic = "the call wall", "dealers may sell into rallies as price approaches it"
        elif put_wall is not None and abs(magnet - put_wall) < 0.5:
            magnet_label, magnet_mechanic = "the put wall", "dealers may buy into dips as price approaches it"
        else:
            magnet_label, magnet_mechanic = "the gamma magnet (strongest pin)", "the level where dealer gamma hedging is most concentrated"
        narrative = f"{regime}. Largest level is {magnet_label} at ${magnet:,.0f}, {magnet_mechanic}."
    else:
        narrative = regime
    signal = {"long": _sig("Dampening", "green"), "short": _sig("Amplifying", "red"),
              "at_flip": _sig("At flip", "amber")}.get(state, _sig("Unknown", "amber"))
    return {"title": "Gamma", "tag": "MAGNET", "signal": signal, "narrative": narrative, "rows": rows}


def vanna_at_spot(spot_exposure_rows, spot):
    """The Vanna card's Net Vanna: directionalized-volume vanna at the strike nearest spot, or None."""
    valid = [r for r in spot_exposure_rows or []
             if r.get("strike") is not None and net_vanna(r, MODE_DIRECTIONALIZED_VOLUME) is not None]
    if not valid or spot is None:
        return None
    r = min(valid, key=lambda x: abs(x["strike"] - spot))
    return net_vanna(r, MODE_DIRECTIONALIZED_VOLUME)


def charm_live_sum(spot_exposure_rows):
    """The Charm card's Live charm: directionalized-volume charm summed over the rows, or None."""
    vals = [net_charm(r, MODE_DIRECTIONALIZED_VOLUME) for r in spot_exposure_rows or []]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def vanna_card(spot_exposure_rows, vanna_peaks, spot):
    net_v = vanna_at_spot(spot_exposure_rows, spot)
    if net_v is None:
        # No rows (or no spot): the card has nothing to say and no vote (DF17).
        return {"title": "Vanna", "tag": "VOLATILITY", "signal": _sig("No data", "amber"),
                "narrative": "No vanna data for this day.", "lean": None,
                "rows": [("Net Vanna", "N/A", ""), ("Top vol-sensitive strike", "N/A", ""),
                         ("Δ from +1pt IV", "N/A", "")]}
    total_v = sum((net_vanna(r, MODE_DIRECTIONALIZED_VOLUME) or 0) for r in spot_exposure_rows)
    short = net_v < 0
    vp = vanna_peaks or {}
    call_peak = vp.get("call_peak") or {}
    put_peak = vp.get("put_peak") or {}
    top = max([call_peak, put_peak], key=lambda p: abs(p.get("signed_call_vanna") or p.get("signed_put_vanna") or 0))
    top_strike = top.get("strike")
    top_value = top.get("signed_call_vanna") if top.get("signed_call_vanna") is not None else top.get("signed_put_vanna")
    rows = [
        ("Net Vanna", f"{_fmt_usd(net_v)}", "Short" if short else "Long"),
        ("Top vol-sensitive strike", f"${top_strike:,.0f}" if top_strike else "N/A", f"{_fmt_usd(top_value)}"),
        ("Δ from +1pt IV", f"{_fmt_usd(total_v)}", ""),
    ]
    if short:
        signal = _sig("Short", "green")
        narrative = ("Short Vanna — IV spikes pressure stock higher via dealer buying. "
                     "If IV rises, dealers lose delta and will likely buy stock to rehedge, "
                     "a cushion during vol spikes.")
    else:
        signal = _sig("Long", "red")
        narrative = "Long Vanna — IV spikes pressure stock lower via dealer selling."
    return {"title": "Vanna", "tag": "VOLATILITY", "signal": signal, "narrative": narrative, "rows": rows}


def charm_card(spot_exposure_rows, charm_qualification, spot):
    live = charm_live_sum(spot_exposure_rows)
    pos_vals = [net_charm(r, MODE_OPEN_INTEREST) for r in spot_exposure_rows or []]
    pos_vals = [v for v in pos_vals if v is not None]
    positioning = sum(pos_vals) if pos_vals else None
    if live is None and positioning is None:
        # No rows: no lean and no vote (DF17).
        return {"title": "Charm", "tag": "TIMER", "signal": _sig("No data", "amber"),
                "narrative": "No charm data for this day.", "lean": None,
                "rows": [("Live charm", "N/A", ""), ("Positioning", "N/A", ""), ("Where it matters", "N/A", "")]}
    live_v = live or 0.0
    pos_v = positioning or 0.0
    # Two zero sums (or one) carry no direction, so they never "agree".
    agree = live_v != 0 and pos_v != 0 and (live_v > 0) == (pos_v > 0)
    live_dir = "Sell pressure" if live_v < 0 else "Buy pressure"
    pos_dir = "Buy pressure" if pos_v > 0 else "Sell pressure"
    imbalance = abs(pos_v) / (abs(live_v) + abs(pos_v)) * 100 if (abs(live_v) + abs(pos_v)) else None
    cq = charm_qualification or {}
    c_strike = cq.get("strike")
    dist = ((c_strike - spot) / spot * 100) if (c_strike is not None and spot) else None
    rows = [
        ("Live charm", f"{_fmt_usd(live)}", live_dir),
        ("Positioning", f"{_fmt_usd(positioning)}", f"{pos_dir} · {imbalance:.0f}% imbalance" if imbalance is not None else ""),
        ("Where it matters", f"${c_strike:,.0f}" if c_strike else "N/A",
         f"{dist:+.1f}% from spot · pin zone" if dist is not None else ""),
    ]
    # The card votes in the cross-signal matrix only when live flow and positioning agree: sell
    # pressure leans bearish, buy pressure bullish. Mixed signals carry no lean (G14).
    lean = None
    if agree:
        signal = _sig("Agree", "green")
        narrative = "Charm signals agree — " + ("sell-side" if live_v < 0 else "buy-side") + " lean."
        lean = "bearish" if live_v < 0 else "bullish"
    else:
        signal = _sig("Mixed", "amber")
        narrative = "Mixed charm signals — low confidence in direction."
    return {"title": "Charm", "tag": "TIMER", "signal": signal, "narrative": narrative, "rows": rows,
            "lean": lean}


def build_summary(snapshot) -> list[dict]:
    spot = snapshot.get("current_level")
    strike_rows = snapshot.get("strike_exposures", [])
    strike_rows_prev = snapshot.get("strike_exposures_prev", [])
    expiry_rows = snapshot.get("expiry_distribution", [])
    levels = snapshot.get("levels", {})
    return [
        delta_card(strike_rows, spot),
        gamma_card(strike_rows, strike_rows_prev, expiry_rows, levels, spot, snapshot.get("regime_detail")),
        vanna_card(snapshot.get("spot_exposures", []), snapshot.get("vanna_peaks"), spot),
        charm_card(snapshot.get("spot_exposures", []), snapshot.get("charm_qualification"), spot),
    ]


def at_a_glance(cards: list[dict]) -> str:
    parts = []
    for c in cards:
        parts.append(f"{c['title']} {c['signal']['label']}")
    return " · ".join(parts)


def signal_matrix(cards: list[dict]) -> dict:
    """Cross-signal synthesis: consensus vs divergence across the 4 cards.

    Mirrors the report's cross-signal matrix: each card carries a directional
    signal, and the matrix reports how many agree. Structural (non-directional)
    cards such as Gamma's regime are treated as context, not a vote.

    Directional mapping:
    - Delta   : Bullish -> bullish vote, Bearish -> bearish vote.
    - Vanna   : Short (cushion on vol spikes) -> bullish vote; Long (drag) -> bearish.
    - Charm   : live flow and positioning agree on buy pressure -> bullish vote, on sell
                pressure -> bearish (the card's ``lean``); mixed -> no vote.
    """
    def _vote(title: str, label: str, lean: str | None = None) -> str | None:
        if lean in ("bullish", "bearish"):
            return lean
        low = label.lower()
        if low in ("bullish",):
            return "bullish"
        if low in ("bearish",):
            return "bearish"
        if title == "Vanna":
            return "bullish" if low == "short" else ("bearish" if low == "long" else None)
        return None

    rows = []
    votes = {"bullish": 0, "bearish": 0}
    total = 0
    for c in cards:
        v = _vote(c["title"], c["signal"]["label"], c.get("lean"))
        if v is not None:
            votes[v] += 1
            total += 1
        rows.append({"title": c["title"], "label": c["signal"]["label"], "tone": c["signal"]["tone"]})

    if total == 0:
        stance, stance_tone = "Neutral", "amber"
    elif votes["bullish"] == total:
        stance, stance_tone = "Bullish consensus", "green"
    elif votes["bearish"] == total:
        stance, stance_tone = "Bearish consensus", "red"
    else:
        stance, stance_tone = "Divergence", "amber"

    return {
        "rows": rows,
        "votes": votes,
        "total_directional": total,
        "stance": stance,
        "tone": stance_tone,
        "summary": (
            f"{votes['bullish']}/{total} directional signals lean bullish, "
            f"{votes['bearish']}/{total} lean bearish."
        ),
    }
