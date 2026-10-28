"""The fundamentals layer's test sources: a small spine and each source's answers in its documented shape.

**Not recorded from the real sources** (no SEC, WRDS, Alpha Vantage, UW or Yahoo call is made from here). The shapes
follow each source's documentation (the SEC's XBRL ``companyfacts`` API and daily index, ``docs/openapi.yaml`` for
UW); the values are chosen for the tests. The SEC CIKs are the issuers' real ones as far as known here; the gvkeys
and GICS codes are illustrative.

| Name | Case |
|---|---|
| MSFT | a 10-K / 10-Q filer: long-term debt, its current portion and short-term borrowings all reported |
| ORCL | same sub-industry as MSFT (the peer ladder's first rung) |
| HEI, HEI-A | two share classes of one CIK (one company, two tickers) |
| TM | a 20-F filer (annual only) |
| WFC-PC | Tier B: never a fundamentals company |
"""
from __future__ import annotations

from datetime import date

SPINE_AS_OF = date(2026, 10, 27)

COMPANIES = [
    {"ticker": "MSFT", "cik": "789019", "name": "MICROSOFT CORP", "exchange": "Nasdaq", "tier": "A",
     "gvkey": "012141", "gsector": "45", "ggroup": "4510", "gind": "451030", "gsubind": "45103020",
     "sector": "Information Technology", "industry": "Software & Services"},
    {"ticker": "ORCL", "cik": "1341439", "name": "ORACLE CORP", "exchange": "NYSE", "tier": "A",
     "gvkey": "012142", "gsector": "45", "ggroup": "4510", "gind": "451030", "gsubind": "45103020",
     "sector": "Information Technology", "industry": "Software & Services"},
    {"ticker": "HEI", "cik": "46619", "name": "HEICO CORP", "exchange": "NYSE", "tier": "A", "gvkey": "005567",
     "gsector": "20", "ggroup": "2010", "gind": "201010", "gsubind": "20101010", "sector": "Industrials",
     "industry": "Capital Goods"},
    {"ticker": "HEI-A", "cik": "46619", "name": "HEICO CORP", "exchange": "NYSE", "tier": "A", "gvkey": "005567",
     "gsector": "20", "ggroup": "2010", "gind": "201010", "gsubind": "20101010", "sector": "Industrials",
     "industry": "Capital Goods"},
    {"ticker": "TM", "cik": "1094517", "name": "TOYOTA MOTOR CORP/", "exchange": "NYSE", "tier": "A",
     "gvkey": "010581", "gsector": "25", "ggroup": "2510", "gind": "251020", "gsubind": "25102010",
     "sector": "Consumer Discretionary", "industry": "Automobiles & Components"},
    {"ticker": "WFC-PC", "cik": "72971", "name": "WELLS FARGO & COMPANY/MN", "exchange": "NYSE", "tier": "B"},
]
TIER_A = sorted(c["ticker"] for c in COMPANIES if c["tier"] == "A")


def spine(rows: list[dict] | None = None, as_of: date = SPINE_AS_OF) -> None:
    """Build and swap in a ``spine.duckdb`` whose ``security`` table holds ``rows`` (the spine's own schema)."""
    from RevenueChaser.rcdata import serving
    from RevenueChaser.rcdata.build.spine import security
    full = [{**{c: None for c, _ in security.SECURITY}, "source": "sec/company_tickers_exchange",
             "source_as_of": as_of, "fetched_at": 1.0, "run_id": "spine-run", **r}
            for r in (COMPANIES if rows is None else rows)]
    with serving.build("spine") as b:
        n = security._create(b.con, "security", full)
        b.swap({"security": n})


def parquet_root() -> None:
    """The builder refuses a missing Parquet root (the raw disk not mounted): the tests make it."""
    from RevenueChaser.rcdata.build import base
    base.parquet_root().mkdir(parents=True, exist_ok=True)


def run(name: str, as_of: str | None = None, **params) -> dict:
    """One CLI request of dataset ``name`` (attended ones run too), daily unless ``params`` say otherwise."""
    from RevenueChaser.rcdata.build import base
    parquet_root()
    p = dict(params)
    if as_of:
        p.setdefault("as_of", as_of)
    return base.run_request({"dataset": name, "requested_by": "cli", "params": p}, sleep=lambda s: None)


def q(sql: str, *params) -> list[tuple]:
    from RevenueChaser.rcdata import serving
    con = serving.open_read("fundamentals")
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()
