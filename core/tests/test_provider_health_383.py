"""Common K3 (#383 R1): provider health is provider-agnostic, and Yahoo counts a failed statement read.

Shape ``{provider, calls, failed, auth_error, failures[], breaker_open, skipped_calls}``; the last two are what
A3's ``history.uw_problems`` reads beside ``calls``/``failed``/``auth_error``, so every provider carries them. The
banner names the provider; the Unusual Whales text stays byte-identical (a health without ``provider`` is UW's).
"""
from __future__ import annotations

import pandas as pd
import pytest

from RevenueChaser.core.providers import yfinance as yfp
from RevenueChaser.core.providers.result import ProviderHealth
from RevenueChaser.core.reporting_common import provider_health_html

_STYLE = ('<div class="rc-provider-health" role="alert" style="margin:8px 0 12px;padding:10px 12px;'
          'border:1px solid var(--amber,#f1c40f);border-radius:8px;background:rgba(241,196,15,.08);'
          'color:var(--amber,#f1c40f);font-size:13px;line-height:1.45"><b>Data incomplete.</b> ')
_TAIL = " This board may be missing rows — run Fetch again later.</div>"


# ── the UW banner is unchanged ───────────────────────────────────────────────────────────────────────────────────
def test_the_uw_banner_is_byte_identical():
    h = {"calls": 12, "failed": 3, "failures": [{"endpoint": "/api/x", "status": 500}],
         "auth_error": {"status": 401}, "breaker_open": False, "skipped_calls": 0}
    want = (_STYLE + "Unusual Whales rejected the API key (HTTP 401) — check UNUSUAL_WHALES_API_KEY in .env. "
            "3 of 12 Unusual Whales calls failed (e.g. /api/x: 500)." + _TAIL)
    assert provider_health_html(h) == want
    assert provider_health_html({**h, "provider": "unusual_whales"}) == want
    h2 = {"calls": 5, "failed": 2, "failures": [{"endpoint": "/api/<y>", "error": "timeout & co"}], "auth_error": None}
    assert provider_health_html(h2) == (_STYLE + "2 of 5 Unusual Whales calls failed "
                                        "(e.g. /api/&lt;y&gt;: timeout &amp; co)." + _TAIL)
    assert provider_health_html({"calls": 4, "failed": 0, "auth_error": None}) == ""


# ── Yahoo ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_yahoo_health_names_yahoo():
    h = {"provider": "yfinance", "calls": 10, "failed": 3, "auth_error": None,
         "failures": [{"endpoint": "AAPL.income_stmt", "error": "HTTP 429"}], "breaker_open": False,
         "skipped_calls": 0}
    out = provider_health_html(h)
    assert "3 of 10 Yahoo Finance calls failed (e.g. AAPL.income_stmt: HTTP 429)." in out
    assert "Unusual Whales" not in out and "UNUSUAL_WHALES_API_KEY" not in out


def test_an_unknown_provider_is_named_as_given_and_escaped():
    out = provider_health_html({"provider": "<acme>", "calls": 1, "failed": 1, "auth_error": {"status": 403}})
    assert "&lt;acme&gt; rejected the request (HTTP 403)." in out and "1 of 1 &lt;acme&gt; calls failed." in out


def test_provider_health_has_the_shared_shape():
    ph = ProviderHealth("yfinance")
    ph.call()
    ph.call(error="boom", endpoint="AAPL.cashflow")
    h = ph.health()
    assert h == {"provider": "yfinance", "calls": 2, "failed": 1, "auth_error": None,
                 "failures": [{"endpoint": "AAPL.cashflow", "error": "boom"}],
                 "breaker_open": False, "skipped_calls": 0}
    for key in ("calls", "failed", "auth_error", "breaker_open", "skipped_calls"):   # A3 uw_problems reads these
        assert key in h
    ph.reset()
    assert ph.health()["calls"] == 0 and ph.health()["failures"] == []


def test_provider_health_keeps_the_first_20_failures():
    ph = ProviderHealth("yfinance")
    for i in range(25):
        ph.call(error=f"e{i}", endpoint=f"T{i}.info")
    h = ph.health()
    assert h["failed"] == 25 and len(h["failures"]) == 20 and h["failures"][0]["error"] == "e0"


class _RaisingTicker:
    """Every statement read raises; ``info`` answers."""
    info = {"longName": "Raise Co"}

    def __getattr__(self, name):
        raise RuntimeError(f"{name} unavailable")


def test_a_raised_statement_read_is_counted(monkeypatch):
    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", lambda t: _RaisingTicker())
    yfp.reset_health()
    out = yfp.get_financials("RAISE")
    assert out["annual"] == []
    h = yfp.health()
    assert h["provider"] == "yfinance"
    # income_stmt, financials, cashflow, cash_flow, balance_sheet, balance_sheets: each raised
    assert h["failed"] == 6 and h["calls"] == 7               # + the .info read that answered
    assert h["failures"][0]["endpoint"] == "RAISE.income_stmt"
    assert "income_stmt unavailable" in h["failures"][0]["error"]


def test_a_raised_info_read_is_counted(monkeypatch):
    import yfinance

    class _NoInfo:
        @property
        def info(self):
            raise RuntimeError("network down")

    monkeypatch.setattr(yfinance, "Ticker", lambda t: _NoInfo())
    yfp.reset_health()
    yfp.get_info("NOINFO")
    assert yfp.health()["failed"] == 1
    assert yfp.health()["failures"] == [{"endpoint": "NOINFO.info", "error": "network down"}]


def test_a_successful_statement_read_is_counted(monkeypatch):
    import pandas as pd
    import yfinance

    class _EmptyTicker:
        """Every statement read answers with an empty frame, so each of the six labels is tried."""
        info = {"longName": "Empty Co"}

        def __getattr__(self, name):
            return pd.DataFrame()

    monkeypatch.setattr(yfinance, "Ticker", lambda t: _EmptyTicker())
    yfp.reset_health()
    yfp.get_financials("EMPTY")
    assert yfp.health()["calls"] == 7 and yfp.health()["failed"] == 0   # six statement reads + .info
