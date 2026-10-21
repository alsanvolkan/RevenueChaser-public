"""E0b, D10 (#296): short interest per session, for the calendar tickers and the 13F tickers.

UW has no past-date short-interest call, so a night not stored is lost (#296): this sweep stores it every night.
Ticker set: the stored calendar's tickers (nearest earnings first) and then the 13F tickers (``sec13f_totals``), all
inside the night's UW cap (decision D10, plan §11). Sources in order (2.5-4):

1. UW ``/short_screener``, 100 tickers a request (``uw_short_screener``);
2. UW ``/shorts/<t>/interest-float/v2`` for each ticker the screener did not return (``uw_interest_float``):
   the calendar's first, then the 13F tickers never stored or stored longest ago, so a cap that binds spreads the
   13F tickers over the nights; a 404 is "UW has no data", not a failure;
3. yfinance ``.info`` for a calendar ticker still missing (``yfinance``): ``si_float`` from ``shortPercentOfFloat``
   (a fraction, as UW serves it) and ``total_float`` from ``floatShares``; every other field stays NULL.

Values are stored as served (``si_float`` is a fraction, not a percent); ``payload`` keeps the source row. Key
(ticker, session_date); a second run of the night asks only for the tickers it has not stored.
"""
from __future__ import annotations

import time
from datetime import date

from RevenueChaser.core import nightly_inputs, rawstore
from RevenueChaser.core.providers import yfinance as yf_provider
from RevenueChaser.module_earnings_radar.inputs._common import (
    CALL_CAP, calendar_tickers, error_of, json_safe, num, outcome, stored_tickers, text)

TABLE = "short_interest"
CHUNK = 100
FIELDS = ("short_interest", "si_float", "total_float", "days_to_cover", "fee_rate", "rebate_rate",
          "short_shares_available")
_YF_KEYS = ("shortPercentOfFloat", "floatShares", "sharesShort", "sharesShortPriorMonth", "dateShortInterest",
            "shortRatio")


def _date(value) -> date | None:
    raw = text(value)
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def uw_row(ticker: str, item: dict, source: str, ctx, fetched_at: float) -> dict:
    return {"ticker": ticker, "session_date": ctx.session_date, "market_date": _date(item.get("market_date")),
            **{f: num(item.get(f)) for f in FIELDS}, "payload": item, "source": source, **ctx.stamps(fetched_at)}


def yf_row(ticker: str, info: dict, ctx, fetched_at: float) -> dict | None:
    si, flt = num(info.get("shortPercentOfFloat")), num(info.get("floatShares"))
    if si is None and flt is None:
        return None
    return {"ticker": ticker, "session_date": ctx.session_date, "market_date": None,
            **{f: None for f in FIELDS}, "si_float": si, "total_float": flt,
            "payload": json_safe({k: info[k] for k in _YF_KEYS if k in info}), "source": "yfinance",
            **ctx.stamps(fetched_at)}


def ticker_sets(session: date) -> tuple[list[str], list[str]]:
    """``(calendar tickers nearest first, 13F tickers not on the calendar)``."""
    cal = calendar_tickers(session)
    on_cal = set(cal)
    f13 = [r["ticker"] for r in rawstore.query("SELECT ticker FROM sec13f_totals ORDER BY ticker")
           if r.get("ticker") and r["ticker"] not in on_cal]
    return cal, f13


def _by_staleness(tickers: list[str]) -> list[str]:
    """Never stored first, then the longest ago, so a binding cap rotates through the 13F tickers night by night."""
    last = {r["ticker"]: r["d"] for r in rawstore.query(
        f"SELECT ticker, max(session_date) AS d FROM {TABLE} GROUP BY ticker")}
    return sorted(tickers, key=lambda t: (last.get(t) is not None, last.get(t) or date.min, t))


def run(ctx) -> dict:
    stored = stored_tickers(TABLE, ctx.session_date)
    cal, f13 = ticker_sets(ctx.session_date)
    cal = [t for t in cal if t not in stored]
    f13 = [t for t in f13 if t not in stored]
    uw, budget = ctx.uw(), ctx.budget("uw")
    rows: dict[str, dict] = {}
    errors: list[str] = []
    asked = 0

    for i in range(0, len(cal + f13), CHUNK):
        chunk = (cal + f13)[i:i + CHUNK]
        asked += 1
        res = uw.short_screener_result(chunk)
        if res.status == "failed":
            errors.append(error_of(res))
            continue
        fetched = time.time()
        for ticker, item in res.data.items():
            if ticker in chunk and isinstance(item, dict):
                rows[ticker] = uw_row(ticker, item, "uw_short_screener", ctx, fetched)

    misses = [t for t in cal if t not in rows] + _by_staleness([t for t in f13 if t not in rows])
    not_asked = 0
    for n, ticker in enumerate(misses):
        if budget.left <= 0:
            not_asked = len(misses) - n
            errors.append(CALL_CAP)
            asked += 1
            break
        asked += 1
        res = uw.short_interest_result(ticker)
        if res.status != "ok":
            if (res.error or {}).get("status") != 404:      # 404: UW has no row for it
                errors.append(error_of(res))
            continue
        if res.data:
            rows[ticker] = uw_row(ticker, res.data, "uw_interest_float", ctx, time.time())

    yf_budget = ctx.budget("yfinance")
    for ticker in [t for t in cal if t not in rows]:
        if not yf_budget.take():
            break
        row = yf_row(ticker, (yf_provider.get_info(ticker) or {}).get("info") or {}, ctx, time.time())
        if row is not None:
            rows[ticker] = row

    by_source: dict[str, int] = {}
    for r in rows.values():
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    missing = len(cal) + len(f13) - len(rows)
    return outcome(asked, errors, tickers=len(cal) + len(f13), missing=missing, not_asked=not_asked,
                   by_source=by_source, **nightly_inputs.record_first(TABLE, list(rows.values())))


SWEEP = nightly_inputs.Sweep("D10", (TABLE,), run)
