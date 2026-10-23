"""Swing indicators — pure technical calculations over OHLCV history.

All functions are pure (no network, no I/O) so they are unit-testable with
synthetic data. ``compute_indicators`` produces the raw values consumed by the
screeners and the ranking engine; the screeners apply the thresholds.
"""
from __future__ import annotations

import logging

import pandas as pd

from RevenueChaser.core.info_fallback import has_fields, refetch_or_keep
from RevenueChaser.core.providers import yf_session  # at import: its 401 watcher sees the first .info read (#402)

logger = logging.getLogger(__name__)

_REQUIRED = ("Open", "High", "Low", "Close", "Volume")


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Align/normalize OHLCV: required columns, datetime index, sorted, deduped."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    missing = [c for c in _REQUIRED if c not in out.columns]
    if missing:
        return pd.DataFrame()
    if not isinstance(out.index, pd.DatetimeIndex):
        idx = pd.to_datetime(out.index, errors="coerce", utc=True)
        out.index = idx.tz_localize(None) if idx.tz is not None else idx
    elif out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    out = out[list(_REQUIRED)].apply(pd.to_numeric, errors="coerce")
    out = out.dropna(subset=["Close"])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


def _ret(close: pd.Series, n: int) -> float | None:
    if len(close) < n + 1 or n <= 0:
        return None
    base = _to_float(close.iloc[-1 - n])
    last = _to_float(close.iloc[-1])
    if not base or not last:
        return None
    return last / base - 1.0


def _sma(close: pd.Series, n: int) -> float | None:
    if len(close) < n:
        return None
    v = close.rolling(n).mean().iloc[-1]
    return _to_float(v)


def _ema(close: pd.Series, n: int) -> float | None:
    if len(close) < n:
        return None
    v = close.ewm(span=n, adjust=False).mean().iloc[-1]
    return _to_float(v)


def _atr(df: pd.DataFrame, n: int) -> float | None:
    if len(df) < n + 1:
        return None
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return _to_float(tr.rolling(n).mean().iloc[-1])


def _consistency(df: pd.DataFrame, lookback: int) -> float | None:
    if len(df) < lookback:
        return None
    sma50 = df["Close"].rolling(50).mean()
    recent = (df["Close"] > sma50).tail(lookback)
    return _to_float(recent.mean())


def _max_move_1_3m(close: pd.Series) -> float | None:
    if len(close) < 64:
        return None
    last = _to_float(close.iloc[-1])
    if not last:
        return None
    best = -1.0
    for i in range(21, 64):  # 1..3 months back
        base = _to_float(close.iloc[-1 - i])
        if base:
            best = max(best, last / base - 1.0)
    return best if best > -1.0 else None


