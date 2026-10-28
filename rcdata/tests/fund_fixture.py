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


def run(name: str, as_of: str | None = None, *, sleep=None, clock=None, **params) -> dict:
    """One CLI request of dataset ``name`` (attended ones run too), daily unless ``params`` say otherwise; no real
    sleep unless ``sleep`` is given."""
    import time

    from RevenueChaser.rcdata.build import base
    parquet_root()
    p = dict(params)
    if as_of:
        p.setdefault("as_of", as_of)
    return base.run_request({"dataset": name, "requested_by": "cli", "params": p},
                            sleep=sleep or (lambda s: None), clock=clock or time.monotonic)


def q(sql: str, *params) -> list[tuple]:
    from RevenueChaser.rcdata import serving
    con = serving.open_read("fundamentals")
    try:
        return con.execute(sql, list(params)).fetchall()
    finally:
        con.close()


# ── The SEC: company facts and the daily index ────────────────────────────────────────────────────────────────

def _fact(val, end, accn, filed, form, fp, fy, start=None):
    f = {"val": val, "end": end, "accn": accn, "filed": filed, "form": form, "fp": fp, "fy": fy}
    if start:
        f["start"] = start
    return f


# MSFT (fiscal year to 30 June): FY2025 from the 10-K, Q1 FY2026 (to 30 Sep 2025) from the 10-Q, Q1 FY2027 (to 30
# Sep 2026) from the 10-Q filed 2026-10-27 when ``with_new_quarter``. Values in USD, chosen for the tests.
K25, Q26, Q27 = "0000950170-25-100001", "0000950170-25-130001", "0000950170-26-130001"


def msft_facts(with_new_quarter: bool = False) -> dict:
    def series(fy_val, q_val, q27_val=None, instant=False):
        fy_start, q_start, q27_start = (None, None, None) if instant else ("2024-07-01", "2025-07-01", "2026-07-01")
        out = [_fact(fy_val, "2025-06-30", K25, "2025-07-30", "10-K", "FY", 2025, fy_start),
               _fact(q_val, "2025-09-30", Q26, "2025-10-29", "10-Q", "Q1", 2026, q_start)]
        if with_new_quarter and q27_val is not None:
            out.append(_fact(q27_val, "2026-09-30", Q27, "2026-10-27", "10-Q", "Q1", 2027, q27_start))
        return out

    gaap = {
        "Revenues": {"units": {"USD": series(281_000, 77_000, 80_000)}},
        "CostOfRevenue": {"units": {"USD": series(87_000, 24_000, 25_000)}},
        "NetIncomeLoss": {"units": {"USD": series(101_000, 27_000, 28_000)}},
        "LongTermDebtNoncurrent": {"units": {"USD": series(40_000, 39_000, 38_000, instant=True)}},
        "LongTermDebtCurrent": {"units": {"USD": series(3_000, 2_500, 2_000, instant=True)}},
        "ShortTermBorrowings": {"units": {"USD": series(500, 400, 300, instant=True)}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": series(30_000, 28_000, 27_000, instant=True)}},
        "ShortTermInvestments": {"units": {"USD": series(64_000, 60_000, 59_000, instant=True)}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": series(13.64, 3.65, 3.80)}},
    }
    return {"cik": 789019, "entityName": "MICROSOFT CORPORATION", "facts": {"us-gaap": gaap}}


# HEI (fiscal year to 31 October): one 10-K a year ago and quarters long past, so its newest statement is old.
def hei_facts() -> dict:
    gaap = {"Revenues": {"units": {"USD": [
        _fact(3_900, "2024-10-31", "0000046619-24-000050", "2024-12-20", "10-K", "FY", 2024, "2023-11-01"),
        _fact(1_000, "2025-04-30", "0000046619-25-000020", "2025-05-28", "10-Q", "Q2", 2025, "2025-02-01")]}}}
    return {"cik": 46619, "entityName": "HEICO CORP", "facts": {"us-gaap": gaap}}


# TM files IFRS facts only (a 20-F filer): no us-gaap taxonomy.
def tm_facts() -> dict:
    return {"cik": 1094517, "entityName": "TOYOTA MOTOR CORP", "facts": {"ifrs-full": {"Revenue": {"units": {
        "JPY": [_fact(48e12, "2025-03-31", "0001193125-25-150000", "2025-06-25", "20-F", "FY", 2025, "2024-04-01")]}}}}}


def daily_index(rows: list[tuple[str, str, str, str, str]]) -> str:
    """A ``master.<YYYYMMDD>.idx`` body: ``rows`` of (cik, company, form, YYYYMMDD, accession)."""
    head = ("Description:           Daily Index of EDGAR Dissemination Feed by Company Name\n"
            "Last Data Received:    October 27, 2026\n\n"
            "CIK|Company Name|Form Type|Date Filed|Filename\n"
            "--------------------------------------------------------------------------------\n")
    return head + "".join(f"{c}|{n}|{f}|{d}|edgar/data/{c}/{a}.txt\n" for c, n, f, d, a in rows)


def sec_routes(http_mock, facts_by_cik: dict, indexes: dict | None = None) -> None:
    """Answer the companyfacts and daily-index URLs: ``facts_by_cik`` {cik: body or callable}, ``indexes``
    {YYYYMMDD: body}. Anything else at the SEC answers 404 (the mock's default)."""
    for day, body in (indexes or {}).items():
        http_mock.add(f"master.{day}.idx", body)
    for cik, body in facts_by_cik.items():
        http_mock.add(f"companyfacts/CIK{int(cik):010d}.json", body)
