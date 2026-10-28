"""Shared fixtures: synthetic raw payload + normalized snapshot (no network)."""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.module_spx_gamma.normalize import normalize_snapshot
from RevenueChaser.module_spx_gamma.snapshot import UWApiError


class FakeUW:
    """A ``UWClient`` stand-in: ``get_result`` over a ``handler(path, params) -> payload``.

    The handler may raise ``UWApiError`` (becomes a failed result, as the real client does).
    ``prefix`` is added to the path before the handler sees it, because the real client's
    paths carry no ``/api`` (its BASE does) while older fixtures were written with one.
    """

    def __init__(self, handler, prefix: str = "/api"):
        self.handler = handler
        self.prefix = prefix
        self.paths: list[str] = []

    def get_result(self, ep, params=None):
        self.paths.append(ep)
        try:
            payload = self.handler(f"{self.prefix}{ep}", params)
        except UWApiError as exc:
            return ProviderResult("failed", {}, {"status": exc.status, "error": exc.message, "attempts": 1}, ep)
        return ProviderResult("ok", payload, None, ep)


def as_uw(fake):
    """Wrap an old-style fake with a ``get(path, params)`` method as a ``UWClient`` stand-in."""
    return FakeUW(fake.get)


def make_candles() -> list[dict]:
    dates = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
             "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    base = 7740.0
    rows = []
    for i, d in enumerate(dates):
        rows.append({
            "date": d,
            "open": base + i * 4,
            "high": base + i * 4 + 15,
            "low": base + i * 4 - 15,
            "close": base + i * 4 + 8,
            "volume": 1_000_000 + i * 1000,
        })
    return rows


def _chain_row(expiry: str, strike: float, cp: str, bid: float, ask: float,
               theta: float, iv: float = 0.10) -> dict:
    """Synthetic /option-contracts row with a deterministic NBBO mid."""
    ymd = expiry.replace("-", "")[2:]
    sym = f"SPXW{ymd}{cp}{int(strike * 1000):08d}"
    return {
        "option_symbol": sym,
        "nbbo_bid": str(bid),
        "nbbo_ask": str(ask),
        "implied_volatility": str(iv),
        "delta": "0.5" if cp == "C" else "-0.5",
        "gamma": "0.01",
        "theta": str(theta),
        "vega": "1.5",
        "open_interest": "1000",
        "volume": "500",
    }


def make_option_chain() -> dict:
    """Synthetic 0DTE chain for the pin-harvest panel.

    spot = 7784, prediction pin = 7800, EM = 7784*0.076/sqrt(252) ≈ 36.9 pts
    -> wing width 35 -> wings 7765 / 7835.
        short straddle 7800: call mid 20, put mid 15
        long wings: 7835 call mid 5, 7765 put mid 6
    -> net credit 24, max risk 11, R:R 2.18, breakevens 7776 / 7824.
    """
    exp = "2026-08-14"
    rows = [
        _chain_row(exp, 7800.0, "C", 19.0, 21.0, -8.0),
        _chain_row(exp, 7800.0, "P", 14.0, 16.0, -7.0),
        _chain_row(exp, 7835.0, "C", 4.0, 6.0, -3.0),
        _chain_row(exp, 7765.0, "P", 5.0, 7.0, -2.0),
        # extra strikes so nearest-strike fallback is exercised / unambiguous
        _chain_row(exp, 7850.0, "C", 3.0, 4.0, -2.5),
        _chain_row(exp, 7750.0, "P", 8.0, 10.0, -3.5),
    ]
    return {"data": rows}


