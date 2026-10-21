"""A fake ``yfinance``, installed on the library itself.

yfinance cannot be pointed at a local server: it hardcodes the Yahoo hosts,
manages its own session with cookie+crumb auth, and impersonates a browser TLS
fingerprint. So it is the one provider that must be faked at the Python
boundary.

Thirteen of the fourteen call sites in this repo go through ``yf.Ticker`` and
one through ``yf.screen``, so replacing those two names on the ``yfinance``
module covers everything — including ``module_spx_gamma/candles.py`` and
``vix.py``, which import ``yfinance`` at module top level and would defeat a
per-module patch.

Like the mock HTTP server, this counts calls, so "refresh performed no network"
stays provable for the yfinance-backed boards (swing, fundamentals, asymmetric
growth, financials).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "provider_fixtures" / "yfinance"

_CALLS: list[str] = []
_LOCK = threading.Lock()


def call_count() -> int:
    with _LOCK:
        return len(_CALLS)


def calls() -> list[str]:
    with _LOCK:
        return list(_CALLS)


def reset_calls() -> None:
    with _LOCK:
        _CALLS.clear()


def _record(what: str) -> None:
    with _LOCK:
        _CALLS.append(what)
        # Cross the process boundary: the harness reads this file to count
        # yfinance calls made inside the dashboard subprocess.
        log = os.environ.get("RC_E2E_YF_LOG")
        if log:
            try:
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write(what + "\n")
            except OSError:
                pass


def _load(name: str) -> dict:
    f = FIXTURES / name
    if not f.exists():
        return {}
    return json.loads(f.read_text(encoding="utf-8"))


class _FakeHistory:
    """Minimal stand-in for the pandas DataFrame ``.history()`` returns.

    Built lazily with pandas so the fake stays honest about column names and
    index type — the indicator code does real DataFrame work on this.
    """

    @staticmethod
    def build(rows: list[dict]):
        import pandas as pd
        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        idx = pd.to_datetime([r["date"] for r in rows])
        return pd.DataFrame(
            {
                "Open": [float(r.get("open", r["close"])) for r in rows],
                "High": [float(r.get("high", r["close"])) for r in rows],
                "Low": [float(r.get("low", r["close"])) for r in rows],
                "Close": [float(r["close"]) for r in rows],
                "Volume": [float(r.get("volume", 1_000_000)) for r in rows],
            },
            index=idx,
        )


class _FakeFastInfo:
    """Minimal ``fast_info`` stand-in — only what Phase B is allowed to read.

    ``market_cap`` must never be read: in real yfinance it falls back to
    ``Ticker.info`` (a network call), so reading it here raises instead of
    quietly succeeding and masking the fallback.
    """

    def __init__(self, data: dict | None):
        self._data = data or {}

    @property
    def shares(self):
        return self._data.get("shares")

    @property
    def last_price(self):
        return self._data.get("last_price")

    @property
    def market_cap(self):
        raise AssertionError(
            "fast_info.market_cap must not be read (falls back to Ticker.info)")


class FakeTicker:
    """Stands in for ``yfinance.Ticker``."""

    def __init__(self, ticker: str, *args, **kwargs):
        self.ticker = str(ticker).upper()
        self._data = _load("tickers.json").get(self.ticker, {})

    @property
    def info(self) -> dict:
        _record(f"info:{self.ticker}")
        return dict(self._data.get("info", {}))

    @property
    def fast_info(self):
        _record(f"fast_info:{self.ticker}")
        return _FakeFastInfo(self._data.get("fast_info"))

    def history(self, *args, **kwargs):
        _record(f"history:{self.ticker}")
        return _FakeHistory.build(self._data.get("ohlc", []))

    # Financial statements (core/providers/yfinance.py reads these by attribute)
    def _statement(self, key: str):
        import pandas as pd
        _record(f"{key}:{self.ticker}")
        payload = self._data.get(key)
        if not payload:
            return pd.DataFrame()
        return pd.DataFrame(payload["values"], index=payload["index"],
                            columns=pd.to_datetime(payload["columns"]))

    @property
    def income_stmt(self):
        return self._statement("income_stmt")

    @property
    def cashflow(self):
        return self._statement("cashflow")

    @property
    def balance_sheet(self):
        return self._statement("balance_sheet")

    @property
    def quarterly_income_stmt(self):
        return self._statement("income_stmt")

    @property
    def quarterly_cashflow(self):
        return self._statement("cashflow")

    @property
    def quarterly_balance_sheet(self):
        return self._statement("balance_sheet")

    @property
    def funds_data(self):
        _record(f"funds_data:{self.ticker}")
        return _FakeFundsData(self._data.get("top_holdings", []))


class _FakeFundsData:
    def __init__(self, holdings: list[dict]):
        self._holdings = holdings

    @property
    def top_holdings(self):
        import pandas as pd
        if not self._holdings:
            return pd.DataFrame()
        return pd.DataFrame(
            {"Name": [h["name"] for h in self._holdings],
             "Holding Percent": [float(h["weight"]) for h in self._holdings]},
            index=[h["symbol"] for h in self._holdings],
        )


def fake_download(*args, **kwargs):
    """Stands in for ``yfinance.download`` (bulk OHLC).

    The real function hardcodes Yahoo hosts, so a comovement Fetch that finds a
    stale/missing ticker must never fall through to the real internet in e2e.
    Records the request and returns an empty frame — no rows to write.
    """
    import pandas as pd
    tickers = args[0] if args else kwargs.get("tickers", [])
    if not isinstance(tickers, (list, tuple)):
        tickers = [tickers]
    _record("download:" + ",".join(str(t) for t in tickers))
    return pd.DataFrame()


def fake_screen(*args, **kwargs) -> dict:
    """Stands in for ``yfinance.screen`` (the universe source).

    Honours ``offset``/``size`` so the paginated scan in
    ``module_asymmetric_growth/engine.py`` terminates instead of looping.
    """
    _record("screen")
    quotes = _load("screener.json").get("quotes", [])
    offset = int(kwargs.get("offset", 0) or 0)
    size = int(kwargs.get("size", len(quotes)) or len(quotes))
    return {"quotes": quotes[offset:offset + size], "total": len(quotes)}


class _FakeEquityQuery:
    """``yfinance.screener.EquityQuery`` is only used to build a query object."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def install() -> None:
    """Replace ``yf.Ticker`` / ``yf.screen`` on the yfinance module itself.

    Must run BEFORE anything imports a module that does ``import yfinance as yf``
    at top level, or that module keeps a reference to the real class.
    """
    import yfinance

    yfinance.Ticker = FakeTicker
    yfinance.screen = fake_screen
    yfinance.download = fake_download
    try:
        import yfinance.screener as screener
        screener.EquityQuery = _FakeEquityQuery
    except Exception:  # pragma: no cover - layout differs across versions
        pass
