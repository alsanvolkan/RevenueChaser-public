"""Render the SPX critical thresholds report as Markdown and HTML (Jinja2)."""
from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

from RevenueChaser.core.reporting_common import THEME_CSS, THEME_JS, help_html, notice_html, provider_health_html

from .brier import scorecard
from .config import BRIER_FILENAME, HISTORY_FILENAME
from .flow_signals import evaluate_flow_signal
from .narrative import build_walkthrough
from .normalize import compute_positioning_deltas
from .relations import GLOSSARY_TR, causal_chain, greek_relations
from .scenarios import build_scenarios
from .summary import at_a_glance, build_summary, signal_matrix

_TEMPLATES = Path(__file__).resolve().parent / "templates"
# The HTML page autoescapes every context value (ADR-X1, G15): ``ticker``, ``source`` and
# the candle source come from a provider. The Markdown file is not HTML, so its
# environment stays unescaped. ``_env`` registers the two filters below on both.


def _env(autoescape: bool) -> Environment:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=autoescape)
    env.filters["fmtnum"] = _fmt_num
    env.filters["fmt_signed_compact"] = fmt_signed_compact
    return env


def _fmt_num(v, spec: str = "{:,.0f}") -> str:
    """Jinja helper: Python-style numeric formatting (thousands + fixed decimals)."""
    if v is None:
        return "N/A"
    try:
        return spec.format(float(v))
    except (TypeError, ValueError):
        return "N/A"


def fmt_level(v) -> str:
    return f"{v:,.0f}" if v is not None else "N/A"


def fmt_signed_compact(v) -> str:
    if v is None:
        return "N/A"
    a = abs(v)
    sign = "+" if v >= 0 else "-"
    if a >= 1e12:
        return f"{sign}{a / 1e12:,.1f}T"
    if a >= 1e9:
        return f"{sign}{a / 1e9:,.1f}B"
    if a >= 1e6:
        return f"{sign}{a / 1e6:,.1f}M"
    if a >= 1e3:
        return f"{sign}{a / 1e3:,.1f}K"
    return f"{sign}{a:,.0f}"


_MD_ENV = _env(autoescape=False)
_HTML_ENV = _env(autoescape=True)


def _cell(strike, signed_value) -> str:
    if strike is None:
        return "N/A"
    return f"{strike:,.0f} ({fmt_signed_compact(signed_value)})"


def _role(regime: str | None) -> str:
    return "threshold (breakout risk)" if (regime and "Short Gamma" in regime) else "support (mean-reversion)"


def top_gex_strikes(snapshot: dict, limit: int = 10) -> list[dict]:
    """Top-N strikes by |net_gamma| with a gamma-flip side label."""
    flip = (snapshot.get("levels") or {}).get("regime_threshold")
    rows = [
        r for r in snapshot.get("strike_exposures", [])
        if r.get("strike") is not None and r.get("net_gamma") is not None
    ]
    rows.sort(key=lambda r: abs(r["net_gamma"]), reverse=True)
    out = []
    for r in rows[:limit]:
        out.append({
            "strike": r["strike"],
            "net_gex": r["net_gamma"],
            "side": ("above" if flip is not None and r["strike"] > flip else
                     "below" if flip is not None and r["strike"] < flip else "at"),
        })
    return out


def _expiry_summary(snapshot: dict) -> list[dict]:
    """Expiry distribution rows formatted for the table (top 30 by |net GEX|)."""
    rows = [
        r for r in snapshot.get("expiry_distribution", [])
        if r.get("expiry") is not None and r.get("net_gex") is not None
    ]
    rows.sort(key=lambda r: abs(r["net_gex"]), reverse=True)
    return [
        {
            "expiry": r["expiry"],
            "dte": r.get("dte"),
            "band": r.get("band") or "",
            "net_gex": fmt_signed_compact(r["net_gex"]),
            "net_dex": fmt_signed_compact(r.get("net_dex")),
        }
        for r in rows[:30]
    ]


def _max_pain_rows(snapshot: dict) -> list[dict]:
    """Max pain rows formatted (expiry + level + % from spot)."""
    rows = []
    for r in snapshot.get("max_pain", []):
        rows.append({
            "expiry": r.get("expiry"),
            "max_pain": fmt_level(r.get("max_pain")),
            "distance_pct": f"{r.get('distance_pct'):+.2f}%" if r.get("distance_pct") is not None else "N/A",
        })
    return rows


