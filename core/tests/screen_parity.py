"""A deterministic scenario that runs the Swing and Fundamentals scans end to end (#147, 27-3a).

The scenario seeds one store (universe with quotes, bars, ticker info, identity types) and runs each board's
``run_scan`` as a Fetch, with the network and EDGAR faked. Since C12 (#383 R3) the boards read D01 bars and D04
quotes, so ``seed()`` writes those (the same bars, dated to end on the 14 Oct 2026 session, and one Yahoo quote row
per name) and ``patch()`` sets the clock seam to that evening; ``swing_ohlc`` stays empty. A Refresh no longer scores (K4, #383 R1): it shows the last
Fetch's recorded payload, which ``app/tests/test_refresh_stored_rows_383.py`` pins, so the golden holds the Fetch only
(its Refresh snapshots were dropped from the file, the Fetch and table parts are unchanged). ``snapshot()`` returns everything the
boards would show or persist, with timestamps masked and floats rounded, so it can be compared across a refactor.

``core/tests/fixtures/screen_parity_147.json`` holds the snapshot captured before the two scan bodies were folded
into ``core/screen_common.py`` (main ``ff54ba6`` plus #145 and #146). It uses only names that exist on that commit,
so the same scenario can be run on it again (``RC_PARITY_CAPTURE=<path>`` in ``test_screen_parity_147.py`` writes a
snapshot).
"""
from __future__ import annotations

import math
import time

import pandas as pd

from datetime import date, datetime, timezone

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import rawstore
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import pipeline as sp

GENERATED_AT = "2026-10-01T06:00:00+00:00"
SESSION = date(2026, 10, 14)                                                    # the bars' last session
NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)                      # 17:45 EDT: the session is complete
D01_WRITTEN = datetime(2026, 10, 14, 20, 40, tzinfo=timezone.utc).timestamp()  # RC-Daily-Bars, after close + 30 min

# ticker -> (market_cap, quote fields). The first group passes the quote-level hygiene, the second does not.
_QUOTE = {"regularMarketPrice": 50.0, "averageDailyVolume10Day": 2_000_000.0,
          "fiftyTwoWeekHigh": 52.0, "fiftyDayAverage": 45.0}
UNIVERSE = {
    "UP1": (1.0e9, dict(_QUOTE, earningsTimestampStart=1_790_000_000)),
    "UP2": (2.5e9, dict(_QUOTE)),
    "UP3": (6.0e8, dict(_QUOTE, regularMarketPrice=40.0, fiftyDayAverage=36.0, fiftyTwoWeekHigh=41.0)),
    "FLAT1": (1.5e9, dict(_QUOTE, regularMarketPrice=40.0, fiftyDayAverage=38.0, fiftyTwoWeekHigh=44.0)),
    "DOWN1": (9.0e8, dict(_QUOTE, regularMarketPrice=30.0, fiftyDayAverage=28.0, fiftyTwoWeekHigh=35.0)),
    "SHORT1": (7.0e8, dict(_QUOTE)),
    "NOBARS1": (7.5e8, dict(_QUOTE)),
    "NOEDGAR": (8.0e8, dict(_QUOTE)),
    "LOWPX": (4.0e8, dict(_QUOTE, regularMarketPrice=3.0, fiftyDayAverage=2.5, fiftyTwoWeekHigh=3.2)),
    "LOWVOL": (4.5e8, dict(_QUOTE, regularMarketPrice=20.0, averageDailyVolume10Day=1_000.0,
                            fiftyDayAverage=18.0, fiftyTwoWeekHigh=21.0)),
    "FAR": (5.0e8, dict(_QUOTE, regularMarketPrice=20.0, fiftyTwoWeekHigh=40.0, fiftyDayAverage=18.0)),
    "BELOW50": (5.5e8, dict(_QUOTE, regularMarketPrice=20.0, fiftyDayAverage=25.0, fiftyTwoWeekHigh=21.0)),
    "ETF1": (3.0e9, dict(_QUOTE)),
}
SPINE = {"UP1": ("Information Technology", "Software & Services", "Systems Software"),
         "UP2": ("Health Care", "Pharmaceuticals, Biotechnology & Life Sciences", "Biotechnology"),
         "UP3": ("Information Technology", "Software & Services", "Application Software"),
         "FLAT1": ("Industrials", "Capital Goods", "Industrial Machinery & Supplies & Components"),
         "DOWN1": ("Energy", "Energy", "Oil & Gas Exploration & Production")}