def make_raw() -> dict:
    return {
        "gex_levels": {"data": {
            "call_wall": "7900", "gamma_flip": "7820.63",
            "gamma_magnet": "7900", "put_wall": "7750",
            "nearby_flips": ["7820.63", "7790.0"],
        }},
        "gex_levels_vol": {"data": {
            "call_wall": "7715", "gamma_flip": "7691.65",
            "gamma_magnet": "7710", "put_wall": "7705",
            "nearby_flips": ["7691.65", "7688.2"],
        }},
        "expiry_exposure": {"data": [
            {"date": "2026-08-14", "expiry": "2026-08-14", "dte": 0,
             "call_gex": "83563.6633", "put_gex": "-29026.4049",
             "call_delta": "9403904.7015", "put_delta": "-1138039.7855"},
            {"date": "2026-08-14", "expiry": "2026-08-21", "dte": 7,
             "call_gex": "50000", "put_gex": "-40000",
             "call_delta": "100000000", "put_delta": "-50000000"},
        ]},
        "strike_exposure": {"data": [
            {"strike": "7700", "call_gex": "1", "put_gex": "-300", "call_delta": "100000000", "put_delta": "-50000000"},
            {"strike": "7750", "call_gex": "100", "put_gex": "-50", "call_delta": "150000000", "put_delta": "-30000000"},
            {"strike": "7800", "call_gex": "350", "put_gex": "-100", "call_delta": "200000000", "put_delta": "-20000000"},
            {"strike": "7850", "call_gex": "150", "put_gex": "-20", "call_delta": "120000000", "put_delta": "-10000000"},
            {"strike": "7900", "call_gex": "200", "put_gex": "-2", "call_delta": "80000000", "put_delta": "-5000000"},
            {"strike": "8000", "call_gex": "50", "put_gex": "-5", "call_delta": "30000000", "put_delta": "-2000000"},
        ]},
        "strike_exposure_prev": {"data": [
            {"strike": "7700", "call_gex": "1", "put_gex": "-290", "call_delta": "95000000", "put_delta": "-48000000"},
            {"strike": "7750", "call_gex": "90", "put_gex": "-45", "call_delta": "145000000", "put_delta": "-29000000"},
            {"strike": "7800", "call_gex": "330", "put_gex": "-95", "call_delta": "195000000", "put_delta": "-19000000"},
            {"strike": "7850", "call_gex": "140", "put_gex": "-18", "call_delta": "115000000", "put_delta": "-9500000"},
            {"strike": "7900", "call_gex": "190", "put_gex": "-2", "call_delta": "75000000", "put_delta": "-4800000"},
            {"strike": "8000", "call_gex": "45", "put_gex": "-4", "call_delta": "28000000", "put_delta": "-1900000"},
        ]},
        "strike_expiry_all": {
            "2026-08-21": {"data": [
                {"expiry": "2026-08-21", "strike": "7600", "call_gex": "5", "put_gex": "-300", "dte": 7},
                {"expiry": "2026-08-21", "strike": "7750", "call_gex": "10", "put_gex": "-150", "dte": 7},
                {"expiry": "2026-08-21", "strike": "7900", "call_gex": "30", "put_gex": "-20", "dte": 7},
            ]},
            "2026-08-15": {"data": [
                {"expiry": "2026-08-15", "strike": "7650", "call_gex": "2", "put_gex": "-200", "dte": 1},
                {"expiry": "2026-08-15", "strike": "7800", "call_gex": "5", "put_gex": "-120", "dte": 1},
                {"expiry": "2026-08-15", "strike": "7900", "call_gex": "10", "put_gex": "-30", "dte": 1},
            ]},
            "2026-09-18": {"data": [
                {"expiry": "2026-09-18", "strike": "7600", "call_gex": "10", "put_gex": "-5", "dte": 35},
                {"expiry": "2026-09-18", "strike": "7750", "call_gex": "30", "put_gex": "-2", "dte": 35},
                {"expiry": "2026-09-18", "strike": "7900", "call_gex": "50", "put_gex": "-1", "dte": 35},
            ]},
            "2026-12-31": {"data": [
                {"expiry": "2026-12-31", "strike": "7500", "call_gex": "5", "put_gex": "-3", "dte": 139},
                {"expiry": "2026-12-31", "strike": "7700", "call_gex": "40", "put_gex": "-2", "dte": 139},
                {"expiry": "2026-12-31", "strike": "8000", "call_gex": "20", "put_gex": "-1", "dte": 139},
            ]},
            "2027-06-18": {"data": [
                {"expiry": "2027-06-18", "strike": "7500", "call_gex": "60", "put_gex": "-2", "dte": 308},
                {"expiry": "2027-06-18", "strike": "7650", "call_gex": "5", "put_gex": "-1", "dte": 308},
                {"expiry": "2027-06-18", "strike": "7900", "call_gex": "10", "put_gex": "-1", "dte": 308},
            ]},
        },
        "spot_exposure": {"data": [
            {"strike": "7650",
             "call_gamma_oi": "500000000", "put_gamma_oi": "-1200000000",
             "call_gamma_vol": "30000000", "put_gamma_vol": "-40000000",
             "call_charm_oi": "0", "put_charm_oi": "0",
             "call_charm_vol": "0", "put_charm_vol": "0",
             "call_vanna_oi": "0", "put_vanna_oi": "0",
             "call_vanna_vol": "0", "put_vanna_vol": "30000000"},
            {"strike": "7850",
             "call_gamma_oi": "900000000", "put_gamma_oi": "-300000000",
             "call_gamma_vol": "-50000000", "put_gamma_vol": "-100000000",
             "call_charm_oi": "0", "put_charm_oi": "0",
             "call_charm_vol": "0", "put_charm_vol": "0",
             "call_vanna_oi": "0", "put_vanna_oi": "0",
             "call_vanna_vol": "15000000", "put_vanna_vol": "0"},
            {"strike": "7900",
             "call_gamma_oi": "-600000000", "put_gamma_oi": "200000000",
             "call_gamma_vol": "120000000", "put_gamma_vol": "-50000000",
             "call_charm_oi": "-20000000", "put_charm_oi": "5000000",
             "call_charm_vol": "-50", "put_charm_vol": "20",
             "call_vanna_oi": "500000", "put_vanna_oi": "100000",
             "call_vanna_vol": "0", "put_vanna_vol": "0"},
            {"strike": "8000",
             "call_gamma_oi": "-1500000000", "put_gamma_oi": "300000000",
             "call_gamma_vol": "-80000000", "put_gamma_vol": "60000000",
             "call_charm_oi": "-40000000", "put_charm_oi": "10000000",
             "call_charm_vol": "-100", "put_charm_vol": "50",
             "call_vanna_oi": "300000", "put_vanna_oi": "200000",
             "call_vanna_vol": "0", "put_vanna_vol": "0"},
        ]},
        "interpolated_iv": {"data": [
            {"date": "2026-08-14", "days": 1, "percentile": "0.001", "volatility": "0.076", "implied_move_perc": "0.001"},
            {"date": "2026-08-14", "days": 7, "percentile": "0.2", "volatility": "0.12", "implied_move_perc": "0.02"},
        ]},
        "strike_expiry": {
            "2026-08-14": {"data": [
                {"expiry": "2026-08-14", "strike": "7700", "call_gex": "1", "put_gex": "-300"},
                {"expiry": "2026-08-14", "strike": "7800", "call_gex": "350", "put_gex": "-100"},
                {"expiry": "2026-08-14", "strike": "7900", "call_gex": "200", "put_gex": "-2"},
            ]},
        },
        "option_chain": make_option_chain(),
        "ohlc": {"source": "yfinance ^GSPC", "data": make_candles()},
    }