def _vol_term_rows(snapshot: dict) -> list[dict]:
    """Vol term structure rows (expiry + DTE + IV + implied move)."""
    rows = []
    for r in snapshot.get("vol_term", []):
        rows.append({
            "expiry": r.get("expiry"),
            "dte": r.get("dte"),
            "volatility": f"{r.get('volatility') * 100:.1f}%" if r.get("volatility") is not None else "N/A",
            "implied_move_perc": f"{r.get('implied_move_perc') * 100:+.2f}%" if r.get("implied_move_perc") is not None else "N/A",
        })
    return rows[:10]


def level_migration(snapshot: dict, prev_snapshot: dict | None) -> list[dict]:
    cur = snapshot.get("levels", {})
    prev = (prev_snapshot or {}).get("levels", {})
    rows = []
    pairs = [
        ("Net GEX", snapshot.get("net_gamma_total"),
         (prev_snapshot or {}).get("net_gamma_total")),
        ("Gamma Flip", cur.get("regime_threshold"), prev.get("regime_threshold")),
        ("Call Wall", cur.get("upper_resistance_band"), prev.get("upper_resistance_band")),
        ("Put Support", cur.get("lower_support_band"), prev.get("lower_support_band")),
        ("Gamma Magnet", cur.get("equilibrium_point"), prev.get("equilibrium_point")),
    ]
    for label, now, before in pairs:
        if now is None:
            continue
        delta = (now - before) if before is not None else None
        rows.append({
            "level": label,
            "now": now,
            "prev": before,
            "delta": delta,
        })
    return rows


# The live raw file behind each panel (snapshot.LIVE_FILES); see run._historical_live_files. The VIX and CFTC panels
# have none since PR 3: they read the market store for the day shown.
_PANEL_FILES = {
    "iv_rank": "iv_rank.json",
    "vrp": "variance_risk_premium.json",
    "realized_vol": "realized_vol.json",
    "options_volume": "options_volume.json",
    "max_pain": "max_pain.json",
    "vol_term": "vol_term_structure.json",
    "skew": "interpolated_iv.json",
    "iron_butterfly": "option_chain.json",
}


def _historical_panels(snapshot: dict) -> dict[str, bool]:
    """Panel -> True when its live file was fetched in a later session than the day shown."""
    live = set(snapshot.get("historical_live_files") or [])
    return {panel: fn in live for panel, fn in _PANEL_FILES.items()}


def store_labels(snapshot: dict) -> dict[str, str]:
    """Market-store input -> the line naming where it came from (its date, source and status), or "<input> not
    stored for <day>" (PR 3). Empty for a snapshot built without the store."""
    inputs = snapshot.get("store_inputs") or {}
    return {k: str(v.get("label")) for k, v in sorted(inputs.items()) if isinstance(v, dict) and v.get("label")}


def store_notes(snapshot: dict) -> dict[str, str]:
    """Market-store input -> its "not stored for <day>" label, for each input the store did not hold (PR 3)."""
    inputs = snapshot.get("store_inputs") or {}
    return {k: str(v.get("label")) for k, v in sorted(inputs.items())
            if isinstance(v, dict) and v.get("stored") is False}


# Quality flags (normalize.normalize_snapshot) -> the phrase the banner names them by.
_QUALITY_LABELS = {
    "core_levels_available": "core GEX levels",
    "charm_available": "charm",
    "vanna_available": "vanna",
    "iv_available": "implied volatility",
    "candle_data_available": "candles",
    "max_pain_available": "max pain",
    "vix_available": "VIX",
    "cftc_available": "CFTC",
}


def _manifest_warnings(data_root, asof) -> list[str]:
    """The Fetch manifest's ``warnings`` for the day shown (best-effort; [] when absent)."""
    if not data_root or not asof:
        return []
    path = Path(data_root) / "raw" / str(asof) / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    warns = manifest.get("warnings") if isinstance(manifest, dict) else None
    return [str(w) for w in warns] if isinstance(warns, list) else []