EXCLUDED = {"ETF1": "etp", "UP2": "common"}      # a typed operating name stays in; an ETP never screens
BARS = {"UP1": ("up", 0.0050), "UP2": ("up", 0.0030), "UP3": ("up", 0.0065), "FLAT1": ("flat", 0.0),
        "DOWN1": ("down", -0.0040), "NOEDGAR": ("up", 0.0040), "LOWPX": ("up", 0.0010),
        "LOWVOL": ("up", 0.0010), "FAR": ("up", 0.0010), "BELOW50": ("up", 0.0010),
        "SPY": ("up", 0.0015)}
LONG_BARS = 300
SHORT_BARS = 100                                   # fewer than the 252 a score needs


def _annual(growth: float, margin_scale: float = 1.0) -> list[dict]:
    return [{"revenue": 100.0 * (1 + growth) ** i, "gross_profit": 55.0 * margin_scale,
             "operating_cash_flow": 25.0 * margin_scale, "capex": 5.0, "total_debt": 10.0, "cash": 30.0,
             "ebit": 20.0 * margin_scale, "depreciation": 3.0, "total_assets": 200.0,
             "total_equity": 100.0, "net_income": 12.0 * margin_scale, "fiscal_year": 2022 + i}
            for i in range(3)]


EDGAR = {"UP1": _annual(0.45), "UP2": _annual(0.20, 0.8), "UP3": _annual(0.60, 1.2),
         "FLAT1": _annual(0.02, 0.5), "DOWN1": _annual(-0.05, 0.3), "SHORT1": _annual(0.3),
         "NOBARS1": _annual(0.3), "NOEDGAR": [], "LOWPX": _annual(0.3), "LOWVOL": _annual(0.3),
         "FAR": _annual(0.3), "BELOW50": _annual(0.3)}


def _bars(kind: str, drift: float, periods: int, end: date = SESSION) -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=periods)
    close = []
    for i in range(periods):
        wave = 1.0 + 0.015 * math.sin(i / 6.0)
        trend = 1.0 + drift * i if kind != "flat" else 1.0
        close.append(max(1.0, 40.0 * trend * wave))
    close = pd.Series(close, index=idx)
    return pd.DataFrame({"Open": close.shift(1).fillna(close.iloc[0]), "High": close * 1.01,
                         "Low": close * 0.99, "Close": close, "Volume": 1_500_000.0})


class _OfflineTicker:
    """The only yfinance object a scan may touch: it always fails, like a rate-limited provider."""

    def __init__(self, ticker):
        self.ticker = ticker

    def history(self, *a, **k):
        raise RuntimeError("Too Many Requests. Rate limited.")

    @property
    def info(self):
        raise RuntimeError("Too Many Requests. Rate limited.")


def seed_d01(ticker: str, df: pd.DataFrame, fetched_at: float = D01_WRITTEN) -> None:
    """``df``'s bars as D01 rows, written by RC-Daily-Bars after the last session's close + 30 min."""
    rawstore.write_rows("price_bars_daily", [{
        "ticker": ticker, "session_date": ts.date(), "open": float(r["Open"]), "high": float(r["High"]),
        "low": float(r["Low"]), "close": float(r["Close"]), "adj_close": None, "volume": float(r["Volume"]),
        "split_ratio": None, "adj_source": None, "source": "uw", "fetched_at": fetched_at, "run_id": "bars",
        "run_at": fetched_at} for ts, r in df.iterrows()])


