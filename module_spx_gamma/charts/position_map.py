"""Two-panel option position map: SPX candles + levels (top), tornado (bottom).

Top panel: 8-week SPX candlestick window with validated level overlays and
gamma-regime shading (unchanged behaviour). Bottom panel: strike-based signed
Net GEX tornado profile (diverging horizontal bars).
"""
from __future__ import annotations

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .. import expected_move
from ..config import CHARM_AXIS_LIMIT_PCT
from .tornado import _level_entries, bar_value_and_overflow, tornado_range, tornado_rows

BG = "#0b1220"
GRID = "#1e293b"
TEXT = "#cbd5e1"

ORANGE = "#e69f00"
PURPLE = "#cc79a7"
VERMILION = "#d55e00"
BLUE = "#0072b2"
YELLOW = "#f0e442"
GREEN = "#009e73"
LIGHT_BLUE = "#7dd3fc"
SKY = "#56b4e9"
PINK = "#ff7bac"
TEAL = "#64d8cb"

UP = "#4ca58d"
DOWN = "#d96c6c"

TORNADO_GREEN = "#1e9e63"
TORNADO_RED = "#d64550"


def _merged(entries: list) -> list[dict]:
    merged = []
    for label, value, color, dash, width in entries:
        for m in merged:
            if abs(m["value"] - value) < 0.5:
                m["label"] += f" / {label}"
                break
        else:
            merged.append({"label": label, "value": value, "color": color, "dash": dash, "width": width})
    return merged


def _add_level(fig: go.Figure, value: float, label: str, color: str, dash: str, width: float, row: int,
               x0: str, x1: str) -> None:
    fig.add_shape(
        type="line", xref="x", yref="y", x0=x0, x1=x1,
        y0=value, y1=value, line=dict(color=color, width=width, dash=dash),
        opacity=0.35, row=row, col=1,
    )
    fig.add_annotation(
        x=1.015, xref="paper", y=value, yref="y",
        text=f"<b>{label}</b>  {value:,.0f}",
        xanchor="left", showarrow=False,
        font=dict(color=color, size=11),
        row=row, col=1,
    )