def data_quality_notice(snapshot: dict, data_root=None) -> str | None:
    """The amber data-quality line: the day's Fetch warnings and any quality flag that is not ok.

    ``warnings`` come from the raw day's manifest (snapshot.py); ``quality`` from the normalized
    snapshot (normalize.py). ``None`` for a clean day — the board then shows no banner.
    """
    warnings = _manifest_warnings(data_root, snapshot.get("asof_date"))
    missing = [_QUALITY_LABELS.get(k, k)
               for k, ok in sorted((snapshot.get("quality") or {}).items()) if ok is False]
    parts = []
    if warnings:
        parts.append("Fetch warnings: " + "; ".join(warnings) + ".")
    if missing:
        parts.append("Data unavailable: " + ", ".join(missing) + ".")
    not_stored = list(store_notes(snapshot).values())
    if not_stored:
        parts.append("Not stored (no live fallback): " + "; ".join(not_stored) + ".")
    return " ".join(parts) or None


def _billions(value) -> str:
    return f"${value / 1e9:,.2f}B" if value is not None else "n/a"


def pin_source_line(snapshot: dict) -> str | None:
    """The line under the pin naming the 0-1 DTE basis of its $10B test and the snapshot it came from (review S4).

    The all-expiry number is shown beside it as context only (A2). None for a snapshot built without the market
    store (no ``zero_one_dte`` meta), which renders as it always did.
    """
    meta = snapshot.get("zero_one_dte")
    if not isinstance(meta, dict):
        return None
    if not meta.get("stored"):
        return f"{meta.get('label')}: the pin's $10B test is not run and the pin flag is not raised."
    pred = snapshot.get("prediction") or {}
    magnet = pred.get("pin_magnet")
    if magnet is None:
        return f"No 0-1 DTE magnet within the pin window. {meta.get('label')}."
    return (f"Pin size at {magnet:,.0f}: {_billions(pred.get('pin_dollars'))} 0-1 DTE OI gamma (flag at $10B); "
            f"all-expiry {_billions(pred.get('pin_dollars_all_expiry'))}, context only. {meta.get('label')}.")


