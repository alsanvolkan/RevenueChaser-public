"""SPDR Select Sector holdings (SSGA daily xlsx): an independent check on the S&P 500 sectors.

SPDR is a *check*. It never sets a value, and its rows are never cached: the refresh compares each S&P 500
member's master sector with the sector of the SPDR fund that holds it and records a ``spdr`` conflict
on a mismatch (``core/identity/refresh.py``). The members come from ``index_constituents`` (slug ``sp500``).
The 11 Select Sector funds partition the S&P 500 by GICS sector; each fund is stored here under the Morningstar
sector name that UW uses for the same companies (#265), so the two sides use one vocabulary. A mismatch can be a
real disagreement between GICS and Morningstar, which is why the check only flags.

The URL per fund is ``<base>/holdings-daily-us-en-<fund>.xlsx``. Base URL: ``RC_SPDR_BASE`` (default the
SSGA library path). The workbook has a few preamble rows, so the header is on the 5th row (Name, Ticker,
Identifier, SEDOL, Weight, ...), followed by the holdings, cash and futures lines and a footer text row.

All or nothing: one fund that cannot be fetched or parsed (or parses to no holdings) makes the whole
result an error, because a partial set would read as a mass of unchecked tickers.
"""
from __future__ import annotations

import io
import re

import httpx
import pandas as pd

from ...providers import sec_edgar
from . import SourceResult, base_url, fail, norm_ticker, now_iso

SOURCE = "spdr"
# fund -> the Morningstar (UW) sector name of what it holds, in fetch order (a ticker held by two funds keeps the first)
FUNDS = {"xlb": "Basic Materials", "xlc": "Communication Services", "xle": "Energy", "xlf": "Financial Services",
         "xli": "Industrials", "xlk": "Technology", "xlp": "Consumer Defensive",
         "xlre": "Real Estate", "xlu": "Utilities", "xlv": "Healthcare", "xly": "Consumer Cyclical"}
DEFAULT_BASE = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us"
_TICKER = re.compile(r"^[A-Z][A-Z0-9.-]{0,6}$")
DROP = {"VYLR"}


def _cell(value) -> str:
    """A cell as stripped text; an empty cell (NaN) is ``""``, never the string ``"nan"``."""
    return "" if value is None or pd.isna(value) else str(value).strip()


def parse(content: bytes, fund: str) -> list[dict]:
    """Holdings of one fund workbook: ``ticker``, ``name``, ``fund``, ``sector``, ``weight`` (float or None).

    Only real equity lines are kept: the ticker must look like one, must not be a known leftover
    (``DROP``) and the name must not say FUTURE. The regex alone keeps a 5-character futures code like
    ``IXAZ6``, so the name rule is the belt to the regex's braces; cash (``CASH_USD``), blank and ``-``
    tickers and the footer row fail the regex. Raises ``ValueError`` when the Ticker or Name column is
    missing (a layout change), so the caller reports an error instead of an empty fund.
    """
    df = pd.read_excel(io.BytesIO(content), header=4)
    df.columns = [str(c).strip() for c in df.columns]
    if "Ticker" not in df.columns or "Name" not in df.columns:
        raise ValueError(f"no Ticker/Name columns (found {list(df.columns)[:8]})")
    rows = []
    for _, r in df.iterrows():
        raw = _cell(r["Ticker"]).upper()
        name = _cell(r["Name"])
        if not _TICKER.match(raw) or raw in DROP or "FUTURE" in name.upper():
            continue
        try:
            weight = float(r["Weight"]) if "Weight" in df.columns and not pd.isna(r["Weight"]) else None
        except (TypeError, ValueError):
            weight = None
        rows.append({"ticker": norm_ticker(raw), "name": name, "fund": fund.upper(),
                     "sector": FUNDS[fund.lower()], "weight": weight})
    return rows


def holdings() -> SourceResult:
    """Every Select Sector fund's holdings: ``ticker``, ``name``, ``fund``, ``sector``, ``weight``.

    All or nothing (see the module docstring). A ticker held by two funds keeps the first fund
    (XLB .. XLY order). Never raises.
    """
    base = base_url("SPDR", DEFAULT_BASE)
    rows: dict[str, dict] = {}
    try:
        for fund in FUNDS:
            r = httpx.get(f"{base}/holdings-daily-us-en-{fund}.xlsx", headers=sec_edgar._headers(),
                          timeout=60, follow_redirects=True)
            if r.status_code != 200:
                return fail(SOURCE, f"SPDR {fund}: HTTP {r.status_code}")
            try:
                parsed = parse(r.content, fund)
            except Exception as exc:  # noqa: BLE001 - not an xlsx, or a layout change
                return fail(SOURCE, f"SPDR {fund}: {type(exc).__name__}: {exc}")
            if not parsed:
                return fail(SOURCE, f"SPDR {fund}: no holdings rows")
            for row in parsed:
                rows.setdefault(row["ticker"], row)
        return SourceResult(SOURCE, "ok", list(rows.values()), None, now_iso())
    except Exception as exc:  # noqa: BLE001 - adapters never raise
        return fail(SOURCE, exc)
