"""PR 3: the SPX Gamma board on the market store, against a real dashboard and the local mock provider.

The market store is a fixture ``market.duckdb`` built in this process through the builder's own path
(``module_spx_gamma/tests/rcdata_fixture.py``) in roots the dashboard subprocess is pointed at. A Fetch asks the mock
UW for the board's own snapshot, queues the two builder datasets in ``ops.sqlite`` and renders from what is stored; a
Refresh asks no provider at all; a day the store does not hold says "not stored", and those lines fit a phone in light
and dark.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from .harness.cdp_browser import Page, find_chrome
from .harness.shared import running_dashboard

pytestmark = pytest.mark.e2e

DAY = "2026-10-26"
PHONE = (390, 844)
TAB = "module_spx_gamma"


def _uw_routes(server) -> None:
    """The SPX endpoints of the board's own snapshot (longest prefix wins over the generic one)."""
    server.route("/api/stock/SPX/", payload={"data": []})
    server.route("/api/stock/SPX/gex-levels", payload={"data": {
        "call_wall": "6900", "gamma_flip": "6700", "gamma_magnet": "6750", "put_wall": "6600"}})
    server.route("/api/stock/SPX/greek-exposure/expiry", payload={"data": [
        {"date": DAY, "expiry": DAY, "dte": 0, "call_gex": "5", "put_gex": "-1"}]})
    server.route("/api/stock/SPX/flow-per-strike", payload=[])
    server.route("/api/stock/SPX/greek-exposure/strike-expiry", payload={"data": [
        {"expiry": DAY, "strike": "6750", "call_gex": "50", "put_gex": "-1"}]})


@pytest.fixture
def market_roots(tmp_path, monkeypatch):
    """Market roots of this test: the fixture builds here, and the dashboard subprocess reads here."""
    from RevenueChaser.core import config
    ssd, hdd = tmp_path / "market", tmp_path / "market_raw"
    monkeypatch.setattr(config, "MARKET_DIR", ssd)
    monkeypatch.setattr(config, "MARKET_RAW_DIR", hdd)
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", None)
    return ssd, hdd


def _board(tmp_path, e2e_env, market_roots, *, stored: bool):
    if stored:
        from RevenueChaser.module_spx_gamma.tests import rcdata_fixture
        rcdata_fixture.serve()
    ssd, hdd = market_roots
    env = {**e2e_env, "RC_MARKET_DIR": str(ssd), "RC_MARKET_RAW_DIR": str(hdd), "RC_MARKET_RAW_COPY_DIR": ""}
    store, cache = tmp_path / ".store", tmp_path / ".cache"
    store.mkdir()
    cache.mkdir()
    return running_dashboard(tmp_path, store, cache, env)


def _queued(ssd: Path) -> list[tuple]:
    con = sqlite3.connect(str(ssd / "ops.sqlite"))
    try:
        return con.execute("SELECT dataset, requested_by, status FROM build_requests ORDER BY id").fetchall()
    finally:
        con.close()


def test_a_fetch_queues_the_two_datasets_and_renders_from_the_store(tmp_path, e2e_env, provider_routes,
                                                                   market_roots):
    _uw_routes(provider_routes)
    with _board(tmp_path, e2e_env, market_roots, stored=True) as dash:
        job = dash.run_job("/run/spxgamma", date=DAY)
        assert job.ok, job
        assert _queued(market_roots[0]) == [("uw/spot_expiry_strike", "spx_gamma", "queued"),
                                            ("market_series", "spx_gamma", "queued")]
        html = dash.view(TAB).text
        assert "builder requested at" in html and "the builder runs hourly at :07" in html
        assert "VIX family of 2026-10-26" in html
        # The pin line sits in the prediction panel, which this thin mock day does not fill; the slice it reads is
        # in the parsed snapshot either way.
        parsed = json.loads((tmp_path / "spx_gamma" / "parsed" / DAY / "snapshot.json").read_text(encoding="utf-8"))
        assert parsed["zero_one_dte"]["stored"] is True
        assert parsed["zero_one_dte"]["label"].startswith("0-1 DTE from the 2026-10-26 ")
        assert "CFTC TFF report of 2026-10-20" in html
        assert not [r for r in provider_routes.requests() if "cftc" in r.path.lower()], "no Socrata request"
        assert dash.yf_calls() == [], "no yfinance request: VIX, COT and the window come from the store"
        latest = json.loads((tmp_path / "spx_gamma" / "latest.json").read_text(encoding="utf-8"))
        assert latest["input_sources"]["vix"]["source"] == "rcdata:market_series"

        before = provider_routes.request_count
        refreshed = dash.view(TAB, refresh=True)
        assert refreshed.status_code == 200 and "VIX family of 2026-10-26" in refreshed.text
        assert provider_routes.request_count == before, "Refresh asked a provider"
        assert dash.yf_calls() == []


@pytest.fixture(scope="module")
def chrome():
    path = find_chrome()
    if path is None:
        msg = "no Chrome/Chromium for the SPX phone check: set RC_CHROME or install google-chrome / chromium"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg + " (a skip on CI would leave the check unenforced)")
        pytest.skip(msg)
    return path


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_not_stored_lines_fit_a_phone_in_light_and_dark(tmp_path, e2e_env, provider_routes, market_roots, chrome,
                                                        theme):
    _uw_routes(provider_routes)
    with _board(tmp_path, e2e_env, market_roots, stored=False) as dash:
        assert dash.run_job("/run/spxgamma", date=DAY).ok
        html = dash.view(TAB).text
        for label in ("VIX family not stored for 2026-10-26", "CFTC COT not stored for 2026-10-26",
                      "0-1 DTE not stored for 2026-10-26"):
            assert label in html, label
        with Page(chrome) as page:
            page.emulate(*PHONE, scheme=theme)
            page.open(f"{dash.url}/view/{TAB}")
            page.eval(f"document.documentElement.setAttribute('data-theme', {theme!r})")
            page.settle()
            m = page.eval("""(function(){ var vw = document.documentElement.clientWidth;
              var wide = Array.prototype.filter.call(document.querySelectorAll('body *'), function(e){
                return !(e instanceof SVGElement) && e.getBoundingClientRect().right > vw + 0.5; })
                .map(function(e){ return e.tagName + '.' + e.className; });
              var notes = Array.prototype.filter.call(document.querySelectorAll('.rc-notice, .note'), function(e){
                return /not stored/.test(e.textContent); }).map(function(e){
                return getComputedStyle(e).whiteSpace; });
              return {vw: vw, scroll: document.documentElement.scrollWidth, wide: wide.slice(0, 8),
                      notes: notes}; })()""")
            assert m["vw"] == PHONE[0]
            assert m["scroll"] <= m["vw"], f"the page scrolls sideways: {m['wide']}"
            assert not m["wide"], f"elements reach past the right edge: {m['wide']}"
            assert m["notes"] and "nowrap" not in m["notes"], m["notes"]