def build_context(snapshot: dict, prev_snapshot: dict | None = None, data_root=None) -> dict:
    levels = snapshot.get("levels", {})
    prev_levels = (prev_snapshot or {}).get("levels", {}) or {}
    charm = snapshot.get("charm_qualification", {})
    vanna = snapshot.get("vanna_peaks") or {}
    em = snapshot.get("expected_move")
    current = snapshot.get("current_level")
    asof = snapshot.get("asof_date", "")
    first = snapshot.get("first_session_date", "")
    last = snapshot.get("last_session_date", "")
    regime = snapshot.get("regime")
    prev_current = (prev_snapshot or {}).get("current_level")

    def lv(key):
        return fmt_level(levels.get(key))

    # ── week-over-week deltas (vs previous report/session) ──
    delta_map: dict[str, float | str | None] = {}
    if prev_snapshot:
        def _d(cur, prev):
            if cur is None or prev is None:
                return None
            try:
                return float(cur) - float(prev)
            except (TypeError, ValueError):
                return None
        delta_map["spot"] = _d(current, prev_current)
        delta_map["net_dex"] = _d(snapshot.get("net_dex"), (prev_snapshot or {}).get("net_dex"))
        delta_map["net_gex"] = _d(snapshot.get("net_gamma_total"), (prev_snapshot or {}).get("net_gamma_total"))
        for key, pkey in [("call_wall", "upper_resistance_band"),
                          ("put_support", "lower_support_band"),
                          ("gamma_flip", "gamma_flip_all"),
                          ("gamma_flip_0dte", "gamma_flip_0dte")]:
            cur_v = levels.get(pkey)
            prev_v = (prev_snapshot or {}).get("levels", {}).get(pkey)
            delta_map[key] = _d(cur_v, prev_v)
        if em and (prev_snapshot or {}).get("expected_move"):
            delta_map["expected_move"] = _d(em.get("lower"), (prev_snapshot or {}).get("expected_move", {}).get("lower"))

    role = _role(regime)
    em_value = f"{fmt_level(em['lower'])} – {fmt_level(em['upper'])}" if em else "N/A"
    em_pts = f"±{em['points']:,.1f}" if em else "N/A"

    charm_label = charm.get("label") or "Charm Peak"
    charm_dist = charm.get("distance_pct_from_spot")
    charm_dom = charm.get("dominance_ratio")
    charm_mode = "directionalized volume" if charm.get("mode") == "directionalized_volume" else "open interest"
    charm_disclosure = f"Highest abs net Charm in Mode A ({charm_mode}). Status: {charm_label}"
    if charm_dom is not None:
        # No spot (the SPX closes not stored and no live level, PR 3) leaves the distance unknown.
        dist_text = f"{charm_dist * 100:.1f}%" if charm_dist is not None else "n/a"
        charm_disclosure += f" (distance {dist_text}, dominance {charm_dom:.2f}x)"
    else:
        charm_disclosure += " (no runner-up)"

    details = [
        {"level": charm_label, "value": _cell(charm.get("strike"), charm.get("signed_net_charm")),
         "ref": "Strike & Signed Charm", "method": charm_disclosure},
        {"level": "Call Wall", "value": lv("upper_resistance_band"),
         "ref": "Provider GEX Level",
         "method": f"Largest positive net gamma above spot — a magnetic ceiling; dealers may sell into rallies as price approaches. Role: {role}."},
        {"level": "Gamma Magnet", "value": lv("equilibrium_point"),
         "ref": "High-GEX Concentration", "method": "Provider gex-levels output: largest-magnitude net gamma concentration."},
        {"level": "0DTE Gamma Concentration", "value": lv("zero_dte_concentration"),
         "ref": "Intraday Concentration", "method": f"Active expiration {asof}: maximum absolute call_gex + put_gex."},
        {"level": "SPX Close / Spot", "value": fmt_level(current),
         "ref": "Market Reference", "method": "Verified candle close as of snapshot market date."},
        {"level": "0DTE Put Concentration", "value": lv("zero_dte_put_concentration"),
         "ref": "Intraday Put Concentration", "method": f"Active expiration {asof}: most negative put_gex."},
        {"level": "Gamma Flip", "value": lv("regime_threshold"),
         "ref": "Regime Threshold", "method": "The volatility turning point — above = positive gamma (calm), below = negative gamma (wild)."},
        {"level": "Put Wall", "value": lv("lower_support_band"),
         "ref": "Provider GEX Level",
         "method": f"Largest positive net gamma below spot — a trampoline floor; dealers may buy into dips as price approaches. Role: {role}."},
        {"level": "0DTE Cumulative GEX Crossing", "value": lv("zero_dte_cumulative_crossing"),
         "ref": "Table-Only Proxy", "method": "Interpolated zero-crossing of cumulative same-day net GEX. Not a scenario flip."},
        {"level": "Vanna Call Peak", "value": _cell((vanna.get("call_peak") or {}).get("strike"), (vanna.get("call_peak") or {}).get("signed_call_vanna")),
         "ref": "Directionalized Volume", "method": "Highest call_vanna_vol within ±25% of the as-of spot (window scales with spot, re-derived per report date)."},
        {"level": "Vanna Put Peak", "value": _cell((vanna.get("put_peak") or {}).get("strike"), (vanna.get("put_peak") or {}).get("signed_put_vanna")),
         "ref": "Directionalized Volume", "method": "Highest put_vanna_vol within ±25% of the as-of spot (window scales with spot, re-derived per report date)."},
        {"level": "One-Day Implied Range (1σ)", "value": em_value,
         "ref": "1-Day Implied Move", "method": f"Spot × IV / √252 = {em_pts} pts. Normal 1σ approximation from 1-day IV."},
    ]

    header = {
        "ticker": snapshot.get("ticker", "SPX"),
        "price_source": snapshot.get("candle_source", ""),
        "report_date": asof,
        "sessions": f"{first} → {last}",
        "retrieved_at": snapshot.get("api_snapshot_time", ""),
        "last_sync": _last_sync(snapshot.get("api_snapshot_time"), asof),
        "source": "Unusual Whales API",
    }

    cards = build_summary(snapshot)
    vanna_signal = next((c["signal"] for c in cards if c["title"] == "Vanna"), None)
    if vanna_signal and vanna_signal["label"] == "Long":
        vanna_note = ("Long vanna: an IV rise raises dealer delta and forces stock selling to re-hedge — "
                      "a drag during vol spikes.")
    elif vanna_signal and vanna_signal["label"] == "Short":
        vanna_note = ("Short vanna: an IV rise lowers dealer delta and forces stock buying to re-hedge — "
                      "a cushion during vol spikes.")
    else:
        vanna_note = ""     # no vanna data: no claim either way (review S1)

    footer = (
        "Source: Unusual Whales API — open-position-based option exposure model. "
        f"Report date: {asof}. "
        "These levels are provider-model outputs and strike aggregations, not guaranteed "
        "support/resistance, pin certainty, forecasts, or price targets. "
        + (f"{vanna_note} A flow effect, not a support guarantee." if vanna_note else "")
    ).rstrip()

    # ── v2 components ──
    cards = build_summary(snapshot)
    # 1D/1W deltas per positioning metric, keyed by summary card title. One history read for the whole report:
    # each card's pill and the header strip's GEX deltas (#49 — it used to be loaded and derived a second time).
    try:
        from .run import _load_history as _lh
        _hist = _lh(Path(data_root) / HISTORY_FILENAME) if data_root else _lh()
    except Exception:  # noqa: BLE001 — history is best-effort
        _hist = {}
    _g_d = compute_positioning_deltas(snapshot.get("net_gamma_total"), "net_gex_oi", _hist, asof=snapshot.get("asof_date"))
    _dex_d = compute_positioning_deltas(snapshot.get("net_dex"), "net_dex", _hist, asof=snapshot.get("asof_date"))
    # Each card's pill and 1D/1W deltas measure the number the card shows (DF19).
    _card_delta_key = {"Delta": "net_dex", "Gamma": "net_gex"}
    _card_deltas = {
        "Delta": _dex_d,        # Net DEX
        "Gamma": _g_d,          # Net GEX (OI)
        "Vanna": compute_positioning_deltas(snapshot.get("vanna_at_spot"), "vanna_at_spot", _hist,
                                            asof=snapshot.get("asof_date")),   # Net Vanna at spot
        "Charm": compute_positioning_deltas(snapshot.get("charm_live_sum"), "charm_live_sum", _hist,
                                            asof=snapshot.get("asof_date")),   # Live charm (summed)
    }
    for c in cards:
        k = _card_delta_key.get(c.get("title"))
        dv = delta_map.get(k) if k in delta_map else None
        if dv is not None:
            dv = round(dv, 0) if abs(dv) >= 1000 else round(dv, 2)
        c["delta"] = dv
        _d = _card_deltas.get(c.get("title"))
        c["delta_1d"] = _d["delta_1d"] if _d else None
        c["delta_1w"] = _d["delta_1w"] if _d else None
    env = snapshot.get("gex_envelope") or {}
    relations = greek_relations(snapshot)
    chain = causal_chain(snapshot)
    scenarios = build_scenarios(snapshot)
    brier = scorecard(path=Path(data_root) / BRIER_FILENAME) if data_root else scorecard()
    signal = signal_matrix(cards)

    _vix = (snapshot.get("vix") or {}).get("vix")
    _net_delta = delta_map.get("net_gex")

    # ── the header strip (its GEX delta pair comes from the history read above; the ``matrix_gex`` pair
    #    nobody read is gone, #49) ──
    header_strip = {
        # Row 1 — Structural / Macro (OI)
        "spot": fmt_level(current),
        "call_wall": lv("upper_resistance_band"),
        "put_support": lv("lower_support_band"),
        "gamma_flip": lv("gamma_flip_all"),
        "gamma_magnet": lv("equilibrium_point"),
        "charm": _cell(charm.get("strike"), charm.get("signed_net_charm")),
        "vanna": _cell((vanna.get("call_peak") or {}).get("strike"),
                       (vanna.get("call_peak") or {}).get("signed_call_vanna")),
        # Row 1b — Intraday Flow (Volume)
        "call_wall_vol": lv("call_wall_vol"),
        "put_wall_vol": lv("put_wall_vol"),
        "gamma_flip_vol": lv("gamma_flip_vol"),
        # Row 2 — GEX (with 1D/1W deltas)
        "net_gex": fmt_signed_compact(snapshot.get("net_gamma_total")),
        "net_gex_delta_1d": fmt_signed_compact(_g_d["delta_1d"]) if _g_d["delta_1d"] else "—",
        "net_gex_delta_1w": fmt_signed_compact(_g_d["delta_1w"]) if _g_d["delta_1w"] else "—",
        "net_dex": fmt_signed_compact(snapshot.get("net_dex")),
        "net_vanna": fmt_signed_compact(snapshot.get("net_vanna")),
        "live_charm": f"{snapshot.get('live_charm')/1e9:.2f}B" if snapshot.get("live_charm") is not None else "—",
        "matrix_gex": fmt_signed_compact(snapshot.get("matrix_gex")),
        # The chip is the regime (spot vs flip); with no regime it says so instead of defaulting to NEGATIVE (G12).
        "source_chip": snapshot.get("source_chip") or "UNKNOWN",
        # The two GEX numbers are coloured by their own sign, not by the regime chip.
        "net_gex_tone": _tone(snapshot.get("net_gamma_total")),
        "matrix_gex_tone": _tone(snapshot.get("matrix_gex")),
        # Row 3 — regime + VIX
        "regime": regime or "Regime unknown",
        "vix": f"{_vix:.2f}" if _vix is not None else "—",
        # legacy keys (kept for md template compatibility)
        "net_gex_delta": fmt_signed_compact(_net_delta) if _net_delta is not None else "—",
        "put_wall_all": lv("put_wall_all"),
        "gamma_flip_0dte": lv("gamma_flip_0dte"),
        "expected_move": em_value,
    }

    absorption = env.get("absorption") or {}
    air_gap = env.get("air_gap") or {}

    return {
        "header": header,
        "at_glance": at_a_glance(cards),
        "delta_map": delta_map,
        "flow_signal": evaluate_flow_signal(snapshot),
        "summary": cards,
        "details": details,
        "walkthrough": build_walkthrough(snapshot),
        "how_to_read": {
            "call_wall": lv("upper_resistance_band"),
            "put_wall": lv("lower_support_band"),
            "flip": lv("regime_threshold"),
        },
        "footer": footer,
        "quality_notice": data_quality_notice(snapshot, data_root),
        "header_strip": header_strip,
        "gex_envelope": {
            "absorption": {
                "range": f"{absorption.get('lo'):,.0f}–{absorption.get('hi'):,.0f}" if absorption.get("lo") is not None else "N/A",
                "net_gex": fmt_signed_compact(absorption.get("net_gex")),
                "per_25pt": f"{absorption.get('per_25pt'):,.1f}" if absorption.get("per_25pt") is not None else "N/A",
            },
            "air_gap": {
                "range": f"{air_gap.get('lo'):,.0f}–{air_gap.get('hi'):,.0f}" if air_gap.get("lo") is not None else "N/A",
                "net_gex": fmt_signed_compact(air_gap.get("net_gex")),
                "per_25pt": f"{air_gap.get('per_25pt'):,.1f}" if air_gap.get("per_25pt") is not None else "N/A",
            },
            "zero_crossing": fmt_level(env.get("zero_crossing")),
        },
        "relations": relations,
        "chain": chain,
        "glossary_tr": GLOSSARY_TR,
        "scenarios": scenarios,
        "brier": brier,
        "signal_matrix": signal,
        "top_gex": top_gex_strikes(snapshot),
        "migration": level_migration(snapshot, prev_snapshot),
        "summary_expiry": _expiry_summary(snapshot),
        "prediction": snapshot.get("prediction") or {},
        "pin_line": pin_source_line(snapshot),
        "store_notes": store_notes(snapshot),
        "store_labels": store_labels(snapshot),
        "iron_butterfly": snapshot.get("iron_butterfly"),
        # ── v3 external panels ──
        "max_pain": _max_pain_rows(snapshot),
        "iv_rank": snapshot.get("iv_rank"),
        "options_volume": snapshot.get("options_volume"),
        "vol_term": _vol_term_rows(snapshot),
        "vrp": snapshot.get("variance_risk_premium"),
        "realized_vol": snapshot.get("realized_vol"),
        "skew": snapshot.get("skew"),
        "vix": snapshot.get("vix"),
        "cftc": snapshot.get("cftc"),
        # Live panels in a backfilled day hold a later session's value: shown "n/a (historical)".
        "historical": _historical_panels(snapshot),
        # ── v4 provider comparison ──
        "dte_band_walls": _dte_band_rows(snapshot),
        "flip_methods": _flip_methods(snapshot),
    }