def compute_indicators(df: pd.DataFrame, spy_df: pd.DataFrame | None = None,
                       returns_df: pd.DataFrame | None = None) -> dict:
    """Compute all raw indicator values for one ticker (plus optional SPY benchmark).

    ``df`` gives the levels (price, averages, the 52-week range, ADR, ATR, volume). ``returns_df``, when given, gives
    the returns (the 1-day, 1-week and 1-month changes, the 1-3 month move, 12-1 momentum and RS against SPY): D3
    (#383 R3 C12) reads D01's nominal bars for levels and its dividend-adjusted bars for returns. Without it ``df``
    gives both, as before.
    """
    ind: dict = {
        "price": None, "change_pct": None, "change_1w": None, "change_1m": None, "ret_3m": None,
        "ema10": None, "ema20": None, "ema20_prev": None,
        "ema10_prev": None, "sma50": None, "sma150": None, "sma200": None,
        "sma200_prev_1m": None, "sma150_prev_1m": None,
        "adr_pct": None, "low_52w": None, "high_52w": None,
        "above_52w_low_pct": None, "within_52w_high_pct": None,
        "vol_20d": None, "vol_ratio": None,
        "move_max_1_3m": None, "mom_12_1": None, "consistency_6m": None,
        "rs_vs_spy_12m": None, "rs_vs_spy_3m": None,
        "atr14": None,
    }
    df = normalize_ohlcv(df)
    if df.empty:
        return ind

    close = df["Close"]
    rclose = close
    if returns_df is not None:
        r = normalize_ohlcv(returns_df)
        rclose = r["Close"] if not r.empty else close.iloc[:0]
    price = _to_float(close.iloc[-1])
    ind["price"] = price
    rlast = _to_float(rclose.iloc[-1]) if len(rclose) else None
    rprev = _to_float(rclose.iloc[-2]) if len(rclose) >= 2 else None
    ind["change_pct"] = (rlast / rprev - 1.0) if (rlast and rprev) else None
    ind["change_1w"] = _ret(rclose, 5)
    ind["change_1m"] = _ret(rclose, 21)
    ind["ret_3m"] = _ret(rclose, 63)      # 3-month return on the RETURNS series (#384 SW-S1, D3)

    ind["ema10"] = _ema(close, 10)
    ind["ema20"] = _ema(close, 20)
    ind["ema10_prev"] = _ema(close.iloc[:-5], 10) if len(close) > 10 else None
    ind["ema20_prev"] = _ema(close.iloc[:-5], 20) if len(close) > 20 else None
    ind["sma50"] = _sma(close, 50)
    ind["sma150"] = _sma(close, 150)
    ind["sma200"] = _sma(close, 200)
    ind["sma200_prev_1m"] = _sma(close.iloc[:-21], 200) if len(close) > 220 else None
    ind["sma150_prev_1m"] = _sma(close.iloc[:-21], 150) if len(close) > 170 else None

    rng = (df["High"] - df["Low"]) / close
    ind["adr_pct"] = _to_float(rng.tail(20).mean())

    if len(df) >= 252:
        low = _to_float(df["Low"].tail(252).min())
        high = _to_float(df["High"].tail(252).max())
    else:
        low = _to_float(df["Low"].min())
        high = _to_float(df["High"].max())
    ind["low_52w"] = low
    ind["high_52w"] = high
    ind["above_52w_low_pct"] = (price / low) if (price and low) else None
    ind["within_52w_high_pct"] = (price / high) if (price and high) else None

    vol = df["Volume"]
    ind["vol_20d"] = _to_float(vol.tail(20).mean())
    last_vol = _to_float(vol.iloc[-1])
    ind["vol_ratio"] = (last_vol / ind["vol_20d"]) if (last_vol and ind["vol_20d"]) else None

    ind["move_max_1_3m"] = _max_move_1_3m(rclose)
    mom_12 = _ret(rclose, 252)
    mom_1 = _ret(rclose, 21)
    # Jegadeesh-Titman 12-1: the return from 12 months ago to 1 month ago
    # (skip the latest month), i.e. (1 + r12) / (1 + r1) - 1, not r12 - r1.
    ind["mom_12_1"] = ((1.0 + mom_12) / (1.0 + mom_1) - 1.0) if (mom_12 is not None and mom_1 is not None) else None
    ind["consistency_6m"] = _consistency(df, 126)

    if spy_df is not None:
        spy = normalize_ohlcv(spy_df)
        if not spy.empty:
            sc = spy["Close"]
            r12 = _ret(rclose, 252)
            s12 = _ret(sc, 252)
            r3 = _ret(rclose, 63)
            s3 = _ret(sc, 63)
            ind["rs_vs_spy_12m"] = (r12 - s12) if (r12 is not None and s12 is not None) else None
            ind["rs_vs_spy_3m"] = (r3 - s3) if (r3 is not None and s3 is not None) else None

    ind["atr14"] = _atr(df, 14)
    return ind


