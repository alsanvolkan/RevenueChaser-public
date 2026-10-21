"""Yahoo sector adapter: yfinance ``.info`` sector and industry for the tickers it is given.

Yahoo rate-limits hard, so this is deliberately slow and narrow: only the tickers passed in
(the job passes the ones UW has no sector for), one worker, 1.2 s + jitter between calls, a
10 / 30 / 90 s backoff on a rate limit, and a stop after 5 consecutive rate-limited tickers.
Stopping keeps the rows already gathered and returns ``error``. ``sleep``, ``rand`` and
``info_fn`` are injectable so tests neither sleep nor touch the network.

Ported from the Identity R1b research script (issue #84).
"""
from __future__ import annotations

import random
import time
from typing import Callable, Iterable

from . import SourceResult, now_iso, redact

SOURCE = "yahoo_sector"
BASE_PACE_S = 1.2
JITTER_S = 0.5
BACKOFF_S = (10, 30, 90)
STOP_AFTER = 5


def is_rate_limit(exc: BaseException) -> bool:
    """True for a Yahoo throttle (``YFRateLimitError``, HTTP 429, "Too Many Requests")."""
    text = f"{type(exc).__name__} {exc}"
    return "RateLimit" in type(exc).__name__ or "429" in text or "Too Many Requests" in text


def _yf_info(ticker: str) -> dict:
    import yfinance as yf          # lazy: importing it is slow and this source is optional

    return yf.Ticker(ticker).info or {}


def sectors(tickers: Iterable[str], info_fn: Callable[[str], dict] | None = None,
            sleep: Callable[[float], None] = time.sleep,
            rand: Callable[[float, float], float] = random.uniform,
            stop_after: int = STOP_AFTER) -> SourceResult:
    """One row per attempted ticker: ``sector``, ``industry``, ``quote_type``, ``status``.

    Row status: ``ok`` (sector found), ``no_data`` (Yahoo knows the ticker but has no sector),
    ``error`` (rate-limited out or failed; never cached). The result is ``error`` only when the
    run stopped early after ``stop_after`` consecutive rate-limited tickers.
    """
    info_fn = info_fn or _yf_info
    todo = [str(t).strip().upper() for t in tickers if str(t).strip()]
    rows: list[dict] = []
    consecutive = 0
    first = True
    for ticker in todo:
        row: dict | None = None
        for attempt in range(len(BACKOFF_S)):
            if not first:
                sleep(BASE_PACE_S + rand(0, JITTER_S))
            first = False
            try:
                info = info_fn(ticker) or {}
                row = {"ticker": ticker, "status": "ok" if info.get("sector") else "no_data",
                       "sector": info.get("sector"), "industry": info.get("industry"),
                       "quote_type": info.get("quoteType")}
                break
            except Exception as exc:  # noqa: BLE001
                if is_rate_limit(exc):
                    row = {"ticker": ticker, "status": "error", "error": "rate_limited"}
                    sleep(BACKOFF_S[attempt])
                    continue
                row = {"ticker": ticker, "status": "error",
                       "error": redact(f"{type(exc).__name__}: {exc}")[:160]}
                break
        rows.append(row)
        rate_limited = row["status"] == "error" and row.get("error") == "rate_limited"
        consecutive = consecutive + 1 if rate_limited else 0
        if consecutive >= stop_after:
            return SourceResult(SOURCE, "error", rows,
                                f"stopped after {stop_after} consecutive rate-limited tickers "
                                f"(at {ticker}; {len(todo) - len(rows)} not attempted)", now_iso())
    return SourceResult(SOURCE, "ok", rows, None, now_iso())
