"""The dealer book under the reports' two assumptions (A4, "Piyasa yapıcı defteri").

Black-Scholes greeks (r = 0, no dividend) from each UW contract's own IV, summed over open interest x 100. Assumption
(a): dealers long calls and short puts (UW's own sign convention); (b): dealers short every option. Units: gamma is
dollar GEX per 1 % move (gamma x S^2 x 1 %), vanna dollar delta per IV point, charm dollar delta per day; vomma,
zomma and speed are share-unit sums, read for their sign. The chain is the Fetch's front and target-Friday chains.
"""
from __future__ import annotations

import math
from datetime import date

MULT = 100.0
DOWN_PTS = 50.0


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_greeks(spot: float, strike: float, years: float, iv: float, cp: str) -> dict | None:
    if not (spot > 0 and strike > 0 and years > 0 and iv and 0 < iv < 5):
        return None
    st = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / st
    d2 = d1 - st
    n = _pdf(d1)
    gamma = n / (spot * st)
    vega = spot * n * math.sqrt(years)
    return {
        "delta": _cdf(d1) if cp == "C" else _cdf(d1) - 1.0,
        "gamma": gamma,
        "vega": vega,
        "vanna": -n * d2 / iv,
        "charm": n * d2 / (2.0 * years),                 # delta change per year as time passes
        "vomma": vega * d1 * d2 / iv,
        "zomma": gamma * (d1 * d2 - 1.0) / iv,
        "speed": -gamma / spot * (d1 / st + 1.0),
    }


def _words(book: dict) -> str:
    g, g50, ch, vo = book["gamma"], book["gamma_down50"], book["charm"], book["vomma"]
    out = ["dealers dampen moves" if g > 0 else "dealers amplify moves"]
    if (g > 0) != (g50 > 0):
        out.append(f"the sign flips {DOWN_PTS:.0f} points lower")
    # charm is the book's own d(delta)/dt: delta that grows as time passes is sold to stay hedged
    out.append("charm sells into expiry" if ch > 0 else "charm buys into expiry")
    out.append("an IV spike grows the book's vega" if vo > 0 else "an IV spike shrinks the book's vega")
    return "; ".join(out)


def dealer_book(chain_rows: list[dict], spot: float | None, asof_date: str) -> dict | None:
    if not spot:
        return None
    base = date.fromisoformat(asof_date)
    sums = {k: {"gamma": 0.0, "gamma_down50": 0.0, "vanna": 0.0, "charm": 0.0, "vomma": 0.0, "zomma": 0.0,
                "speed": 0.0} for k in ("a", "b")}
    used, expiries, seen = 0, set(), set()
    for r in chain_rows or []:
        key = (r.get("expiry"), r.get("strike"), r.get("type"))
        if key in seen:
            continue    # the 0DTE fallback chain (no expiry) also holds the target Friday's contracts
        seen.add(key)
        oi, iv, k, cp, e = r.get("open_interest"), r.get("iv"), r.get("strike"), r.get("type"), r.get("expiry")
        if not oi or not iv or not k or cp not in ("C", "P") or not e:
            continue
        days = (date.fromisoformat(e) - base).days
        years = max(days, 0.5) / 365.0
        g = bs_greeks(spot, k, years, iv, cp)
        g50 = bs_greeks(spot - DOWN_PTS, k, years, iv, cp)
        if g is None or g50 is None:
            continue
        used += 1
        expiries.add(e)
        q = oi * MULT
        for key, sign in (("a", 1.0 if cp == "C" else -1.0), ("b", -1.0)):
            s = sums[key]
            s["gamma"] += sign * q * g["gamma"] * spot * spot * 0.01
            s["gamma_down50"] += sign * q * g50["gamma"] * (spot - DOWN_PTS) ** 2 * 0.01
            s["vanna"] += sign * q * g["vanna"] * spot * 0.01
            s["charm"] += sign * q * g["charm"] / 365.0 * spot
            s["vomma"] += sign * q * g["vomma"]
            s["zomma"] += sign * q * g["zomma"]
            s["speed"] += sign * q * g["speed"]
    if not used:
        return None
    for key in ("a", "b"):
        sums[key]["words"] = _words(sums[key])
    return {"a": sums["a"], "b": sums["b"], "contracts": used, "expiries": sorted(expiries)}
