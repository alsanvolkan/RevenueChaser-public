"""E0e, D28 (#305, the Cboe part): the daily volatility indices, stored by trading date.

Cboe's public daily-price CSV per symbol (``https://cdn.cboe.com/api/global/us_indices/daily_prices/<SYMBOL>_History.csv``,
columns ``DATE,OPEN,HIGH,LOW,CLOSE`` or ``DATE,<SYMBOL>`` for close-only indices) — two requests per symbol (the Cboe
cdn answers 307 to cdn-api.cboe.com, which ``follow_redirects=True`` follows; 14 a night, inside the Cboe cap of 20) — plus MOVE, which
only Yahoo serves (``^MOVE`` daily bars through the repo's ``yf.download`` helper, one ``period="max"`` download sliced
locally; the existing yfinance budget, 1 call).

``session_date`` is the row's own trading date as given (a vol index trades the NYSE calendar; a date the holiday table
does not list is still stored as served, never shifted). Every symbol keeps its newest ``LIMIT`` rows each run; a symbol
with nothing stored yet keeps its whole file (``FIRST_RUN_LIMIT``), so the first night is the one deep pull and no
``--backfill-all`` flag is needed. A failed symbol is an error in the outcome that names it (every failure is in
``symbol_errors``, the first also in ``note``), never an exception; a body without the CSV header (a Cboe HTML error
page) is an error too. The first write of (symbol, session_date, source) wins.
"""
from __future__ import annotations

import csv
import io
import os
import time
from datetime import date, datetime

import httpx

from RevenueChaser.core import nightly_inputs, prices, rawstore
from RevenueChaser.module_earnings_radar.inputs._common import CALL_CAP, num, outcome

TABLE = "vol_index_daily"
CBOE_BASE = os.getenv("RC_CBOE_BASE") or "https://cdn.cboe.com/api/global/us_indices/daily_prices"
USER_AGENT = "RevenueChaser/1.0 (nightly inputs: Cboe vol indices)"
TIMEOUT = 30.0

SYMBOLS = ("VIX", "VIX9D", "VIX3M", "VIX6M", "VIX1Y", "VVIX", "OVX")
MOVE = "MOVE"                 # the table's symbol; Yahoo's ticker is ``MOVE_TICKER``
MOVE_TICKER = "^MOVE"
LIMIT = 60                    # rows kept per symbol per run once it has any
FIRST_RUN_LIMIT = None        # ... and on the first run of a symbol: None = the whole file
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d")      # Cboe serves MM/DD/YYYY; the ISO shape is the defensive second


def url_for(symbol: str) -> str:
    return f"{CBOE_BASE}/{symbol}_History.csv"