def build(snapshot: dict, include_tornado: bool = True) -> go.Figure | None:
    candles = snapshot.get("candles", [])
    if len(candles) < 2:
        return None

    levels = snapshot.get("levels", {})
    charm = snapshot.get("charm_qualification", {})
    vanna = snapshot.get("vanna_peaks") or {}
    em = expected_move.shown(snapshot)      # no band from a later session's IV (review S1)
    current = snapshot.get("current_level")
    asof = snapshot.get("asof_date", "")
    first = snapshot.get("first_session_date", "")
    last = snapshot.get("last_session_date", "")

    dates = [c["date"] for c in candles]
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    # Cboe's SPX file, the market store's window since PR 3, has one column: a close and no open/high/low. Such a
    # window is drawn as a close line, since a candlestick without its OHLC draws nothing.
    has_ohlc = any(v is not None for v in opens + highs + lows)

    tornado_rows_data = tornado_rows(snapshot) if include_tornado else []
    has_tornado = include_tornado and bool(tornado_rows_data)
    tmin, tmax = tornado_range(tornado_rows_data) if has_tornado else (None, None)

    row_heights = [0.62, 0.38] if has_tornado else [1.0]
    rows = 2 if has_tornado else 1
    fig = make_subplots(
        rows=rows, cols=1,
        row_heights=row_heights,
        shared_xaxes=False,
        vertical_spacing=0.06,
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=BG,
        plot_bgcolor=BG,
        font=dict(color=TEXT, size=12),
        title=dict(text=f"SPX Option Position Map — {asof}", font=dict(size=16)),
        autosize=True,
        # 700 px; a phone's page script shortens it to 70% of the screen (section_position_map.html.j2).
        height=1180 if has_tornado else 700,
        margin=dict(l=70, r=250, t=75, b=40),
        hoverlabel=dict(bgcolor="#111a2c"),
        annotations=[
            dict(
                text=f"8-Week Price Window [{first} to {last}]",
                xref="paper", yref="paper", x=0, y=1.06,
                showarrow=False, font=dict(size=12, color=TEXT),
            ),
            dict(
                text=("Candles: yfinance (^GSPC)" if has_ohlc else "Closes: Cboe SPX (market store)")
                + ". Option levels: Unusual Whales API.",
                xref="paper", yref="paper", x=0, y=-0.02,
                showarrow=False, font=dict(size=9, color="#64748b"),
            ),
        ],
    )

    # ── TOP PANEL (row 1): candles + levels ──
    put_wall = levels.get("lower_support_band")
    call_wall = levels.get("upper_resistance_band")
    flip = levels.get("regime_threshold")            # all-tenor flip (OI)
    flip_0dte = levels.get("gamma_flip_0dte")        # 0DTE-ish flip (vol mode)
    anchors = [v for v in [put_wall, call_wall, current, flip, flip_0dte,
                           levels.get("zero_dte_concentration"),
                           levels.get("zero_dte_put_concentration"),
                           em["lower"] if em else None,
                           em["upper"] if em else None] if v is not None]
    anchors += [v for v in (highs + lows if has_ohlc else closes) if v is not None]

    charm_strike = charm.get("strike")
    charm_in_chart = (
        charm_strike is not None and current is not None
        and abs(charm_strike - current) / current <= CHARM_AXIS_LIMIT_PCT
    )
    if charm_in_chart:
        anchors.append(charm_strike)

    vanna_call = (vanna.get("call_peak") or {}).get("strike")
    vanna_put = (vanna.get("put_peak") or {}).get("strike")
    vanna_call_in_chart = (
        vanna_call is not None and current is not None
        and abs(vanna_call - current) / current <= CHARM_AXIS_LIMIT_PCT
    )
    vanna_put_in_chart = (
        vanna_put is not None and current is not None
        and abs(vanna_put - current) / current <= CHARM_AXIS_LIMIT_PCT
    )
    if vanna_call_in_chart:
        anchors.append(vanna_call)
    if vanna_put_in_chart:
        anchors.append(vanna_put)

    ymin, ymax = min(anchors), max(anchors)
    pad = max(25.0, 0.08 * (ymax - ymin))
    ymin, ymax = ymin - pad, ymax + pad

    if flip is not None:
        fig.add_shape(type="rect", xref="x", yref="y", x0=dates[0], x1=dates[-1],
                      y0=flip, y1=ymax, fillcolor=UP, opacity=0.08, line_width=0, row=1, col=1)
        fig.add_shape(type="rect", xref="x", yref="y", x0=dates[0], x1=dates[-1],
                      y0=ymin, y1=flip, fillcolor=DOWN, opacity=0.08, line_width=0, row=1, col=1)

    # Dealer Flow bands (liquidity layer) as horizontal zones on the price panel.
    env = snapshot.get("gex_envelope") or {}
    for _kind, _lo, _hi, _color in (
        ("absorption", (env.get("absorption") or {}).get("lo"), (env.get("absorption") or {}).get("hi"), UP),
        ("air_gap", (env.get("air_gap") or {}).get("lo"), (env.get("air_gap") or {}).get("hi"), DOWN),
    ):
        if _lo is not None and _hi is not None and _lo < _hi and ymin <= _lo and _hi <= ymax:
            fig.add_shape(type="rect", xref="x", yref="y", x0=dates[0], x1=dates[-1],
                          y0=_lo, y1=_hi, fillcolor=_color, opacity=0.06, line_width=0, row=1, col=1)

    if em and len(dates) >= 2:
        x0 = dates[-4] if len(dates) >= 4 else dates[0]
        fig.add_shape(
            type="rect", xref="x", yref="y", x0=x0, x1=dates[-1],
            y0=em["lower"], y1=em["upper"],
            fillcolor="rgba(230,159,0,0.08)",
            line=dict(color=ORANGE, width=1, dash="dot"),
            row=1, col=1,
        )

    if has_ohlc:
        fig.add_trace(go.Candlestick(
            x=dates, open=opens, high=highs, low=lows, close=closes,
            name="SPX",
            increasing_line_color=UP, increasing_fillcolor=UP,
            decreasing_line_color=DOWN, decreasing_fillcolor=DOWN,
        ), row=1, col=1)
    else:
        fig.add_trace(go.Scatter(x=dates, y=closes, mode="lines+markers", name="SPX close",
                                 line=dict(color=UP, width=1.6), marker=dict(size=4)), row=1, col=1)

    entries = []
    if current is not None:
        entries.append(("Current / Spot", current, ORANGE, "dot", 1.5))
    if charm_in_chart:
        entries.append((charm.get("label", "Charm Peak"), charm_strike, PURPLE, "dash", 1.5))
    if call_wall is not None:
        entries.append(("Call Wall", call_wall, VERMILION, "dash", 1.5))
    if levels.get("equilibrium_point") is not None:
        entries.append(("Gamma Magnet", levels["equilibrium_point"], SKY, "dash", 1.5))
    if levels.get("zero_dte_concentration") is not None:
        entries.append(("0DTE Gamma Concentration", levels["zero_dte_concentration"], GREEN, "dot", 1.5))
    if levels.get("zero_dte_put_concentration") is not None:
        entries.append(("0DTE Put Concentration", levels["zero_dte_put_concentration"], LIGHT_BLUE, "dot", 1.5))
    if flip is not None:
        entries.append(("Gamma Flip (all-tenor)", flip, YELLOW, "solid", 3))
    if flip_0dte is not None:
        entries.append(("Gamma Flip (0DTE)", flip_0dte, YELLOW, "dash", 1.5))
    if put_wall is not None:
        entries.append(("Put Wall", put_wall, BLUE, "dash", 1.5))
    if vanna_call_in_chart:
        entries.append(("Vanna Call Peak", vanna_call, PINK, "dot", 1.2))
    if vanna_put_in_chart:
        entries.append(("Vanna Put Peak", vanna_put, TEAL, "dot", 1.2))

    line_x0 = dates[-6] if len(dates) >= 6 else dates[0]
    line_x1 = dates[-1]
    for e in _merged(entries):
        _add_level(fig, e["value"], e["label"], e["color"], e["dash"], e["width"], row=1,
                   x0=line_x0, x1=line_x1)

    fig.update_xaxes(gridcolor=GRID, rangeslider_visible=False, range=[dates[0], dates[-1]], row=1, col=1)
    fig.update_yaxes(gridcolor=GRID, range=[ymin, ymax], row=1, col=1)

    # ── BOTTOM PANEL (row 2): tornado ──
    if has_tornado:
        strikes = [r["strike"] for r in tornado_rows_data]
        raw_vals = [r["net_gamma"] for r in tornado_rows_data]
        colors = [TORNADO_GREEN if v >= 0 else TORNADO_RED for v in raw_vals]
        cap = (tmax or 0) / 1.15
        vals = []
        overflow = []
        for v in raw_vals:
            clamped, ovf = bar_value_and_overflow(v, cap)
            vals.append(clamped)
            overflow.append(ovf)
        fig.add_trace(go.Bar(
            x=vals, y=strikes, orientation="h",
            marker=dict(color=colors, line=dict(width=0)),
            name="Signed Net GEX",
            hovertemplate="Strike %{y:,.0f}<br>Net GEX %{x:,.0f}<extra></extra>",
            showlegend=False,
        ), row=2, col=1)
        fig.add_shape(type="line", xref="x", yref="y", x0=0, x1=0,
                      y0=1.0, y1=1.0, line=dict(color="#2c3e50", width=1.4),
                      row=2, col=1)

        for e in _level_entries(snapshot, strikes[0], strikes[-1]):
            fig.add_shape(
                type="line", xref="x", yref="y", x0=1.0, x1=1.0,
                y0=e["value"], y1=e["value"],
                line=dict(color=e["color"], width=1.4, dash=e["dash"]), opacity=0.9,
                row=2, col=1,
            )
            fig.add_annotation(
                xref="paper", x=1.0, y=e["value"], yref="y",
                text=f"<b>{e['label']}</b>  {e['value']:,.0f}",
                xanchor="left", xshift=6, showarrow=False,
                font=dict(color=e["color"], size=10),
                bgcolor=BG, bordercolor=e["color"], borderpad=3,
                row=2, col=1,
            )

        for s, v, raw, ovf in zip(strikes, vals, raw_vals, overflow):
            if abs(raw) < 0.08 * cap:
                continue
            label = f"{raw:,.0f}"
            if ovf:
                label = ("▸ " if raw > 0 else "◂ ") + label
            fig.add_annotation(
                x=v + (0.03 * cap if v >= 0 else -0.03 * cap), y=s, text=label,
                showarrow=False, font=dict(color=TEXT, size=9),
                xanchor="left" if v >= 0 else "right",
                row=2, col=1,
            )

        fig.update_xaxes(
            gridcolor=GRID, range=[tmin, tmax], tickformat="~s",
            title="Signed Net GEX", row=2, col=1,
        )
        fig.update_yaxes(gridcolor=GRID, tickformat=",", row=2, col=1)

    return fig


