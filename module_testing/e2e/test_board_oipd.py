"""Implied Probability (OIPD) board, end to end: fetch -> store -> refresh.

The chain fixture is generated relative to ``date.today()`` rather than stored
as static JSON, because the board's expiry window is DTE-based (1d-365d) and a
frozen fixture would age out of range.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from RevenueChaser.module_oipd import opd_math

_SPOT = 100.0
_R = 0.04
_TICKER = "TST"
_TRUE = dict(a=0.02, b=0.12, rho=-0.5, m=0.0, sigma=0.15)


def _expiries():
    today = date.today()
    return [(today + timedelta(days=n)).isoformat() for n in (30, 60, 120)]


def _occ(expiry, cp, strike):
    return f"{_TICKER}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{cp}{int(round(strike * 1000)):08d}"


def _contracts_for(expiry):
    d = (date.fromisoformat(expiry) - date.today()).days
    T = d / 365.0
    F = _SPOT * math.exp(_R * T)
    out = []
    for K in np.arange(round(_SPOT * 0.7), _SPOT * 1.31, _SPOT * 0.02):
        k = math.log(K / F)
        iv = math.sqrt(float(opd_math.svi_w(k, **_TRUE)) / T)
        for cp in ("C", "P"):
            price = opd_math.black76_price(F, K, T, _R, iv, cp)
            out.append({
                "option_symbol": _occ(expiry, cp, K),
                "nbbo_bid": round(price * 0.99, 4), "nbbo_ask": round(price * 1.01, 4),
                "last_price": round(price, 4), "implied_volatility": round(iv, 5),
                "open_interest": 100, "volume": 10, "delta": 0.5,
                "last_tape_time": "2026-09-23T20:00:00Z"})
    return out


@pytest.fixture
def oipd_routes(provider_routes):
    """Canned UW chain for TST, with expiries relative to today."""
    exps = _expiries()
    contracts = [c for e in exps for c in _contracts_for(e)]
    provider_routes.route(
        f"/api/stock/{_TICKER}/expiry-breakdown",
        payload=lambda p: {"data": [{"expiry": e, "open_interest": 10000,
                                     "volume": 500, "chains": 50} for e in exps]})
    provider_routes.route(
        f"/api/stock/{_TICKER}/option-contracts",
        payload=lambda p: {"data": contracts})
    # Real UW candle rows carry a date and a session tag; since MB4 a row with
    # no date is not a bar, so a bare {"close": ...} would mean "no spot".
    provider_routes.route(f"/api/stock/{_TICKER}/ohlc/1d",
                          payload={"data": [{"date": "2026-09-29", "market_time": "r",
                                             "open": _SPOT, "high": _SPOT, "low": _SPOT,
                                             "close": _SPOT, "volume": 1000}]})
    return provider_routes


@pytest.fixture
def fetched(dashboard, oipd_routes):
    result = dashboard.run_job("/run/module_oipd", ticker=_TICKER)
    assert result.ok, f"OIPD fetch failed: {result}\n{dashboard.logs()}"
    return result


class TestFetch:
    def test_the_dashboard_serves_its_shell(self, dashboard):
        r = dashboard.get("/")
        assert r.status_code == 200
        assert "Implied Probability" in r.text

    def test_fetch_persists_the_chain(self, dashboard, fetched):
        assert dashboard.table_count("oipd_chain") > 0

    def test_fetch_writes_the_board_artifact(self, dashboard, fetched):
        assert dashboard.artifact("oipd_latest.html").exists()

    def test_the_board_shows_the_fitted_ticker(self, dashboard, fetched):
        html = dashboard.view("module_oipd").text
        assert "Implied Probability" in html
        assert _TICKER in html
        assert "Risk-neutral" in html

    def test_provenance_reports_a_data_date(self, dashboard, fetched):
        assert dashboard.data_as_of("module_oipd") is not None

    def test_the_fetch_called_the_chain_endpoint(self, dashboard, oipd_routes, fetched):
        assert oipd_routes.requests(f"/api/stock/{_TICKER}/option-contracts"), \
            "fetch must hit the option-contracts endpoint"


class TestRefreshIsOfflineAndFaithful:
    def test_refresh_rebuilds_a_deleted_artifact(self, dashboard, fetched, clear_cache):
        clear_cache()
        assert not dashboard.artifact("oipd_latest.html").exists()
        r = dashboard.view("module_oipd", refresh=True, ticker=_TICKER)
        assert r.status_code == 200
        assert "Implied Probability" in r.text

    def test_refresh_makes_no_network_calls(self, dashboard, fetched, oipd_routes,
                                            clear_cache):
        clear_cache()
        before = oipd_routes.request_count
        r = dashboard.view("module_oipd", refresh=True, ticker=_TICKER)
        assert r.status_code == 200
        assert oipd_routes.request_count == before, (
            "refresh reached the network: "
            f"{[q.path for q in oipd_routes.requests()[before:]]}")

    def test_refresh_survives_a_restart(self, dashboard, fetched, clear_cache):
        clear_cache()
        dashboard.restart()
        r = dashboard.view("module_oipd", refresh=True, ticker=_TICKER)
        assert r.status_code == 200
        assert _TICKER in r.text


class TestEmptyStore:
    def test_a_board_with_no_data_renders_an_empty_state(self, dashboard):
        r = dashboard.view("module_oipd", refresh=True, ticker="ZZZ")
        assert r.status_code == 200
        assert "No cached chain" in r.text