def seed_quotes(session: date = SESSION) -> None:
    """One D04 Yahoo row per universe name: the scenario's screener quote, as the universe pull records it."""
    rawstore.write_rows("quote_snapshot", [{
        "ticker": t, "session_date": session, "source": "yfinance", "market_cap": cap,
        "price": q["regularMarketPrice"], "shares_outstanding": None, "week_52_high": q.get("fiftyTwoWeekHigh"),
        "week_52_low": None, "payload": q, "fetched_at": D01_WRITTEN, "run_id": "universe",
        "run_at": D01_WRITTEN} for t, (cap, q) in UNIVERSE.items()])


def seed() -> None:
    """Write the scenario's store. Call with an isolated, fully created rawstore."""
    rawstore.replace_all("equity_universe", [
        {"ticker": t, "market_cap": cap, "price": q["regularMarketPrice"], "name": f"{t} Inc",
         "sector": SPINE.get(t, ("Utilities",))[0],
         "industry_group": SPINE.get(t, (None, None, None))[1] if t in SPINE else None,
         "sub_industry": SPINE.get(t, (None, None, None))[2] if t in SPINE else None,
         "quote": q}
        for t, (cap, q) in UNIVERSE.items()])
    rawstore.replace_all("equity_universe_provenance", [
        {"key": "__meta__", "generated_at": GENERATED_AT, "refreshed_by": "Swing Potential"}])
    rawstore.ensure_table("security_master")
    rawstore.write_rows("security_master", [{"ticker": t, "security_type": ty} for t, ty in EXCLUDED.items()])
    for t, (kind, drift) in BARS.items():
        seed_d01(t, _bars(kind, drift, LONG_BARS))
    seed_d01("SHORT1", _bars("up", 0.004, SHORT_BARS))
    seed_quotes()
    rawstore.write_rows("swing_info", [
        {"ticker": t, "payload": {"longName": f"{t} Holdings", "industry": "Software - Infrastructure",
                                  "longBusinessSummary": "Cloud software and data platform.",
                                  "marketCap": cap},
         "cached_at": time.time()}
        for t, (cap, _q) in UNIVERSE.items() if t in ("UP1", "UP3", "FLAT1")])


def patch(monkeypatch) -> None:
    """No network, no EDGAR, no sleeping: every input comes from ``seed()``; the clock is ``NOW``."""
    monkeypatch.setattr(cal, "_now", lambda: NOW)
    monkeypatch.setattr("yfinance.Ticker", _OfflineTicker)

    def boom(*a, **k):
        raise AssertionError("yfinance was called for real")
    for name in ("screen", "download"):
        monkeypatch.setattr(f"yfinance.{name}", boom)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(fp.sec_edgar, "get_fundamentals",
                        lambda ticker, **k: (EDGAR.get(ticker, []), [], {}))


# ── normalisation ─────────────────────────────────────────────────────────────

# keys that exist only after #147 and are not part of what was captured before the refactor
_IGNORED_KEYS = ("universe_partial",)


def _norm(v):
    if isinstance(v, dict):
        return {k: ("<ts>" if (k == "generated_at" or k.endswith("_at")) and v[k] is not None else _norm(x))
                for k, x in sorted(v.items()) if k not in _IGNORED_KEYS}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, float):
        return None if v != v else round(v, 6)
    return v


TABLES = {"swing": ("swing_scores", "swing_gates"), "fundamentals": ("fundamentals_scores", "fundamentals_gates")}


def _table(name: str) -> list[dict]:
    rows = rawstore.query(f"SELECT * FROM {name}")
    return sorted((_norm(r) for r in rows), key=lambda r: [str(r.get(k)) for k in sorted(r)])


def snapshot() -> dict:
    """A Fetch on both boards, and the rows each Fetch persisted."""
    out: dict = {}
    for name, mod in (("swing", sp), ("fundamentals", fp)):
        fetch = mod.run_scan()
        out[name] = {"fetch": _norm(fetch),
                     "tables": {t: _table(t) for t in TABLES[name]}}
    return out
