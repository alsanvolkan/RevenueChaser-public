"""Money Flow leaderboard — 21-day rolling Normalized Net Dollar Flow.

Directional Flow_t = sign(Close_t - Close_{t-1}) * (Close_t * Volume_t)
Flow Score         = sum(Directional Flow) / sum(Close * Volume) * 100   (over window)

Scores are normalized to roughly -100..+100. The leaderboard is computed as a
separate, cached async job so the rotation tab refresh stays fast. Closes and
volumes are the stored daily bars (the prices store, ``rcdata.read.ticker_bars``,
PR 9), not a UW candle (M-ROT-3, #374).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional

from RevenueChaser.core.providers.unusual_whales import UWClient
from .universe import ROTATION_ETFS

_NOT_RANKED = ("region", "bond", "yield", "commodity")


def ranked_tickers() -> list[str]:
    """The ETFs the leaderboard ranks, in universe order: every rotation ETF but the region, bond, yield and commodity
    groups (the last two are macro/futures series with no meaningful equity dollar volume)."""
    return [t for t, info in ROTATION_ETFS.items() if info.get("group") not in _NOT_RANKED]


def bar_rows(tickers, session: date, days: int = 30) -> list[dict]:
    """``{ticker, date, close, volume}`` rows from one prices-store read (``rcdata.read.ticker_bars``, split basis),
    the last ``days`` sessions of each ticker up to ``session``; never fetches. A ticker the store holds no bar of
    has no row."""
    from RevenueChaser.rcdata import read
    frame = read.ticker_bars(list(tickers), start=session - timedelta(days=days * 2 + 14), end=session, basis="split")
    if frame is None or frame.empty:
        return []
    by: dict[str, list[dict]] = {}
    for t, d, c, v in zip(frame["ticker"], frame["session_date"], frame["close"], frame["volume"]):
        if c is None or (isinstance(c, float) and math.isnan(c)):
            continue
        vol = 0.0 if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
        day = d.date() if hasattr(d, "date") else d
        by.setdefault(str(t), []).append({"ticker": str(t), "date": day.isoformat(), "close": float(c), "volume": vol})
    rows = []
    for t in by:
        rows.extend(sorted(by[t], key=lambda r: r["date"])[-days:])
    return rows


def get_money_flow_leaderboards(df, window: int = 21, top_n: int = 4) -> dict:
    """Calculate normalized money-flow scores; return top inflows/outflows.

    ``df`` is a DataFrame with columns ticker, date, close, volume. The result
    is {inflows: [...], outflows: [...]} where each row is
    {ticker, name, flow_score}.

    Inflows are the highest positive scores and outflows the most negative ones
    (DF70, D-M8; #181 R20): a score of 0 or above is never an outflow, a score
    of 0 or below is never an inflow, so the two lists never share a ticker and
    either can hold fewer than ``top_n`` rows.
    """
    import numpy as np

    if df is None or len(df) == 0:
        return {"inflows": [], "outflows": []}

    df = df.sort_values(["ticker", "date"]).copy()
    df["dollar_vol"] = df["close"] * df["volume"]
    df["price_change"] = df.groupby("ticker")["close"].diff()
    df["directional_flow"] = df["dollar_vol"] * np.sign(df["price_change"])

    # rolling sums per ticker
    net_flow = df.groupby("ticker")["directional_flow"].rolling(window, min_periods=1).sum().reset_index(level=0, drop=True)
    tot_vol = df.groupby("ticker")["dollar_vol"].rolling(window, min_periods=1).sum().reset_index(level=0, drop=True)

    df["flow_score"] = (net_flow / tot_vol.replace(0, np.nan) * 100).fillna(0.0)
    latest = df.groupby("ticker").last().reset_index()

    def _rows(subset):
        out = []
        for _, r in subset.iterrows():
            out.append({
                "ticker": r.get("ticker"),
                "name": ROTATION_ETFS.get(r.get("ticker"), {}).get("name", ""),
                "flow_score": round(float(r.get("flow_score") or 0.0), 2),
            })
        return out

    shown = latest["flow_score"].round(2)                  # the sign as the board prints it
    inflows = _rows(latest[shown > 0].sort_values("flow_score", ascending=False).head(top_n))
    outflows = _rows(latest[shown < 0].sort_values("flow_score", ascending=True).head(top_n))
    return {"inflows": inflows, "outflows": outflows}


def compute_money_flow(uw: Optional[UWClient] = None, days: int = 30, window: int = 21, top_n: int = 4) -> dict:
    """Rank the sector/theme ETFs by money flow from the stored closes and volumes (M-ROT-3, ROT-13; PR 9).

    One ``bar_rows`` read up to the latest complete session; ``uw`` is kept for the callers' signature
    and not used. region, bond, yield and commodity groups are excluded (``ranked_tickers``).

    When the store holds no bar of some ETFs in the window, or an ETF's bars stop before the newest session read, the result
    also carries ``partial: True`` and ``missing`` (their tickers, in universe order); a whole result has neither key.
    """
    import pandas as pd
    from RevenueChaser.core import calendar

    wanted = ranked_tickers()
    try:
        session = calendar.latest_complete_session()
    except ValueError:                                  # outside the holiday table: nothing to rank
        session = None
    rows = bar_rows(wanted, session, days=days) if session else []
    if rows:
        # An ETF whose stored series stops before the newest session read is not ranked as of it (its window would end
        # weeks earlier than the others'): it is missing (review 2026-10-16).
        newest = max(r["date"] for r in rows)
        last: dict[str, str] = {}
        for r in rows:
            last[r["ticker"]] = max(last.get(r["ticker"], ""), r["date"])
        rows = [r for r in rows if last[r["ticker"]] == newest]
    got = {r["ticker"] for r in rows}
    missing = [t for t in wanted if t not in got]
    if not rows:
        result = {"inflows": [], "outflows": [], "generated_at": None, "as_of": None}
    else:
        df = pd.DataFrame(rows)
        result = get_money_flow_leaderboards(df, window=window, top_n=top_n)
        result["generated_at"] = None  # filled by the runner
        result["as_of"] = max(r["date"] for r in rows)
    if missing:
        result["partial"] = True
        result["missing"] = missing
    return result
