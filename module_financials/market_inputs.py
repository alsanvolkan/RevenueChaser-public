"""Financials' market inputs from the prices store (``prices.duckdb``, G2): the price, the market cap, the shares and
the beta the models read, Yahoo's values only where the store has none.

- **Price:** the newest close (``rcdata.read.latest_market``, at most ``LATEST_WITHIN_DAYS`` behind the store).
- **Market cap:** the issuer's (every listed class), as the universe reads it and as Yahoo's was; a fund's or a
  trust's its own (one trust's CIK files many funds), and the class's own when the store has no issuer sum. It is
  the WACC's equity weight.
- **Shares:** the market cap over the price, the class-equivalent count. For one class it is the shares outstanding;
  for an issuer with two classes it is the count that makes the per-share value comparable with this class's price.
- **Beta:** the slope of ``BETA_MONTHS`` monthly returns of the split-adjusted close on the S&P 500 level
  (``index:SPX``), Yahoo's own definition (five years, monthly). Price returns on both sides: the index level carries
  no dividends. Fewer than ``BETA_MIN_MONTHS`` returns, and Yahoo's beta stays.

Each value the store does not give keeps Yahoo's, and the Source line names which came from where.
"""
from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

BETA_MONTHS = 60
BETA_MIN_MONTHS = 36
INDEX_ID = "index:SPX"
STORE = "prices store"


def _month_ends(rows: list[tuple[date, float]], before: date):
    """The last close of each calendar month before ``before``'s month, as a pandas Series on a monthly PeriodIndex
    that has every month in its span (a month with no bar is NaN, so no return spans a gap)."""
    import pandas as pd
    kept = [(d, c) for d, c in rows if c is not None and c == c and c > 0 and (d.year, d.month) < (before.year,
                                                                                                    before.month)]
    if not kept:
        return pd.Series(dtype=float)
    s = pd.Series([c for _, c in kept], index=pd.PeriodIndex([d for d, _ in kept], freq="M"))
    s = s.groupby(level=0).last()
    return s.reindex(pd.period_range(s.index.min(), s.index.max(), freq="M"))


def monthly_beta(stock: list[tuple[date, float]], index: list[tuple[date, float]], before: date) -> float | None:
    """The beta of ``stock`` on ``index`` over the last ``BETA_MONTHS`` monthly returns of complete months before
    ``before``; None with fewer than ``BETA_MIN_MONTHS`` months where both have a return."""
    s = _month_ends(stock, before).pct_change(fill_method=None)
    m = _month_ends(index, before).pct_change(fill_method=None)
    both = s.to_frame("s").join(m.to_frame("m"), how="inner").dropna().tail(BETA_MONTHS)
    if len(both) < BETA_MIN_MONTHS or both["m"].var() == 0:
        return None
    return float(both["s"].cov(both["m"]) / both["m"].var())


def _is_fund(ticker: str) -> bool:
    """A fund, trust or other excluded type (``identity.types.EXCLUDED_TYPES``) by the universe's own type seam: its
    cap is the class's own, because one trust's CIK files many funds (``core/universe.py``'s ``_spine_universe``)."""
    from RevenueChaser.core import universe
    try:
        return ticker in universe.excluded_tickers()
    except Exception as exc:  # noqa: BLE001 - an unreadable type reads as an operating company: the issuer's cap
        logger.warning("financials: the security type of %s could not be read (%s); the issuer's cap counts",
                       ticker, exc)
        return False


def from_store(ticker: str) -> dict:
    """``{current_price, market_cap, shares_outstanding, beta, price_session, beta_months}`` from the prices store,
    each only when the store gives it; ``{}`` with no prices file or a failed read (logged)."""
    from RevenueChaser.core import spine_reads
    from RevenueChaser.rcdata import read
    t = str(ticker).strip().upper()
    try:
        latest = spine_reads.latest_market([t]).get(t) or {}
    except Exception as exc:  # noqa: BLE001 - Yahoo's values stay, as before the prices store
        logger.warning("financials: the prices store could not be read for %s (%s); Yahoo's price, shares and "
                       "beta stay", t, exc)
        return {}
    price, session = latest.get("price"), latest.get("price_session")
    if price is None or session is None or not price > 0:
        return {}
    out = {"current_price": float(price), "price_session": session}
    cap = latest.get("market_cap") if _is_fund(t) else latest.get("issuer_market_cap")
    if cap is None:
        cap = latest.get("market_cap")
    if cap is not None and cap > 0:
        out["market_cap"] = float(cap)
        out["shares_outstanding"] = float(cap) / float(price)
    try:
        start = date(session.year - (BETA_MONTHS // 12) - 1, session.month, 1)
        bars = read.ticker_bars([t], start=start, end=session, basis="split")
        stock = [(ts.date(), float(c)) for ts, c in zip(bars["session_date"], bars["close"])]
        index = [(r["session_date"], r["close"]) for r in read.bars([INDEX_ID], start=start, end=session)]
    except Exception as exc:  # noqa: BLE001 - Yahoo's beta stays
        logger.warning("financials: the bars for %s's beta could not be read (%s); Yahoo's beta stays", t, exc)
        return out
    beta = monthly_beta(stock, index, session)
    if beta is not None:
        out["beta"] = beta
    return out


def overlay(ticker: str, financials: dict) -> tuple[dict, str]:
    """``(financials with the store's market inputs, the Source line's market part)``. A value the store does not
    give keeps Yahoo's; the part names each side, e.g. ``prices store (price, shares) · Yahoo (beta)``."""
    store = from_store(ticker)
    got = {"price": "current_price" in store, "shares": "shares_outstanding" in store, "beta": "beta" in store}
    fields = {k: store[k] for k in ("current_price", "market_cap", "shares_outstanding", "beta") if k in store}
    meta = {"from_store": [k for k, v in got.items() if v], "price_session": store.get("price_session")}
    if got["beta"]:
        fields["beta_source"] = f"{STORE} ({BETA_MONTHS // 12}y monthly vs S&P 500)"
    out = {**financials, **fields, "market_meta": meta}
    ours = [k for k, v in got.items() if v]
    theirs = [k for k, v in got.items() if not v]
    parts = ([f"{STORE} ({', '.join(ours)})"] if ours else []) + ([f"Yahoo ({', '.join(theirs)})"] if theirs else [])
    return out, " · ".join(parts)
