"""PR 3, sub-step 4: VIX, COT and the candle window come from the market store; there is no live fallback.

A Fetch goes through the real ``UWClient`` with every request answered at the HTTP transport (rcdata's
``http_mock``), so the test sees each request the Fetch sends, to UW or anywhere else. yfinance is spied on at its
``Ticker``: the only yfinance read left is the live spot during an open session (A7), and these runs are outside it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.module_spx_gamma import render, run as spx_run, snapshot as snap
from RevenueChaser.rcdata.tests.conftest import http_mock  # noqa: F401  (the transport-level mock)

from . import rcdata_fixture as fx

DAY = "2026-10-26"
UW_HOST = "uw.test"

# The UW requests of a Fetch of 26 Oct 2026 with the fixture's SPX closes (spot 6749 = the 26 Oct close), captured
# on the code before PR 3 (snapshot.py at ee29e0a, its candles handed in with the same closes) and unchanged by it.
# The count is 18 + one all-expiry strike-expiry call per expiry the expiry answer lists (two here; SPX lists dozens
# live), plus the ~10-day GEX history backfill run() starts afterwards.
TODAYS_UW_REQUESTS = [
    ("/api/stock/SPX/gex-levels", [("date", DAY), ("source", "oi")]),
    ("/api/stock/SPX/gex-levels", [("date", DAY), ("source", "vol")]),
    ("/api/stock/SPX/greek-exposure/expiry", [("date", DAY)]),
    ("/api/stock/SPX/greek-exposure/strike", [("date", DAY)]),
    ("/api/stock/SPX/interpolated-iv", []),
    ("/api/stock/SPX/max-pain", []),
    ("/api/stock/SPX/iv-rank", []),
    ("/api/stock/SPX/options-volume", []),
    ("/api/stock/SPX/volatility/term-structure", []),
    ("/api/stock/SPX/volatility/variance-risk-premium", []),
    ("/api/stock/SPX/volatility/realized", []),
    ("/api/stock/SPX/flow-per-strike", [("date", DAY)]),
    ("/api/stock/SPX/spot-exposures/strike",
     [("date", DAY), ("limit", "500"), ("max_strike", "6749"), ("min_strike", "5061")]),
    ("/api/stock/SPX/spot-exposures/strike",
     [("date", DAY), ("limit", "500"), ("max_strike", "8436"), ("min_strike", "6750")]),
    ("/api/stock/SPX/greek-exposure/strike-expiry", [("date", DAY), ("expiry", DAY)]),
    ("/api/stock/SPX/greek-exposure/strike-expiry", [("date", DAY), ("expiry", DAY)]),
    ("/api/stock/SPX/greek-exposure/strike-expiry", [("date", DAY), ("expiry", "2026-10-27")]),
    ("/api/stock/SPX/option-contracts", [("expiry", DAY)]),
    ("/api/stock/SPX/option-contracts", []),
    ("/api/stock/SPX/greek-exposure/strike", [("date", "2026-10-23")]),
]


def _uw_answer(request):
    path = request.url.path
    if path.endswith("/gex-levels"):
        body = {"data": {"call_wall": "6900", "gamma_flip": "6700", "gamma_magnet": "6800", "put_wall": "6600"}}
    elif path.endswith("/greek-exposure/expiry"):
        body = {"data": [{"date": DAY, "expiry": DAY, "dte": 0, "call_gex": "5", "put_gex": "-1"},
                         {"date": DAY, "expiry": "2026-10-27", "dte": 1, "call_gex": "3", "put_gex": "-2"}]}
    elif path.endswith("/flow-per-strike"):
        body = []
    elif path.endswith("/spot-exposures/strike"):
        body = {"data": [{"strike": "6750", "call_gamma_oi": "1000", "put_gamma_oi": "-500"}]}
    elif path.endswith("/strike-expiry"):
        body = {"data": [{"expiry": DAY, "strike": "6760", "call_gex": "5", "put_gex": "-1"}]}
    else:
        body = {"data": []}
    return 200, json.dumps(body).encode(), {"content-type": "application/json"}


@pytest.fixture
def uw(monkeypatch, http_mock):  # noqa: F811
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", f"https://{UW_HOST}/api")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc))
    http_mock.add(UW_HOST, _uw_answer)
    return unusual_whales.UWClient(key="test-key")


@pytest.fixture
def yfinance_calls(monkeypatch):
    import yfinance
    calls = []

    def ticker(symbol, *a, **k):
        calls.append(symbol)
        raise AssertionError(f"yfinance asked for {symbol}")
    monkeypatch.setattr(yfinance, "Ticker", ticker)
    return calls


def _uw_requests(http_mock):  # noqa: F811
    return [(r.url.path, sorted(parse_qsl(r.url.query.decode(), keep_blank_values=True)))
            for r in http_mock.requests if r.url.host == UW_HOST]


def test_the_uw_requests_of_a_fetch_are_todays(uw, http_mock, rcdata_market, tmp_path):  # noqa: F811
    snap.fetch_snapshot(uw, DAY, data_root=tmp_path / "raw", fetch_all_expiries=True)
    assert _uw_requests(http_mock) == TODAYS_UW_REQUESTS
    assert len(TODAYS_UW_REQUESTS) == 20


def test_a_fetch_with_nothing_stored_asks_no_live_source_and_says_not_stored(uw, http_mock, yfinance_calls,  # noqa: F811
                                                                             tmp_path, monkeypatch):
    monkeypatch.setattr(spx_run, "_backfill_history", lambda *a, **k: None)
    out = spx_run.run(DAY, data_root=tmp_path, uw=uw)
    assert yfinance_calls == []
    assert {r.url.host for r in http_mock.requests} == {UW_HOST}, "no Socrata, no Yahoo, no other host"
    day_dir = tmp_path / "raw" / DAY
    assert (day_dir / "manifest.json").exists()
    for name in ("vix_family.json", "cftc_tff.json", "ohlc_8wk.json"):
        assert not (day_dir / name).exists(), name
    html = out["paths"]["report_html"].read_text(encoding="utf-8")
    for label in ("VIX family not stored for 2026-10-26", "CFTC COT not stored for 2026-10-26",
                  "SPX closes not stored for 2026-10-26", "0-1 DTE not stored for 2026-10-26"):
        assert label in html, label


def test_a_fetch_reads_vix_cot_and_candles_from_the_store(uw, http_mock, yfinance_calls, rcdata_market,  # noqa: F811
                                                          tmp_path, monkeypatch):
    monkeypatch.setattr(spx_run, "_backfill_history", lambda *a, **k: None)
    out = spx_run.run(DAY, data_root=tmp_path, uw=uw)
    s = out["snapshot"]
    assert yfinance_calls == [] and {r.url.host for r in http_mock.requests} == {UW_HOST}
    assert s["vix"]["vix"] is not None and s["vix"]["term_mode"] == "contango"
    assert s["cftc"]["report_date"] == "2026-10-20" and s["cftc"]["rows"][0]["change"] == 10_000.0
    assert s["candles"][-1]["date"] == DAY and s["candles"][-1]["close"] == 6749.0
    assert s["candle_source"].startswith("rcdata:market_series")
    inputs = s["store_inputs"]
    assert {k: v["stored"] for k, v in inputs.items()} == {"vix": True, "cftc": True, "candles": True,
                                                           "zero_one_dte": True}
    manifest = json.loads((tmp_path / "raw" / DAY / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["input_sources"]["candles"]["source"] == "rcdata:market_series"
    assert manifest["input_sources"]["candles"]["status"] == "changed"
    assert not any(n in manifest["sha256"] for n in ("vix_family.json", "cftc_tff.json", "ohlc_8wk.json"))
    latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert set(latest["input_sources"]) == {"vix", "cftc", "candles", "zero_one_dte"}
    assert latest["input_sources"]["zero_one_dte"]["source"] == "rcdata:spx_gamma_expiry_strike"


def test_refresh_renders_what_the_fetch_rendered_from_the_store(uw, http_mock, rcdata_market, tmp_path,  # noqa: F811
                                                                monkeypatch):
    monkeypatch.setattr(spx_run, "_backfill_history", lambda *a, **k: None)
    fetched = spx_run.run(DAY, data_root=tmp_path, uw=uw)["snapshot"]
    before = len(http_mock.requests)
    refreshed = spx_run.refresh(DAY, data_root=tmp_path)["snapshot"]
    assert len(http_mock.requests) == before, "Refresh makes no request"
    for key in ("vix", "cftc", "candles", "store_inputs", "zero_one_dte"):
        assert refreshed[key] == fetched[key], key


def test_the_three_files_are_no_longer_live_files():
    assert not {"vix_family.json", "cftc_tff.json", "ohlc_8wk.json"} & snap.LIVE_FILES
    assert not {"vix", "cftc"} & set(render._PANEL_FILES)


def test_the_snapshot_imports_no_live_vix_or_cot_reader():
    import inspect
    src = inspect.getsource(snap)
    for name in ("fetch_vix_family", "fetch_cftc_tff", "stored_inputs", "index_levels"):
        assert name not in src, name
