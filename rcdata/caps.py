"""Provider limits, as the providers publish them (master plan Order 5, rule 3). Each number names its source.

``DAY_CAPS``: the quotas published per day. The builder refuses to start a dataset whose provider has reached its cap
for the New York day; the counter is ``ops.provider_calls_daily`` (every HTTP request the builder sends, retries
included).

``WINDOWS``: the published request windows, ``(requests, seconds)``, enforced by ``base.Pacer`` (``base.run_pacer``).
A provider with no published limit (FRED, WRDS, the fund-file hosts, Nasdaq Trader, the NYSE threshold file, IBKR's
short-stock file) is in neither table: no number is invented for it. A pacer some of them keep is politeness, not a
provider limit, and is labelled so where it is set.
"""
from __future__ import annotations

from . import ops

DAY_CAPS: dict[str, int] = {
    "uw": 100_000,  # UW API requests a day: owner-marked avo 2026-11-02
    "alphavantage": 25,  # free tier requests a day: https://www.alphavantage.co/support/#api-key
}

WINDOWS: dict[str, tuple[int, float]] = {
    # per User-Agent, both hosts
    "sec": (10, 1.0),  # https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
    "alpaca": (200, 60.0),  # Basic plan historical calls: https://docs.alpaca.markets/us/docs/about-market-data-api
    "openfigi": (250, 60.0),  # /v3/mapping with a key: https://www.openfigi.com/api/documentation
    "openfigi_search": (20, 60.0),  # /v3/search with a key (no caller yet): https://www.openfigi.com/api/documentation
    "finra": (1_200, 60.0),  # synchronous, per IP: https://developer.finra.org/docs
    "yfinance": (2_000, 3_600.0),  # per IP, Yahoo publishes none: owner-marked avo 2026-11-02
}


def cap_for(provider: str) -> int | None:
    return DAY_CAPS.get(provider)


def at_cap(provider: str) -> tuple[bool, int, int | None]:
    """``(refused, calls today, cap)``; ``refused`` is True once today's calls reached the cap."""
    cap = cap_for(provider)
    used = ops.calls_today(provider)
    return (cap is not None and used >= cap), used, cap