def _dte_band_rows(snapshot: dict) -> list[dict]:
    """DTE-band walls formatted for the table."""
    rows = []
    for w in snapshot.get("dte_band_walls", []):
        rows.append({
            "band": w["band"],
            "put_wall": fmt_level(w.get("put_wall")),
            "put_wall_net": fmt_signed_compact(w.get("put_wall_net")),
            "call_wall": fmt_level(w.get("call_wall")),
            "call_wall_net": fmt_signed_compact(w.get("call_wall_net")),
        })
    return rows


def _tone(value) -> str:
    """CSS tone of a signed number by its own sign; empty for missing or zero."""
    if value is None or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


def _flip_methods(snapshot: dict) -> list[dict]:
    """The gamma flip by each method, side by side, with which side of it spot is on.

    UW does not document how it computes its flip, and the OI and volume flips can differ, so
    all of them are shown. Only the first (UW, open interest) decides the regime; the others
    are cross-checks.
    """
    d = snapshot.get("regime_detail") or {}
    flips = d.get("flips") or {}
    sides = d.get("sides") or {}
    side_text = {"long": "spot above (long gamma)", "short": "spot below (short gamma)",
                 "at_flip": "spot at the flip", None: "—"}
    rows = [
        ("UW flip, open interest (decides the regime)", "oi", "UW gex-levels source=oi"),
        ("UW flip, volume", "vol", "UW gex-levels source=vol"),
        ("Local cumulative crossing, open interest book", "cumulative",
         "interpolated zero of cumulative OI gamma across strikes, nearest to spot"),
    ]
    return [{"method": name, "value": fmt_level(flips.get(key)),
             "side": side_text.get(sides.get(key), "—"), "note": note}
            for name, key, note in rows]


