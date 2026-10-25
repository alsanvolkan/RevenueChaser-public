"""Implied Probability engine: UW fetch -> store -> fit, and store-only refresh."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_oipd import engine, opd_math, report

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


class _FakeUW:
    def __init__(self):
        self.expiries = _expiries()
        self.calls = {"breakdown": 0, "contracts": 0, "ohlc": 0}

    def expiry_breakdown(self, ticker, date=None):
        self.calls["breakdown"] += 1
        return [{"expiry": e, "open_interest": 10000, "volume": 500, "chains": 50}
                for e in self.expiries]

    def option_contracts(self, ticker, expiry=None, **kw):
        self.calls["contracts"] += 1
        if expiry in self.expiries:
            return _chain_for(expiry)
        return [c for e in self.expiries for c in _chain_for(e)]

    def ohlc_latest(self, ticker):
        self.calls["ohlc"] += 1
        return {"close": _SPOT}


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr(engine, "_today", lambda: _ASOF)


class TestFetch:
    def test_persists_and_fits(self, fixed_today):
        uw = _FakeUW()
        payload = engine.run(uw, "TST")
        assert payload["error"] is None
        assert payload["spot"] == _SPOT
        assert len(payload["expiries"]) == 4
        assert payload["fan"] is not None
        assert rawstore.count("oipd_chain") > 0
        assert rawstore.get("oipd_meta", "TST")["spot"] == _SPOT

    def test_each_expiry_is_arbitrage_clean(self, fixed_today):
        payload = engine.run(_FakeUW(), "TST")
        for e in payload["expiries"]:
            assert e["g_min"] >= 0.0
            assert abs(e["mean"] - e["forward"]) / e["forward"] < 0.02

    def test_probabilities_bracket_the_spot(self, fixed_today):
        payload = engine.run(_FakeUW(), "TST")
        for e in payload["expiries"]:
            assert 0.0 <= e["prob_below_spot"] <= 1.0
            assert abs(e["prob_below_spot"] + e["prob_above_spot"] - 1.0) < 1e-3

    def test_missing_spot_still_fits_from_the_chain(self, fixed_today):
        # UW returned no regular-session daily bar: the forward comes from the
        # put/call pairs, and only the spot-relative probabilities are unknown.
        class _NoSpotUW(_FakeUW):
            def ohlc_latest(self, ticker):
                return {}

        payload = engine.run(_NoSpotUW(), "TST")
        assert payload["spot"] is None
        assert len(payload["expiries"]) == 4
        assert any("No spot price" in w for w in payload["warnings"])
        for e in payload["expiries"]:
            assert e["forward"] > 0
            for k in ("prob_below_spot", "prob_above_spot", "p_up10", "p_down10"):
                assert e[k] is None


class TestRefresh:
    def test_refresh_reproduces_the_fetch_offline(self, fixed_today):
        fetched = engine.run(_FakeUW(), "TST")
        refreshed = engine.run(ticker="TST", no_fetch=True)
        assert refreshed["n_contracts"] == fetched["n_contracts"]
        assert ([e["quantiles"]["median"] for e in refreshed["expiries"]]
                == [e["quantiles"]["median"] for e in fetched["expiries"]])

    def test_empty_store_returns_an_explicit_error(self):
        payload = engine.run(ticker="ZZZ", no_fetch=True)
        assert payload["expiries"] == []
        assert "No cached chain" in payload["error"]

    def test_unknown_ticker_says_check_the_ticker(self, fixed_today):
        class EmptyUW:
            def expiry_breakdown(self, ticker, date=None):
                return []
            def option_contracts(self, ticker, expiry=None, **kw):
                return []
            def ohlc_latest(self, ticker):
                return {}

        payload = engine.run(EmptyUW(), "APPL")
        assert payload["expiries"] == []
        assert payload["n_contracts"] == 0
        assert "no option chain for APPL" in payload["error"]
        assert "check the ticker" in payload["error"]


class TestReport:
    def test_renders_the_board(self, fixed_today):
        html = report.render_oipd_dashboard(engine.run(_FakeUW(), "TST"))
        assert "Implied Probability" in html
        assert "Risk-neutral" in html
        assert "Last sync:" in html and "EOD" in html
        assert "TST" in html

    def test_has_no_regime_badge(self, fixed_today):
        html = report.render_oipd_dashboard(engine.run(_FakeUW(), "TST"))
        assert "Regime" not in html

    def test_renders_the_empty_state(self):
        html = report.render_oipd_dashboard(
            engine._empty_payload("SPY", "No cached chain for SPY. Run a Fetch first."))
        assert "No cached chain" in html

    def test_has_in_board_filters_and_combobox(self, fixed_today):
        """docs/dashboard_ui.md §5: filters → one combobox, no Detail level."""
        html = report.render_oipd_dashboard(engine.run(_FakeUW(), "TST"))
        assert 'id="ticker-combo"' in html
        assert 'role="combobox"' in html
        assert "RCCombobox.mount(" in html
        assert 'id="rc-filters"' in html
        assert html.index('id="rc-filters"') < html.index('id="ticker-combo"')
        assert "Detail" not in html
        assert 'id="q"' not in html
        assert 'id="count"' not in html

    def test_combobox_reload_is_store_only_refresh(self, fixed_today):
        html = report.render_oipd_dashboard(engine.run(_FakeUW(), "TST"))
        assert "location.search" in html
        assert "?refresh=1&ticker=" in html

    def test_filters_do_not_hide_the_fitted_board(self, fixed_today):
        """The filter universe is empty here, but the fitted payload (fan/PDF)
        still renders — ``pass()`` only narrows the combobox."""
        html = report.render_oipd_dashboard(engine.run(_FakeUW(), "TST"))
        assert "var DATA = [];" in html
        assert 'id="fan"' in html
        assert 'id="dist"' in html

    def test_default_ticker_is_the_payload_ticker(self, fixed_today):
        html = report.render_oipd_dashboard(engine.run(_FakeUW(), "TST"))
        assert "window.OIPD_TICKER" in html
        assert 'var DEFAULT_TICKER = "TST"' in html

    def test_shows_share_price_next_to_the_ticker(self, fixed_today):
        html = report.render_oipd_dashboard(engine.run(_FakeUW(), "TST"))
        assert "Share price" in html
        assert "$100.00" in html
        assert 'id="share-px"' in html


def test_oipd_store_populated(fixed_today):
    """Populates ``oipd_chain`` + ``oipd_meta`` (manifest population gate)."""
    engine.run(_FakeUW(), "TST")
    assert rawstore.count("oipd_chain") > 0
    assert rawstore.get("oipd_meta", "TST") is not None


# ── #166: a failed or partial UW pull is not a chain ─────────────────────────────────────

class _ResultUW(_FakeUW):
    """Implements ``option_contracts_result``: ``partial`` / ``failed`` map an expiry to its outcome."""

    def __init__(self, partial=(), failed=(), status=500):
        super().__init__()
        self.partial, self.failed, self.status = set(partial), set(failed), status

    def option_contracts_result(self, ticker, expiry=None, **kw):
        from RevenueChaser.core.providers.result import ProviderResult
        self.calls["contracts"] += 1
        rows = _chain_for(expiry) if expiry in self.expiries else [
            c for e in self.expiries for c in _chain_for(e)]
        if expiry in self.failed or (expiry is None and self.failed):
            return ProviderResult("failed", [], {"status": self.status, "error": f"HTTP {self.status}", "page": 1}, "")
        if expiry in self.partial:
            return ProviderResult("partial", rows[: len(rows) // 2],
                                  {"status": self.status, "error": f"HTTP {self.status}", "page": 2}, "")
        return ProviderResult("ok", rows, None, "")


def _stored():
    return (rawstore.count("oipd_chain"), rawstore.get("oipd_meta", "TST"))


class TestFailedPulls:
    def test_a_partial_chain_is_fitted_with_a_warning_per_partial_expiry_but_not_saved(self, fixed_today):
        uw = _ResultUW(partial={_expiries()[1], _expiries()[2]})
        payload = engine.run(uw, "TST")
        assert payload["error"] is None and payload["expiries"]            # still shown
        text = " ".join(payload["warnings"])
        assert _expiries()[1] in text and _expiries()[2] in text and "page 2" in text
        assert _expiries()[0] not in text
        assert "not saved" in text
        assert rawstore.count("oipd_chain") == 0 and rawstore.get("oipd_meta", "TST") is None

    def test_a_partial_chain_leaves_the_stored_whole_one_alone(self, fixed_today):
        engine.run(_FakeUW(), "TST")
        before = _stored()
        assert before[0] > 0
        payload = engine.run(_ResultUW(partial={_expiries()[0]}), "TST")
        assert payload["warnings"]
        assert _stored() == before

    def test_a_whole_chain_is_saved_again_after_a_partial_one(self, fixed_today):
        engine.run(_ResultUW(partial={_expiries()[0]}), "TST")
        assert rawstore.count("oipd_chain") == 0
        engine.run(_ResultUW(), "TST")
        assert rawstore.count("oipd_chain") > 0

    def test_an_expiry_that_failed_outright_makes_the_chain_partial(self, fixed_today):
        uw = _ResultUW(failed={_expiries()[3]})
        payload = engine.run(uw, "TST")
        text = " ".join(payload["warnings"])
        assert _expiries()[3] in text and "missing" in text and "not saved" in text
        assert len(payload["expiries"]) == 3
        assert rawstore.count("oipd_chain") == 0

    def test_a_failed_pull_says_the_request_failed_not_check_the_ticker(self, fixed_today):
        uw = _ResultUW(failed=set(_expiries()))
        payload = engine.run(uw, "TST")
        assert payload["expiries"] == [] and payload["n_contracts"] == 0
        assert "Unusual Whales request failed" in payload["error"] and "HTTP 500" in payload["error"]
        assert "check the ticker" not in payload["error"]
        assert rawstore.count("oipd_chain") == 0

    def test_a_failed_pull_leaves_the_stored_chain_alone(self, fixed_today):
        engine.run(_FakeUW(), "TST")
        before = _stored()
        engine.run(_ResultUW(failed=set(_expiries())), "TST")
        assert _stored() == before

    def test_a_404_chain_is_a_missing_chain_not_an_outage(self, fixed_today):
        payload = engine.run(_ResultUW(failed=set(_expiries()), status=404), "APPL")
        assert "check the ticker" in payload["error"] and "request failed" not in payload["error"]

    def test_the_default_chain_path_reports_a_failure_too(self, fixed_today):
        class _NoBreakdown(_ResultUW):
            def expiry_breakdown(self, ticker, date=None):
                return []

        payload = engine.run(_NoBreakdown(failed={None}), "TST")
        assert "Unusual Whales request failed" in payload["error"]

    def test_fetch_still_returns_the_three_tuple(self, fixed_today):
        contracts, meta, warnings = engine.fetch(_FakeUW(), "TST")
        assert contracts and meta["ticker"] == "TST" and warnings == []

    def test_an_empty_but_ok_chain_still_says_check_the_ticker(self, fixed_today):
        class _OkEmpty(_ResultUW):
            def option_contracts_result(self, ticker, expiry=None, **kw):
                from RevenueChaser.core.providers.result import ProviderResult
                return ProviderResult("ok", [], None, "")

        payload = engine.run(_OkEmpty(), "APPL")
        assert "check the ticker" in payload["error"]


class TestDensityPayload:
    """The fit payload carries the honest-density fields and the forward-scaled Greeks (#172 M2, M3)."""

    def test_clean_chain_is_unflagged_and_carries_the_fields(self, fixed_today):
        for e in engine.run(_FakeUW(), "TST")["expiries"]:
            assert e["density_flag"] is False and e["density_note"] == "butterfly ok"
            assert e["clipped_pct"] < 0.01 and e["min_var"] > 0
            assert e["g_min_window"] is not None
            assert e["density_g_min_shown"] == min(e["g_min"], e["g_min_window"])

    def test_greeks_are_scaled_by_the_chain_forward_not_spot(self, fixed_today):
        payload = engine.run(_FakeUW(), "TST")
        for e in payload["expiries"]:
            iv, T, F = e["atm_iv"], e["T"], e["forward"]
            d1 = 0.5 * iv * math.sqrt(T)
            disc = math.exp(-_R * T)
            unit_gamma = disc * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (iv * math.sqrt(T))
            assert e["greeks"]["gamma"] * F == pytest.approx(unit_gamma, rel=5e-3)

    def test_steep_smile_is_flagged_with_its_clipped_share(self, fixed_today, monkeypatch):
        steep = dict(a=-0.0378, b=0.5768, rho=-0.9827, m=-0.2705, sigma=0.4598, g_min=0.19, rmse=0.0)
        monkeypatch.setattr(opd_math, "fit_svi", lambda *a, **k: dict(steep))
        payload = engine.run(_FakeUW(), "TST")
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
