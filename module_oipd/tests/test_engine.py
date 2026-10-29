"""Implied Probability engine: the options store's chain -> fit, on a Fetch and a store-only Refresh alike (G9).

``stored`` puts TST on the option watch list and writes its chain into a fixture ``options.duckdb``.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from RevenueChaser.module_oipd import engine, opd_math, report
from RevenueChaser.module_oipd.tests.options_store import daily_row, eod_rows, write_options
from RevenueChaser.rcdata import ops

_ASOF = "2026-09-23"
_SPOT = 100.0
_R = 0.04
_TRUE = dict(a=0.02, b=0.12, rho=-0.5, m=0.0, sigma=0.15)


def _expiries():
    base = date.fromisoformat(_ASOF)
    return [(base + timedelta(days=n)).isoformat() for n in (30, 60, 120, 240)]


def _occ(root, expiry, cp, strike):
    return f"{root}{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{cp}{int(round(strike * 1000)):08d}"


def _chain_for(expiry):
    d = (date.fromisoformat(expiry) - date.fromisoformat(_ASOF)).days
    T = d / 365.0
    F = _SPOT * math.exp(_R * T)
    out = []
    for K in np.arange(round(_SPOT * 0.7), _SPOT * 1.31, _SPOT * 0.02):
        k = math.log(K / F)
        iv = math.sqrt(float(opd_math.svi_w(k, **_TRUE)) / T)
        for cp in ("C", "P"):
            price = opd_math.black76_price(F, K, T, _R, iv, cp)
            out.append({
                "option_symbol": _occ("TST", expiry, cp, K),
                "nbbo_bid": round(price * 0.99, 4), "nbbo_ask": round(price * 1.01, 4),
                "last_price": round(price, 4), "implied_volatility": round(iv, 5),
                "open_interest": 100, "volume": 10, "delta": 0.5,
                "last_tape_time": "2026-09-23T20:00:00Z"})
    return out


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr(engine, "_today", lambda: _ASOF)


def store_chain(ticker="TST", close=_SPOT):
    """``ticker`` on the option watch list, with its chain of ``_ASOF`` in the options store."""
    ops.watch_add("option", ticker, by="test")
    session = date.fromisoformat(_ASOF)
    write_options(option_eod=eod_rows(ticker, session, [c for e in _expiries() for c in _chain_for(e)]),
                  option_daily=[daily_row(ticker, session, close)])


@pytest.fixture
def stored(fixed_today):
    store_chain()


class TestFit:
    def test_fits_every_stored_expiry(self, stored):
        payload = engine.run(None, "TST")
        assert payload["error"] is None
        assert payload["spot"] == _SPOT
        assert len(payload["expiries"]) == 4
        assert payload["fan"] is not None

    def test_each_expiry_is_arbitrage_clean(self, stored):
        payload = engine.run(None, "TST")
        for e in payload["expiries"]:
            assert e["g_min"] >= 0.0
            assert abs(e["mean"] - e["forward"]) / e["forward"] < 0.02

    def test_probabilities_bracket_the_spot(self, stored):
        payload = engine.run(None, "TST")
        for e in payload["expiries"]:
            assert 0.0 <= e["prob_below_spot"] <= 1.0
            assert abs(e["prob_below_spot"] + e["prob_above_spot"] - 1.0) < 1e-3

    def test_missing_spot_still_fits_from_the_chain(self, fixed_today):
        # No stored close for the session: the forward comes from the put/call pairs, and only the spot-relative
        # probabilities are unknown.
        store_chain(close=None)
        payload = engine.run(None, "TST")
        assert payload["spot"] is None
        assert len(payload["expiries"]) == 4
        assert any("No spot price" in w for w in payload["warnings"])
        for e in payload["expiries"]:
            assert e["forward"] > 0
            for k in ("prob_below_spot", "prob_above_spot", "p_up10", "p_down10"):
                assert e[k] is None


class TestRefresh:
    def test_refresh_reproduces_the_fetch_offline(self, stored):
        fetched = engine.run(None, "TST", capture={})
        refreshed = engine.run(ticker="TST", no_fetch=True)
        assert refreshed["n_contracts"] == fetched["n_contracts"]
        assert ([e["quantiles"]["median"] for e in refreshed["expiries"]]
                == [e["quantiles"]["median"] for e in fetched["expiries"]])

    def test_empty_store_returns_an_explicit_error(self):
        ops.watch_add("option", "ZZZ", by="test")
        payload = engine.run(ticker="ZZZ", no_fetch=True)
        assert payload["expiries"] == []
        assert "No stored option chain for ZZZ" in payload["error"]


class TestReport:
    def test_renders_the_board(self, stored):
        html = report.render_oipd_dashboard(engine.run(None, "TST"))
        assert "Implied Probability" in html
        assert "Risk-neutral" in html
        assert "Last sync:" in html and "EOD" in html
        assert "TST" in html

    def test_has_no_regime_badge(self, stored):
        html = report.render_oipd_dashboard(engine.run(None, "TST"))
        assert "Regime" not in html

    def test_renders_the_empty_state(self):
        html = report.render_oipd_dashboard(
            engine._empty_payload("SPY", "No stored option chain for SPY yet."))
        assert "No stored option chain" in html

    def test_has_in_board_filters_and_combobox(self, stored):
        """docs/dashboard_ui.md §5: filters → one combobox, no Detail level."""
        html = report.render_oipd_dashboard(engine.run(None, "TST"))
        assert 'id="ticker-combo"' in html
        assert 'role="combobox"' in html
        assert "RCCombobox.mount(" in html
        assert 'id="rc-filters"' in html
        assert html.index('id="rc-filters"') < html.index('id="ticker-combo"')
        assert "Detail" not in html
        assert 'id="q"' not in html
        assert 'id="count"' not in html

    def test_combobox_reload_is_store_only_refresh(self, stored):
        html = report.render_oipd_dashboard(engine.run(None, "TST"))
        assert "location.search" in html
        assert "?refresh=1&ticker=" in html

    def test_filters_do_not_hide_the_fitted_board(self, stored):
        """The filter universe is empty here, but the fitted payload (fan/PDF)
        still renders — ``pass()`` only narrows the combobox."""
        html = report.render_oipd_dashboard(engine.run(None, "TST"))
        assert "var DATA = [];" in html
        assert 'id="fan"' in html
        assert 'id="dist"' in html

    def test_default_ticker_is_the_payload_ticker(self, stored):
        html = report.render_oipd_dashboard(engine.run(None, "TST"))
        assert "window.OIPD_TICKER" in html
        assert 'var DEFAULT_TICKER = "TST"' in html

    def test_shows_share_price_next_to_the_ticker(self, stored):
        html = report.render_oipd_dashboard(engine.run(None, "TST"))
        assert "Share price" in html
        assert "$100.00" in html
        assert 'id="share-px"' in html


class TestDensityPayload:
    """The fit payload carries the honest-density fields and the forward-scaled Greeks (#172 M2, M3)."""

    def test_clean_chain_is_unflagged_and_carries_the_fields(self, stored):
        for e in engine.run(None, "TST")["expiries"]:
            assert e["density_flag"] is False and e["density_note"] == "butterfly ok"
            assert e["clipped_pct"] < 0.01 and e["min_var"] > 0
            assert e["g_min_window"] is not None
            assert e["density_g_min_shown"] == min(e["g_min"], e["g_min_window"])

    def test_greeks_are_scaled_by_the_chain_forward_not_spot(self, stored):
        payload = engine.run(None, "TST")
        for e in payload["expiries"]:
            iv, T, F = e["atm_iv"], e["T"], e["forward"]
            d1 = 0.5 * iv * math.sqrt(T)
            disc = math.exp(-_R * T)
            unit_gamma = disc * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (iv * math.sqrt(T))
            assert e["greeks"]["gamma"] * F == pytest.approx(unit_gamma, rel=5e-3)

    def test_steep_smile_is_flagged_with_its_clipped_share(self, stored, monkeypatch):
        steep = dict(a=-0.0378, b=0.5768, rho=-0.9827, m=-0.2705, sigma=0.4598, g_min=0.19, rmse=0.0)
        monkeypatch.setattr(opd_math, "fit_svi", lambda *a, **k: dict(steep))
        payload = engine.run(None, "TST")
        assert payload["expiries"]
        for e in payload["expiries"]:
            assert e["density_flag"] is True
            assert e["density_note"].startswith("density clipped ")
            assert e["clipped_pct"] > 0.01 and e["g_min_window"] < 0
            assert e["density_g_min_shown"] == e["g_min_window"]
        html = report.render_oipd_dashboard(payload)
        assert "e.density_flag" in html and "e.density_note" in html
        assert "statistics unreliable" in html and ".muted{" in html
        # #49: the stat-tile row array is assigned via innerHTML, so the note must be escaped there too
        assert "rcEsc(e.density_note)" in html
        assert ",e.density_note," not in html
        assert "e.g_min<0" not in html, "the old label logic must not come back"