def _last_sync(stamp, fallback) -> str:
    """'23 Oct' from the fetch stamp (UTC), as the New York day it fell on (AGENTS.md section 6: no raw timestamp).

    A stored day without a stamp, or with one that does not parse, shows the report date instead.
    """
    from datetime import date, datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        day = dt.astimezone(ZoneInfo("America/New_York")).date()
    except (ValueError, TypeError):
        try:
            day = date.fromisoformat(str(fallback)[:10])
        except ValueError:
            return str(fallback or "—")
    return f"{day.day} {day:%b}"


def render_markdown(snapshot: dict, chart_png: str | None = None, prev_snapshot: dict | None = None,
                    data_root=None) -> str:
    ctx = build_context(snapshot, prev_snapshot, data_root=data_root)
    ctx["chart_image"] = chart_png
    return _MD_ENV.get_template("section_position_map.md.j2").render(**ctx)


# The shared "What am I looking at?" box (#383 C3); fixed text, so it goes in as Markup.
HELP = help_html(
    "Where SPX option dealers are positioned for one trading day: gamma exposure by strike, the levels "
    "where hedging tends to pin or push the index, the expected move and a directional read with its "
    "confidence. The cause-and-effect chain below walks through each step. <b>Fetch</b> downloads the "
    "SPX option chain and flow for the chosen date from <b>Unusual Whales</b> (a past day already "
    "stored is rebuilt from disk). <b>Refresh</b> rebuilds the newest stored day from disk with no "
    "network call.")


