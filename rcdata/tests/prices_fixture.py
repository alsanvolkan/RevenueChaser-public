"""The prices store's test sources: a small live spine and Alpaca's bars answer, in the documented shapes.

**Not recorded from the real sources** (a cloud thread has no Alpaca key and makes no WRDS call). The answer shape
follows Alpaca's market-data reference (read 2026-10-28); the prices are chosen for the tests. The CIKs are the
issuers' real ones as far as known here; the gvkeys and iids are illustrative.

| Name | Case |
|---|---|
| AAPL | the plain case: Tier A, linked to Compustat, a quarterly dividend |
| BRK-B | a share class: Alpaca writes ``BRK.B`` |
| FLG | Flagstar's 1-for-3 reverse split of 2025-07-01: a price gap that an action explains |
| GAPX | a price gap with no action (the ``price_gap_no_action`` issue) |
| SPY | a benchmark ETF, Tier B (asked as a benchmark, not as Tier A) |
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import duckdb

SPINE = [
    {"ticker": "AAPL", "cik": "320193", "name": "Apple Inc.", "exchange": "Nasdaq", "tier": "A",
     "security_type": "common", "gvkey": "001690", "iid": "01", "composite_figi": "BBG000B9XRY4"},
    {"ticker": "BRK-B", "cik": "1067983", "name": "BERKSHIRE HATHAWAY INC", "exchange": "NYSE", "tier": "A",
     "security_type": "common", "gvkey": "072726", "iid": "02", "composite_figi": None},
    {"ticker": "FLG", "cik": "910073", "name": "Flagstar Financial, Inc.", "exchange": "NYSE", "tier": "A",
     "security_type": "common", "gvkey": None, "iid": None, "composite_figi": "BBG000BWBX65"},
    {"ticker": "GAPX", "cik": "4000001", "name": "GAP EXAMPLE INC", "exchange": "Nasdaq", "tier": "A",
     "security_type": "common", "gvkey": None, "iid": None, "composite_figi": None},
    {"ticker": "SPY", "cik": "884394", "name": "SPDR S&P 500 ETF TRUST", "exchange": "NYSE", "tier": "B",
     "security_type": "etf", "gvkey": "001000", "iid": "01", "composite_figi": "BBG000BDTBL9"},
]


def write_spine(rows=SPINE) -> None:
    """A live ``spine.duckdb`` holding ``rows`` in its ``security`` table (the other columns NULL)."""
    from RevenueChaser.rcdata import serving
    from RevenueChaser.rcdata.build.spine.security import SECURITY
    path = serving.live_path("spine")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                    "build_id TEXT)")
        con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('spine', 1, 0, 'test')")
        cols = ", ".join(f'"{c}" {t}' for c, t in SECURITY)
        con.execute(f"CREATE TABLE security ({cols})")
        names = [c for c, _ in SECURITY]
        for r in rows:
            con.execute(f"INSERT INTO security ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                        [r.get(c) for c in names])
    finally:
        con.close()


def bar_time(session: date) -> str:
    """Alpaca's daily bar timestamp: midnight New York, in UTC."""
    from RevenueChaser.core import calendar
    return datetime(session.year, session.month, session.day, tzinfo=calendar.NY).astimezone(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bar(session: date, close: float, volume: float = 1_000_000, **extra) -> dict:
    return {"t": bar_time(session), "o": extra.get("o", close), "h": extra.get("h", close * 1.01),
            "l": extra.get("l", close * 0.99), "c": close, "v": volume, "n": extra.get("n", 1000),
            "vw": extra.get("vw", close)}


class BarsAnswer:
    """``/v2/stocks/bars``: ``series[adjustment][symbol] = [bar, ...]``; each answer holds the asked symbols' bars
    inside ``start``/``end`` (by New York session), or every bar when ``unfiltered``. ``refuse`` symbols make the
    whole request a 422, as Alpaca answers a symbol it does not take. ``page_size`` splits the answer into pages."""

    def __init__(self, series: dict, refuse=(), unfiltered=False, page_size: int | None = None, fail_after=None):
        self.series, self.refuse, self.unfiltered = series, set(refuse), unfiltered
        self.page_size, self.fail_after, self.served = page_size, fail_after, 0

    def __call__(self, request):
        from RevenueChaser.core import calendar
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        symbols = q["symbols"].split(",")
        if self.fail_after is not None and self.served >= self.fail_after:
            return 500, b"down", {}
        if self.refuse & set(symbols):
            return 422, json.dumps({"message": "invalid symbol"}).encode(), {}
        lo = datetime.fromisoformat(q["start"].replace("Z", "+00:00")).astimezone(calendar.NY).date()
        hi = datetime.fromisoformat(q["end"].replace("Z", "+00:00")).astimezone(calendar.NY).date()
        bars = {}
        for s in symbols:
            got = [b for b in self.series.get(q["adjustment"], {}).get(s, [])
                   if self.unfiltered or lo <= datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                   .astimezone(calendar.NY).date() <= hi]
            if got:
                bars[s] = got
        flat = [(s, b) for s, bs in bars.items() for b in bs]
        size = self.page_size or max(1, len(flat))
        page = int(q.get("page_token") or 0)
        chunk = flat[page * size:(page + 1) * size]
        body = {"bars": {}, "currency": "USD",
                "next_page_token": str(page + 1) if (page + 1) * size < len(flat) else None}
        for s, b in chunk:
            body["bars"].setdefault(s, []).append(b)
        self.served += 1
        return 200, json.dumps(body).encode(), {"content-type": "application/json"}