def parse_date(value) -> date | None:
    """A CSV ``DATE`` cell as a date: MM/DD/YYYY as Cboe serves it, or YYYY-MM-DD; None when it is neither."""
    raw = str(value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_csv(body: str) -> tuple[list[dict] | None, str | None]:
    """``(bars oldest first, why)`` from a daily-price CSV; a body without a usable ``DATE``/price header is an error
    (Cboe answers an HTML error page with a 200). Cboe serves either OHLC (``DATE,OPEN,HIGH,LOW,CLOSE``) or a
    close-only file (``DATE,<SYMBOL>``); the latter stores NULL ``open``/``high``/``low``. A row whose date will not
    parse, or whose close is blank or not a number, is skipped."""
    reader = csv.DictReader(io.StringIO(body or ""))
    keys = {(f or "").strip().upper(): f for f in (reader.fieldnames or [])}
    if "DATE" not in keys:
        return None, "not_a_csv"

    if "CLOSE" in keys:
        close_key = "CLOSE"
        has_ohlc = True
    else:
        price_cols = [k for k in keys if k not in ("DATE", "OPEN", "HIGH", "LOW", "VOLUME")]
        if len(price_cols) != 1:
            return None, "not_a_csv"
        close_key = price_cols[0]
        has_ohlc = False

    def cell(item: dict, name: str):
        return item.get(keys[name]) if name in keys else None

    bars = []
    for item in reader:
        d = parse_date(cell(item, "DATE"))
        if d is None:
            continue
        close = num(cell(item, close_key))
        if close is None:                       # a bar without a close is not a bar (first write of a key wins)
            continue
        bars.append({"session_date": d,
                     "open": num(cell(item, "OPEN")) if has_ohlc else None,
                     "high": num(cell(item, "HIGH")) if has_ohlc else None,
                     "low": num(cell(item, "LOW")) if has_ohlc else None,
                     "close": close})
    return bars, None


def fetch_csv(symbol: str) -> tuple[list[dict] | None, str | None]:
    """``(bars, why)`` for one symbol: one GET of its CSV, no retry (cdn.cboe.com answers 307 to cdn-api.cboe.com,
    so ``follow_redirects=True`` makes this two real HTTP requests per symbol)."""
    try:
        resp = httpx.get(url_for(symbol), timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    return parse_csv(resp.text)


def download_move(download=None) -> tuple[list[dict] | None, str | None]:
    """``(bars oldest first, why)`` for MOVE: one ``yf.download`` of ``^MOVE``, parsed by ``core.prices``."""
    dl = download or prices._yf_download
    try:
        frame = dl([MOVE_TICKER], period="max", **prices.YF_KW)
    except Exception as exc:  # noqa: BLE001 - one provider's failure is this sweep's error, not the run's
        return None, f"download failed ({type(exc).__name__})"
    bars, _ = prices.rows_from_yf(frame, [MOVE_TICKER], fetched_at=0.0, run_id="", run_at=0.0)
    rows = [{"session_date": b["session_date"], "open": b["open"], "high": b["high"], "low": b["low"],
             "close": b["close"]} for b in bars.get(MOVE_TICKER, [])]
    return (rows, None) if rows else (None, "no_bars")


def stored(symbol: str) -> bool:
    """Whether the table already holds a row of ``symbol`` (its first run keeps the whole file instead of ``LIMIT``)."""
    return bool(rawstore.query(f"SELECT 1 AS x FROM {TABLE} WHERE symbol = ? LIMIT 1", [symbol]))


def keep(symbol: str, bars: list[dict]) -> list[dict]:
    """The newest ``LIMIT`` bars, or all of them on the symbol's first run (``FIRST_RUN_LIMIT`` = None)."""
    limit = LIMIT if stored(symbol) else FIRST_RUN_LIMIT
    return bars if limit is None else bars[-limit:]


def rows_of(symbol: str, source: str, bars: list[dict], ctx, fetched: float) -> list[dict]:
    """One ``vol_index_daily`` row per kept bar, ``operator`` NULL (no index has one to attribute)."""
    return [{"symbol": symbol, "session_date": bar["session_date"], "source": source,
             "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
             "operator": None, **ctx.stamps(fetched)} for bar in keep(symbol, bars)]


def run(ctx, fetch=None, download=None) -> dict:
    fetch = fetch or fetch_csv
    asked, rows, errors = len(SYMBOLS) + 1, [], []       # 7 Cboe files and one MOVE download
    budget = ctx.budget("cboe")
    for symbol in SYMBOLS:
        if not budget.take():
            errors.append(CALL_CAP)
            continue
        bars, why = fetch(symbol)
        if bars is None:
            errors.append(f"{symbol}: {why or 'failed'}")
            continue
        if not bars:
            errors.append(f"{symbol}: no_rows")
            continue
        rows.extend(rows_of(symbol, "cboe", bars, ctx, time.time()))

    if ctx.budget("yfinance").take():
        bars, why = download_move(download)
        if bars is None:
            errors.append(f"{MOVE}: {why or 'failed'}")
        elif not bars:
            errors.append(f"{MOVE}: no_rows")
        else:
            rows.extend(rows_of(MOVE, "yfinance", bars, ctx, time.time()))
    else:
        errors.append(CALL_CAP)
    return outcome(asked, errors, symbol_errors=errors, **nightly_inputs.record_first(TABLE, rows))


SWEEP = nightly_inputs.Sweep("D28", (TABLE,), run)