def render_html(snapshot: dict, chart_png: str | None = None, chart_html: str | None = None,
                prev_snapshot: dict | None = None, tornado_html: str | None = None,
                envelope_html: str | None = None, data_root=None) -> str:
    ctx = build_context(snapshot, prev_snapshot, data_root=data_root)
    ctx["chart_image"] = chart_png
    # The only trusted markup: the three Plotly fragments this package renders itself.
    ctx["chart_html"] = Markup(chart_html) if chart_html else chart_html
    ctx["tornado_html"] = Markup(tornado_html) if tornado_html else tornado_html
    ctx["envelope_html"] = Markup(envelope_html) if envelope_html else envelope_html
    ctx["help_block"] = Markup(HELP)
    # The shell's palette (dark; light under data-theme), the one place it is defined.
    ctx["theme_css"] = Markup(THEME_CSS)
    ctx["theme_js"] = Markup(THEME_JS)        # applies the reader's stored choice before the page paints
    # Escapes every provider string itself (Common K3); empty for a clean run.
    ctx["provider_banner"] = Markup(provider_health_html(snapshot.get("provider_health")))
    # The Fetch warnings and any non-ok quality flag (M-SPX-5); empty for a clean day.
    ctx["quality_banner"] = Markup(notice_html(ctx.get("quality_notice")))
    return _HTML_ENV.get_template("section_position_map.html.j2").render(**ctx)
