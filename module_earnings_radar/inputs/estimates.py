"""E0e, D14 (A6, #323): analyst estimates per calendar ticker and period, from yfinance ``earningsTrend``.

One yfinance request per ticker (``core/providers/yfinance.get_earnings_trend``), nearest earnings first, inside the
night's yfinance cap; five failures in a row stop the sweep (Yahoo throttling or down). Each answer gives up to four
rows (periods ``0q``, ``+1q``, ``0y``, ``+1y``) with the 21 value columns of ``estimate_snapshots``; a field Yahoo
does not give stays NULL, never 0 (NaN included). Field names are matched without case (Yahoo has served
``downLast7Days`` as well as ``downLast7days``). ``payload`` keeps the period's four blocks. Key (ticker,
session_date, period); a second run of the night asks only for the tickers it has not stored.

``probe`` is the PC's read-only M1 check: it prints the installed yfinance version, the requests per ticker and the
fields each block gives, and writes nothing.
"""
from __future__ import annotations

import time
from datetime import date

from RevenueChaser.core import nightly_inputs
from RevenueChaser.core.providers import yfinance as yf_provider
from RevenueChaser.module_earnings_radar.inputs._common import (
    CALL_CAP, calendar_tickers, error_of, json_safe, num, outcome, stored_tickers)

TABLE = "estimate_snapshots"
PERIODS = ("0q", "+1q", "0y", "+1y")
# block -> (column prefix, {lower-case yfinance field: column suffix})
MAP = {
    "earnings_estimate": ("eps_est", {"numberofanalysts": "analysts", "avg": "avg", "low": "low", "high": "high",
                                      "yearagoeps": "year_ago", "growth": "growth"}),
    "revenue_estimate": ("rev_est", {"numberofanalysts": "analysts", "avg": "avg", "low": "low", "high": "high",
                                     "yearagorevenue": "year_ago", "growth": "growth"}),
    "eps_trend": ("eps_trend", {"current": "current", "7daysago": "7d", "30daysago": "30d", "60daysago": "60d",
                                "90daysago": "90d"}),
    "eps_revisions": ("eps_rev", {"uplast7days": "up_7d", "uplast30days": "up_30d", "downlast7days": "down_7d",
                                  "downlast30days": "down_30d"}),
}
VALUE_COLUMNS = tuple(f"{prefix}_{suffix}" for prefix, fields in MAP.values() for suffix in fields.values())
FAIL_STREAK = 5
FLUSH_EVERY = 100


def trend_rows(ticker: str, blocks: dict, session_date: date, stamps: dict) -> list[dict]:
    """The ``estimate_snapshots`` rows of one answer: one per period that has at least one value."""
    rows = []
    for period in PERIODS:
        values = dict.fromkeys(VALUE_COLUMNS)
        payload: dict = {}
        for block, (prefix, fields) in MAP.items():
            for field, by_period in ((blocks or {}).get(block) or {}).items():
                if not isinstance(by_period, dict) or period not in by_period:
                    continue
                payload.setdefault(block, {})[str(field)] = json_safe(by_period[period])
                suffix = fields.get(str(field).lower())
                if suffix:
                    values[f"{prefix}_{suffix}"] = num(by_period[period])
        if all(v is None for v in values.values()):
            continue
        rows.append({"ticker": ticker, "session_date": session_date, "period": period, **values,
                     "payload": payload, "source": "yfinance", **stamps})
    return rows


def run(ctx) -> dict:
    stored = stored_tickers(TABLE, ctx.session_date)
    tickers = [t for t in calendar_tickers(ctx.session_date) if t not in stored]
    budget = ctx.budget("yfinance")
    rows: list[dict] = []
    errors: list[str] = []
    asked = empty = streak = not_asked = 0
    written = {"offered": 0, "written": 0}

    def flush():
        got = nightly_inputs.record_first(TABLE, rows)
        written["offered"] += got["offered"]
        written["written"] += got["written"]
        rows.clear()

    for n, ticker in enumerate(tickers):
        if streak >= FAIL_STREAK:
            not_asked = len(tickers) - n
            ctx.log(f"D14: {FAIL_STREAK} yfinance failures in a row; {not_asked} tickers not asked")
            break
        if not budget.take():
            not_asked = len(tickers) - n
            errors.append(CALL_CAP)
            asked += 1
            break
        asked += 1
        res = yf_provider.get_earnings_trend(ticker)
        if res.status != "ok":
            errors.append(error_of(res))
            streak += 1
            continue
        streak = 0
        got = trend_rows(ticker, res.data, ctx.session_date, ctx.stamps(time.time()))
        empty += not got
        rows += got
        if len(rows) >= FLUSH_EVERY * len(PERIODS):
            flush()
    flush()
    return outcome(asked, errors, tickers=len(tickers), empty=empty, not_asked=not_asked, **written)


def probe(tickers=("AAPL", "MSFT", "JPM"), out=print) -> int:
    """M1 (read only, ``len(tickers)`` yfinance reads, no store access): what the installed yfinance gives D14."""
    import yfinance

    out(f"yfinance {yfinance.__version__}")
    counted = {"n": 0}
    try:
        from yfinance.data import YfData
        original = YfData.get_raw_json

        def counting(self, *a, **k):
            counted["n"] += 1
            return original(self, *a, **k)

        YfData.get_raw_json = counting
    except (ImportError, AttributeError):
        original = None
        out("request count: not measured (no YfData.get_raw_json in this version)")
    try:
        for ticker in tickers:
            before = counted["n"]
            res = yf_provider.get_earnings_trend(ticker)
            out(f"{ticker}: {res.status}" + (f" {res.error}" if res.error else "")
                + (f", {counted['n'] - before} request(s)" if original else ""))
            for block in MAP:
                out(f"  {block}: {sorted((res.data or {}).get(block) or {})}")
            for row in trend_rows(ticker, res.data, date.today(), {}):
                filled = sum(row[c] is not None for c in VALUE_COLUMNS)
                out(f"  {row['period']}: {filled}/{len(VALUE_COLUMNS)} columns filled")
    finally:
        if original is not None:
            YfData.get_raw_json = original
    return 0


SWEEP = nightly_inputs.Sweep("D14", (TABLE,), run)
