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


# ── SEC: the daily index, the submissions file and the cover facts of a filing's XBRL instance ─────────────────
# Shapes after EDGAR's documented files; Alphabet's three classes (A and C listed, B not) are the per-class case, the
# share counts are the tests'.

ALPHABET = [
    {"ticker": "GOOGL", "cik": "1652044", "name": "Alphabet Inc.", "exchange": "Nasdaq", "tier": "A",
     "security_type": "common", "gvkey": "160329", "iid": "01", "composite_figi": None},
    {"ticker": "GOOG", "cik": "1652044", "name": "Alphabet Inc.", "exchange": "Nasdaq", "tier": "A",
     "security_type": "common", "gvkey": "160329", "iid": "03", "composite_figi": None},
]
GOOG_ACCESSION = "0001652044-26-000071"


def master_index(lines) -> bytes:
    """``daily-index/<year>/QTR<n>/master.<YYYYMMDD>.idx``: the preamble, the header, the dashes, the rows."""
    head = ["Description:           Master Index of EDGAR Dissemination Feed",
            "Last Data Received:    October 26, 2026", "Comments:              webmaster@sec.gov",
            "Anonymous FTP:         ftp://ftp.sec.gov/edgar/", " ", "CIK|Company Name|Form Type|Date Filed|Filename",
            "-" * 80]
    return ("\n".join(head + ["|".join(r) for r in lines]) + "\n").encode("latin-1")


def submissions(cik: str, filings) -> bytes:
    """``data.sec.gov/submissions/CIK##########.json``: ``filings.recent`` as parallel arrays, newest first.
    ``filings``: ``(accession, form, filed, report_date, primary_document, inline)``."""
    keys = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument", "isInlineXBRL")
    recent = {k: [f[i] for f in filings] for i, k in enumerate(keys)}
    return json.dumps({"cik": cik, "name": "?", "filings": {"recent": recent, "files": []}}).encode()


def _context(cid: str, cik: str, period: str, members=()) -> str:
    seg = "".join(f'<xbrldi:explicitMember dimension="{d}">{m}</xbrldi:explicitMember>' for d, m in members)
    seg = f"<xbrli:segment>{seg}</xbrli:segment>" if seg else ""
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">'
            f"{int(cik):010d}</xbrli:identifier>{seg}</xbrli:entity><xbrli:period>{period}</xbrli:period>"
            "</xbrli:context>")


def instance(cik: str, doc_type: str, period_end: str, cover_day: str, classes, co_registrant=False) -> bytes:
    """A filing's XBRL instance with its cover facts only. ``classes``: ``(class member or None, trading symbol or
    None, shares)``; a class with a symbol gets the ``dei:TradingSymbol`` of the 12(b) table."""
    axis = "us-gaap:StatementClassOfStockAxis"
    dur = f"<xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>{period_end}</xbrli:endDate>"
    inst = f"<xbrli:instant>{cover_day}</xbrli:instant>"
    parts = [_context("c-1", cik, dur)]
    facts = [f'<dei:DocumentType contextRef="c-1">{doc_type}</dei:DocumentType>',
             f'<dei:DocumentPeriodEndDate contextRef="c-1">{period_end}</dei:DocumentPeriodEndDate>']
    for n, (member, symbol, shares) in enumerate(classes):
        members = [(axis, member)] if member else []
        parts += [_context(f"s-{n}", cik, inst, members), _context(f"d-{n}", cik, dur, members)]
        if symbol:
            facts.append(f'<dei:TradingSymbol contextRef="d-{n}">{symbol}</dei:TradingSymbol>')
        facts.append(f'<dei:EntityCommonStockSharesOutstanding contextRef="s-{n}" unitRef="shares" decimals="INF">'
                     f"{shares}</dei:EntityCommonStockSharesOutstanding>")
    if co_registrant:
        parts.append(_context("sub", cik, inst, [("dei:LegalEntityAxis", "goog:SubsidiaryMember")]))
        facts.append('<dei:EntityCommonStockSharesOutstanding contextRef="sub" unitRef="shares" decimals="INF">'
                     "100</dei:EntityCommonStockSharesOutstanding>")
    return ('<?xml version="1.0" encoding="utf-8"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            'xmlns:dei="http://xbrl.sec.gov/dei/2025" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
            'xmlns:us-gaap="http://fasb.org/us-gaap/2025" xmlns:goog="http://www.abc.xyz/20260630">'
            + "".join(parts) + "".join(facts) + "</xbrli:xbrl>").encode()


def goog_instance() -> bytes:
    return instance("1652044", "10-Q", "2026-06-30", "2026-07-17",
                    [("us-gaap:CommonClassAMember", "GOOGL", 5_800_000_000),
                     ("us-gaap:CommonClassBMember", None, 860_000_000),
                     ("goog:CapitalClassCMember", "GOOG", 5_450_000_000)], co_registrant=True)


def aapl_instance() -> bytes:
    return instance("320193", "10-K", "2026-09-26", "2026-10-17", [(None, "AAPL", 14_800_000_000)])