def build_envelope_fig(snapshot: dict, window_pct: float = 0.05) -> go.Figure | None:
    """Cumulative Net GEX envelope: line + absorption/air-gap bands + zero crossing.

    Standalone chart for the Liquidity Layer — Dealer Flow section. Walks strikes
    around spot (default ±5%), plots the running cumulative signed Net GEX, shades
    the absorption (positive) and air-gap (negative) regions, and marks the zero
    crossing (approx. gamma flip).
    """
    spot = snapshot.get("current_level")
    rows = [r for r in snapshot.get("strike_exposures", [])
            if r.get("strike") is not None and r.get("net_gamma") is not None]
    if spot is None or len(rows) < 2:
        return None

    lo_all = spot * (1 - window_pct)
    hi_all = spot * (1 + window_pct)
    win = sorted((r for r in rows if lo_all <= r["strike"] <= hi_all), key=lambda r: r["strike"])
    if len(win) < 2:
        return None

    strikes = [r["strike"] for r in win]
    cum = []
    total = 0.0
    for r in win:
        total += r["net_gamma"]
        cum.append(total)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=strikes, y=cum, mode="lines+markers", name="Cumulative Net GEX",
        line=dict(color=TEXT, width=2), marker=dict(size=5, color=TEXT),
        hovertemplate="Strike %{x:,.0f}<br>Cum Net GEX %{y:,.0f}<extra></extra>",
    ))

    # absorption / air-gap zones from the computed envelope
    env = snapshot.get("gex_envelope") or {}
    for kind, color in (("absorption", UP), ("air_gap", DOWN)):
        b = env.get(kind) or {}
        lo, hi = b.get("lo"), b.get("hi")
        if lo is not None and hi is not None and lo < hi:
            fig.add_vrect(x0=lo, x1=hi, fillcolor=color, opacity=0.12,
                          line_width=0, annotation_text=kind, annotation_font_color=color)

    if env.get("zero_crossing") is not None:
        fig.add_vline(x=env["zero_crossing"], line=dict(color=YELLOW, width=1.5, dash="dash"))

    fig.add_hline(y=0, line=dict(color="#2c3e50", width=1))

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=BG,
        plot_bgcolor=BG,
        font=dict(color=TEXT, size=12),
        title=dict(text="Liquidity Layer — Cumulative Net GEX (Dealer Flow)", font=dict(size=15)),
        autosize=True,
        height=460,
        margin=dict(l=60, r=40, t=60, b=40),
        hoverlabel=dict(bgcolor="#111a2c"),
        xaxis=dict(title="Strike", gridcolor=GRID),
        yaxis=dict(title="Cumulative Net GEX", gridcolor=GRID, tickformat="~s"),
        showlegend=False,
    )
    return fig
