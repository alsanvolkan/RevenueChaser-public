"""Tornado: strike-based signed Net GEX profile (SPX) — bottom panel of the map.

Replicates the report's Sekil 1 (tornado) geometry: horizontal diverging bars
with strikes on the y-axis and signed Net GEX on the x-axis. Positive bars are
green and extend right (dampening wall); negative bars are red and extend left
(amplifying). Reference level lines (Call Wall / Spot / Gamma Flip / Put
Support / Charm / Vanna) are drawn as horizontal lines with right-edge labels.
"""
from __future__ import annotations

import plotly.graph_objects as go

from ..config import TORNADO_WINDOW_PCT


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

BG = "#0b1220"
GRID = "#1e293b"
TEXT = "#cbd5e1"

GREEN = "#1e9e63"
RED = "#d64550"
ORANGE = "#e69f00"
YELLOW = "#f0e442"
BLUE = "#0072b2"
PURPLE = "#cc79a7"
SKY = "#56b4e9"
PINK = "#ff7bac"
TEAL = "#64d8cb"


def tornado_rows(snapshot: dict, window_pct: float = TORNADO_WINDOW_PCT) -> list[dict]:
    """Strike rows inside the signed-Net-GEX window around spot, ascending."""
    spot = snapshot.get("current_level")
    if spot is None:
        return []
    lo = spot * (1 - window_pct)
    hi = spot * (1 + window_pct)
    rows = [
        r for r in snapshot.get("strike_exposures", [])
        if r.get("strike") is not None
        and r.get("net_gamma") is not None
        and lo <= r["strike"] <= hi
    ]
    return sorted(rows, key=lambda r: r["strike"])


def tornado_range(rows: list[dict], cap_pct: float = 0.95) -> tuple[float | None, float | None]:
    """Signed Net GEX x-axis range — robust symmetric cap around a quantile.

    The raw UW profile is heavily right-skewed (a few OTM strikes carry the bulk
    of exposure while the median bar is ~50x smaller). Using the max would
    flatten nearly every bar, so the axis is capped at the given quantile;
    overflow bars are drawn at the cap and flagged with a marker (their true
    value stays in the label).
    """
    vals = [abs(r["net_gamma"]) for r in rows if r.get("net_gamma") is not None]
    if not vals:
        return None, None
    vals.sort()
    i = min(len(vals) - 1, int(len(vals) * cap_pct))
    cap = vals[i]
    return -cap * 1.15, cap * 1.15


def bar_value_and_overflow(value: float, cap: float | None) -> tuple[float, bool]:
    """Clamped bar length for drawing + whether the true value overflowed."""
    if cap is None or cap <= 0:
        return value, False
    if abs(value) <= cap:
        return value, False
    return cap * (1 if value > 0 else -1), True


def _level_entries(snapshot: dict, strike_lo: float | None = None, strike_hi: float | None = None) -> list[dict]:
    """Reference levels to draw on the tornado panel (value + label + color).

    Levels outside the visible strike range are skipped so far-OTM peaks (e.g. a
    charm strike far from spot) never draw off-panel lines.
    """
    def _keep(value) -> bool:
        if value is None:
            return False
        if strike_lo is not None and value < strike_lo:
            return False
        if strike_hi is not None and value > strike_hi:
            return False
        return True

    levels = snapshot.get("levels", {})
    charm = snapshot.get("charm_qualification", {})
    vanna = snapshot.get("vanna_peaks") or {}
    entries = [
        {"value": levels.get("upper_resistance_band"), "label": "Call Wall", "color": RED, "dash": "dash"},
        {"value": snapshot.get("current_level"), "label": "Spot", "color": ORANGE, "dash": "dot"},
        {"value": levels.get("regime_threshold"), "label": "Gamma Flip", "color": YELLOW, "dash": "dash"},
        {"value": levels.get("lower_support_band"), "label": "Put Support", "color": BLUE, "dash": "dash"},
    ]
    c_strike = charm.get("strike")
    if c_strike is not None:
        entries.append({"value": c_strike, "label": charm.get("label", "Charm Peak"), "color": PURPLE, "dash": "dash"})
    v_call = (vanna.get("call_peak") or {}).get("strike")
    v_put = (vanna.get("put_peak") or {}).get("strike")
    if v_call is not None and v_call != c_strike:
        entries.append({"value": v_call, "label": "Vanna Call", "color": PINK, "dash": "dot"})
    if v_put is not None and v_put != c_strike:
        entries.append({"value": v_put, "label": "Vanna Put", "color": TEAL, "dash": "dot"})
    return [e for e in entries if _keep(e["value"])]


