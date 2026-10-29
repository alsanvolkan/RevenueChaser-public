"""Provider day caps: the builder refuses to start a dataset whose provider has reached its cap for the New York day.

The counter is ``ops.provider_calls_daily`` (every HTTP request the builder sends, retries included). Numbers are
avo's of 27 Oct 2026, to be tuned; a provider not listed has no cap.
"""
from __future__ import annotations

from . import ops

DAY_CAPS: dict[str, int] = {
    "uw": 150_000,
    "sec": 40_000,
    "fred": 10_000,
    "alpaca": 20_000,
    # The spine's providers (PR 5; working numbers, not avo's yet): WRDS queries, OpenFIGI requests (each up to 100
    # CUSIPs), and the fund-file hosts (one request per file and run).
    "wrds": 50,
    "openfigi": 1_000,
    "ssga": 50,
    "ishares": 50,
    "vanguard": 50,
    "nasdaq": 50,
    "invesco": 50,
    # yfinance, shared by two stores (working numbers, not avo's yet): the prices store's float lookups (PR 7, 3,000,
    # paced 1.5 s apart) and the fundamentals layer's weekly analyst estimates (PR 12, 2,000, one per ticker).
    "yfinance": 3_000 + 2_000,
    # The ownership store's providers (PR 16; working numbers, not avo's yet). The SEC's filings share "sec" above.
    # FINRA: up to 5 pages of 5,000 rows per settlement date; Nasdaq Trader and NYSE: the Reg SHO threshold lists,
    # one file per market and day; IBKR: its public short-stock file (one FTP download a run, no account).
    "finra": 2_000,
    "nasdaqtrader": 500,
    "nyse": 2_000,
    "ibkr": 50,
    # The fundamentals layer's (PR 12; working numbers): Alpha Vantage's free tier allows 25 requests a day. Its
    # yfinance estimates share "yfinance" above.
    "alphavantage": 25,
}


def cap_for(provider: str) -> int | None:
    return DAY_CAPS.get(provider)


def at_cap(provider: str) -> tuple[bool, int, int | None]:
    """``(refused, calls today, cap)``; ``refused`` is True once today's calls reached the cap."""
    cap = cap_for(provider)
    used = ops.calls_today(provider)
    return (cap is not None and used >= cap), used, cap
