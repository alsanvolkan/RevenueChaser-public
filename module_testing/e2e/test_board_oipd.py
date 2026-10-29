"""Implied Probability (OIPD) board, end to end: options store -> Fetch -> Refresh (G9, build plan PR 19).

The chain is a fixture ``options.duckdb`` in the dashboard's market root, as ``uw/option_list`` serves it, for a name
put on the option watch list through the dashboard's own route. Its session is today and its expiries are relative to
today, because the board's expiry window is DTE-based (1d-365d) and a frozen fixture would age out of range. Neither
the Fetch nor the Refresh calls a provider.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from RevenueChaser.module_oipd import opd_math
from RevenueChaser.module_oipd.tests.options_store import daily_row, eod_rows, write_options

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
def stored(dashboard):
    """TST on the option watch list, with today's chain and close in ``options.duckdb``."""
    r = dashboard.client.post(f"/api/watchlist/option/toggle/{_TICKER}", headers={"X-RC-Panel": "oipd"})
    assert r.status_code == 200, r.text
    today = date.today()
    write_options(Path(dashboard.env["RC_MARKET_DIR"]),
                  option_eod=eod_rows(_TICKER, today, [c for e in _expiries() for c in _contracts_for(e)]),
                  option_daily=[daily_row(_TICKER, today, _SPOT)])
    return dashboard


@pytest.fixture
def fetched(stored, provider_routes):
    result = stored.run_job("/run/module_oipd", ticker=_TICKER)
    assert result.ok, f"OIPD fetch failed: {result}\n{stored.logs()}"
    return result


class TestFetch:
    def test_the_dashboard_serves_its_shell(self, dashboard):
        r = dashboard.get("/")
        assert r.status_code == 200
        assert "Implied Probability" in r.text

    def test_fetch_writes_the_board_artifact(self, dashboard, fetched):
        assert dashboard.artifact("oipd_latest.html").exists()

    def test_the_board_shows_the_fitted_ticker(self, dashboard, fetched):
        html = dashboard.view("module_oipd").text
        assert "Implied Probability" in html
        assert _TICKER in html
        assert "Risk-neutral" in html
        assert "stored chain · options store" in html

    def test_the_fetch_is_a_builder_request_and_calls_no_provider(self, dashboard, provider_routes, fetched):
        assert provider_routes.request_count == 0, [q.path for q in provider_routes.requests()]
        assert "Builder request: uw/option_list not queued" in dashboard.view("module_oipd").text

    def test_provenance_reports_a_data_date(self, dashboard, fetched):
        assert dashboard.data_as_of("module_oipd") is not None


class TestRefreshIsOfflineAndFaithful:
    def test_refresh_rebuilds_a_deleted_artifact(self, dashboard, fetched, clear_cache):
        clear_cache()
        assert not dashboard.artifact("oipd_latest.html").exists()
        r = dashboard.view("module_oipd", refresh=True, ticker=_TICKER)
        assert r.status_code == 200
        assert "Implied Probability" in r.text

    def test_refresh_makes_no_network_calls(self, dashboard, fetched, provider_routes, clear_cache):
        clear_cache()
        before = provider_routes.request_count
        r = dashboard.view("module_oipd", refresh=True, ticker=_TICKER)
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            "refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}")

    def test_refresh_survives_a_restart(self, dashboard, fetched, clear_cache):
        clear_cache()
        dashboard.restart()
        r = dashboard.view("module_oipd", refresh=True, ticker=_TICKER)
        assert r.status_code == 200
        assert _TICKER in r.text


class TestOffTheList:
    def test_a_name_off_the_list_shows_the_label_and_no_fit(self, stored):
        r = stored.view("module_oipd", refresh=True, ticker="ZZZ")
        assert r.status_code == 200
        assert "ZZZ is not on the option watch list" in r.text

    def test_a_list_name_with_no_stored_chain_says_when_it_fills(self, dashboard):
        r = dashboard.view("module_oipd", refresh=True, ticker="SPX")
        assert r.status_code == 200
        assert "No stored option chain for SPX yet" in r.text