def build_tornado_fig(snapshot: dict, height: int = 460) -> go.Figure | None:
    """Build a standalone tornado figure for the bottom panel."""
    rows = tornado_rows(snapshot)
    if not rows:
        return None
    xmin, xmax = tornado_range(rows)
    if xmin is None:
        return None

    strikes = [r["strike"] for r in rows]
    raw_vals = [r["net_gamma"] for r in rows]
    colors = [GREEN if v >= 0 else RED for v in raw_vals]
    cap = (xmax or 0) / 1.15
    vals = []
    overflow = []
    for v in raw_vals:
        clamped, ovf = bar_value_and_overflow(v, cap)
        vals.append(clamped)
        overflow.append(ovf)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=vals, y=strikes, orientation="h",
        marker=dict(color=colors, line=dict(width=0)),
        customdata=[[fmt_signed_compact(v)] for v in raw_vals],
        hovertemplate="Strike %{y:,.0f}<br>Net GEX %{customdata[0]}<extra></extra>",
        showlegend=False,
    ))

    # zero baseline
    fig.add_shape(type="line", xref="x", yref="y", x0=0, x1=0,
                  y0=1.0, y1=1.0, line=dict(color="#2c3e50", width=1.4))

    # reference level lines + right-edge labels. The annotations are collected and set once:
    # fig.add_annotation rebuilds the whole tuple on each call (quadratic, #376).
    annotations: list[dict] = []
    slo, shi = min(strikes), max(strikes)
    for e in _level_entries(snapshot, slo, shi):
        fig.add_shape(
            type="line", xref="x", yref="y", x0=1.0, x1=1.0,
            y0=e["value"], y1=e["value"],
            line=dict(color=e["color"], width=1.4, dash=e["dash"]), opacity=0.9,
        )
        annotations.append(dict(
            xref="paper", x=1.0, y=e["value"], yref="y",
            text=f"<b>{e['label']}</b>  {e['value']:,.0f}",
            xanchor="left", xshift=6, showarrow=False,
            font=dict(color=e["color"], size=10),
            bgcolor=BG, bordercolor=e["color"], borderpad=3,
        ))

    # value labels at bar tips (only for prominent bars > 8% of cap)
    for s, v, raw, ovf in zip(strikes, vals, raw_vals, overflow):
        if abs(raw) < 0.08 * cap:
            continue
        label = "▸ " + fmt_signed_compact(raw) if ovf and raw > 0 else fmt_signed_compact(raw)
        label = "◂ " + fmt_signed_compact(raw) if ovf and raw < 0 else label
        annotations.append(dict(
            x=v + (0.03 * cap if v >= 0 else -0.03 * cap), y=s, text=label,
            showarrow=False, font=dict(color=TEXT, size=9),
            xanchor="left" if v >= 0 else "right",
        ))

    # The footer. The loop this replaces passed it to update_layout after the add_annotation
    # calls, and plotly applies a single dict given for a list property to EVERY existing element:
    # each level and bar label took the footer's text and position (only their own colours and
    # font colour survive). That is the figure the loop always produced and #376 keeps it identical
    # on purpose (golden test); the merge is done on the dicts here because update_layout over
    # hundreds of annotations is itself the slow step. Fixing the display (append the footer
    # instead) is a separate change.
    footer = dict(
        text="<span style='color:#d64550'>&larr; Negative GEX (short gamma)</span>  "
             "<span style='color:#9c9da1'>| unit: notional</span>  "
             "<span style='color:#1e9e63'>Positive GEX (long gamma) &rarr;</span>",
        xref="paper", yref="paper", x=0.5, y=-0.16,
        showarrow=False, font=dict(size=11),
    )
    if annotations:
        annotations = [{**a, **footer, "font": {**a["font"], **footer["font"]}} for a in annotations]
    else:
        annotations.append(footer)
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(color=TEXT, size=11),
        title=dict(
            text=f"Strike-Based Signed Net GEX — {snapshot.get('asof_date', '')}",
            font=dict(size=14),
        ),
        height=height,
        margin=dict(l=60, r=220, t=50, b=40),
        xaxis=dict(
            title="Signed Net GEX", range=[xmin, xmax],
            gridcolor=GRID, zeroline=False, tickformat="~s",
        ),
        yaxis=dict(
            title="Strike", gridcolor=GRID, tickformat=",",
        ),
        hoverlabel=dict(bgcolor="#111a2c"),
        annotations=annotations,
    )
    return fig