def make_snapshot() -> dict:
    return normalize_snapshot(make_raw(), "2026-08-14")


@pytest.fixture(autouse=True)
def market_roots(tmp_path_factory, monkeypatch):
    """Every test gets its own market roots (ops.sqlite and market.duckdb), as rcdata's own tests do: a serving file
    or a queued request one test left must never be the next test's input (PR 3).

    rcdata's ``market_roots`` puts them in ``tmp_path``; here they get a directory of their own, because many of
    these tests use ``tmp_path`` as the board's data root and list what is in it.
    """
    from RevenueChaser.core import config
    from RevenueChaser.rcdata import ops
    root = tmp_path_factory.mktemp("market")
    ssd, hdd = root / "market", root / "market_raw"
    monkeypatch.setattr(config, "MARKET_DIR", ssd)
    monkeypatch.setattr(config, "MARKET_RAW_DIR", hdd)
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", None)
    monkeypatch.setenv("RC_MARKET_DIR", str(ssd))
    monkeypatch.setenv("RC_MARKET_RAW_DIR", str(hdd))
    monkeypatch.setenv("RC_MARKET_RAW_COPY_DIR", "")
    monkeypatch.setattr(ops, "read_log", ops.ReadLog(process="test"))
    return ssd, hdd


@pytest.fixture
def stored_make_raw_window():
    """The market store holding ``make_candles()``'s closes as Cboe's SPX series, for a test that builds a day from
    ``make_raw()`` through ``run.build_day`` (which reads the window from the store since PR 3, not from the raw
    day). Cboe's SPX file has one column, so the stored window has closes only."""
    from datetime import date
    from RevenueChaser.module_spx_gamma.tests import rcdata_fixture
    rcdata_fixture.serve(spx={}, symbols=("SPX",), cot_reports=(), series_end=date(2026, 8, 14),
                         closes={c["date"]: c["close"] for c in make_candles()})


@pytest.fixture
def rcdata_market():
    """The default fixture ``market.duckdb`` (five SPX sessions, the VIX family, SPX closes, four COT reports)."""
    from RevenueChaser.module_spx_gamma.tests import rcdata_fixture
    rcdata_fixture.serve()
    return rcdata_fixture


@pytest.fixture
def raw():
    return make_raw()


@pytest.fixture
def snapshot():
    return make_snapshot()


@pytest.fixture(autouse=True)
def market_closed(monkeypatch):
    """Pin the US session shut for every test in this module.

    Since D03 the snapshot reads a LIVE spot while the session is open (the owner's choice, 2026-10-22), so any
    test that builds a snapshot would otherwise pass or fail by the time of day it runs — the strike window is
    derived from spot. A test that wants the open-session path patches ``in_us_session_window`` itself.
    """
    from RevenueChaser.core import calendar as nyse_calendar
    monkeypatch.setattr(nyse_calendar, "in_us_session_window", lambda *a, **k: False)