def load_ohlc(ticker: str, ttl_days: float = 1.0, retries: int = 3,
              no_fetch: bool = False) -> pd.DataFrame:
    """2y daily OHLCV (normalized), cache-first in ``.store/raw.duckdb`` (swing_ohlc).

    Cache-first so re-scans reuse the same OHLC instead of hammering yfinance
    (which rate-limits under load and caused whole scans to drop to ~0 results).
    On a cache miss/expiry it fetches with backoff. When the refetch fails and a
    (stale) cached series exists, that series is returned with
    ``df.attrs["stale"] = True`` — stale-but-present beats empty (#205); with no
    cache the empty frame carries ``attrs["fetch_failed"] = True``.

    Freshness uses ``swing_ohlc.cached_at`` (per ticker), seeded from the source
    CSV mtime at migration. A series is also stale, whatever its age, when its last
    bar is D or older (D = the latest complete session) and it was written before D's
    close + 30 min (#128): it cannot hold a complete D bar, and the forecast ledger
    freezes only complete-D boards. That covers bars that end before D and a D bar
    cached mid-session (a partial bar). ``no_fetch=True`` (model-only refresh) reads the cache
    regardless of age and never touches the network (and never evaluates that rule);
    returns empty when there is no cache.
    """
    import time

    from RevenueChaser.core import ledger, rawstore
    from RevenueChaser.module_swing_potential import config

    ticker = (ticker or "").strip().upper()
    if not ticker:
        return pd.DataFrame()

    rows = rawstore.query(
        "SELECT ts, \"open\", \"high\", \"low\", \"close\", \"volume\", cached_at "
        "FROM swing_ohlc WHERE ticker = ? ORDER BY ts", [ticker])
    if rows:
        if no_fetch:
            return _df_from_rows(rows)
        cached_at = rows[-1]["cached_at"] or 0
        if ((time.time() - cached_at) < ttl_days * 86400
                and not ledger.needs_session_refetch(rows[-1]["ts"], cached_at)):
            return _df_from_rows(rows)

    if no_fetch:
        return pd.DataFrame()

    import yfinance as yf
    df = pd.DataFrame()
    reset = False                     # one session reset and retry per call after Yahoo's 401 (U1, #402)
    attempt = 0
    while attempt < retries:
        started, error = yf_session.clock(), None
        try:
            raw = yf.Ticker(ticker).history(period="2y", interval="1d")
            df = normalize_ohlcv(raw)
            if not df.empty:
                break
        except Exception as exc:  # noqa: BLE001 - rate limit / transient
            error = exc
            logger.debug("ohlc %s attempt %d/%d: %s", ticker, attempt + 1, retries, exc)
            # Counted for the board banner (#80); the frame returned below is unchanged.
            from RevenueChaser.core.providers.yfinance import _HEALTH
            _HEALTH.call(error=str(exc) or type(exc).__name__, endpoint=f"{ticker}.history")
        if not reset and yf_session.recover(started, error):
            reset = True
            continue                  # the retry, at once; it is not one of ``retries``
        attempt += 1
        if error is not None and attempt < retries:
            time.sleep(2 * attempt)
    if not df.empty:
        try:
            outcome = _write_ohlc(ticker, df)
        except Exception:  # noqa: BLE001
            logger.warning("ohlc cache write failed for %s", ticker)
            outcome = "failed"
        if outcome == "refused" and rows:
            # #261 + F2b (``module_swing_potential/ohlc_cache.py``): a short refetch never replaces a longer stored
            # history, unless it is a new listing (no shared session, starting more than 30 days after the stored
            # last bar).
            kept = _df_from_rows(rows)
            kept.attrs["stale"] = True
            kept.attrs["short_refetch_refused"] = True
            return kept
        return df
    if rows:
        logger.warning("ohlc %s: refetch failed; serving the stale cache", ticker)
        stale = _df_from_rows(rows)
        stale.attrs["stale"] = True
        return stale
    df.attrs["fetch_failed"] = True
    return df


def _df_from_rows(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                            "close": "Close", "volume": "Volume"})
    # utc=True: legacy CSVs mix tz-aware and naive timestamps — normalize_ohlcv
    # strips the tz afterwards.
    df.index = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df.drop(columns=["ts"])
    return normalize_ohlcv(df)


def _write_ohlc(ticker: str, df: pd.DataFrame) -> str:
    """``df`` as ``ticker``'s cached series, through the one ``swing_ohlc`` writer (C8, #383 R3): "written" or
    "refused" (``module_swing_potential/ohlc_cache.write``)."""
    import time as _time

    from RevenueChaser.module_swing_potential import ohlc_cache
    return ohlc_cache.write(ohlc_cache.rows_for(ticker, df, _time.time())).get(ticker, "written")


def load_ticker_info(ticker: str, no_fetch: bool = False) -> dict:
    """yfinance ticker ``.info``, cached to ``.store/raw.duckdb`` (swing_info).

    ``no_fetch=True`` reads the cache regardless of age (model-only refresh);
    returns {} when there is no cache.
    """
    import time

    from RevenueChaser.core import rawstore
    from RevenueChaser.module_swing_potential import config

    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {}
    row = rawstore.get("swing_info", ticker)
    stale = None
    if row:
        info = row.get("payload")
        if isinstance(info, dict):
            cached_at = row.get("cached_at") or 0
            if no_fetch or (time.time() - cached_at) < config.OHLC_TTL_DAYS * 86400:
                return info
            stale = info
    if no_fetch:
        return {}
    import yfinance as yf
    # #259: a failed or empty refetch keeps the expired info, marked stale (never mutating the store).
    info, status = refetch_or_keep(stale, lambda: yf.Ticker(ticker).info or {}, has_fields, label=ticker)
    # A refetch that did not answer is a failed Yahoo read (#80). The value returned below is unchanged.
    from RevenueChaser.core.providers.yfinance import _HEALTH
    if status == "fresh":
        _HEALTH.call(endpoint=f"{ticker}.info")
    else:
        _HEALTH.call(error="refetch failed" if status == "stale" else "empty", endpoint=f"{ticker}.info")
    if status == "fresh":
        try:
            rawstore.write_rows("swing_info", [{
                "ticker": ticker, "payload": info, "cached_at": time.time(),
            }])
        except Exception:  # noqa: BLE001
            logger.warning("info cache write failed for %s", ticker)
        return info
    if status == "stale":
        return info
    return {}
