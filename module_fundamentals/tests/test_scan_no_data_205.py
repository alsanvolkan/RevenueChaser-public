"""#205: a scan never falls back to the demo fixture; it reports why it has no data.

Parametrised over Fundamentals and Swing, which share ``screen_common.load_bars`` (D01 since C12, #383 R3) and the
SPY fallback ``load_ohlc``.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import rawstore, screen_common
from RevenueChaser.core.scan_quality import no_data_payload
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_swing_potential import pipeline as sp
from RevenueChaser.module_swing_potential.report import render_swing_dashboard
from RevenueChaser.module_swing_potential.tests.test_ohlc import _df, _seed_ohlc

NOW = datetime(2026, 10, 14, 21, 45, tzinfo=timezone.utc)      # the 14 Oct session is complete

MODS = [(fp, render_fundamental_dashboard), (sp, render_swing_dashboard)]
ANNUAL = [{"revenue": 100.0 * (i + 1), "gross_profit": 50.0, "operating_cash_flow": 20.0,
           "capex": 5.0, "total_debt": 10.0, "cash": 30.0, "total_assets": 200.0,
           "total_equity": 100.0, "net_income": 10.0} for i in range(3)]


class _FailingTicker:
    def __init__(self, t):
        pass

    def history(self, *a, **k):
        raise RuntimeError("Too Many Requests. Rate limited.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("yfinance was called for real")
    monkeypatch.setattr("yfinance.Ticker", boom)
    monkeypatch.setattr("yfinance.download", boom)
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def scan_env(rawstore_tmp_db, monkeypatch):
    rows = [{"ticker": t, "market_cap": 1e9, "price": 10.0, "name": t, "sector": "Technology"}
            for t in ("AAA", "BBB", "SPY2")]
    for mod in (fp, sp):
        monkeypatch.setattr(mod, "fetch_universe_stats", lambda **kw: (rows, {"out_of_universe": 0}))
        monkeypatch.setattr(mod, "prefilter", lambda r: screen_common.Prefiltered(list(r)))
        monkeypatch.setattr(mod, "load_ticker_info", lambda *a, **k: {})
    monkeypatch.setattr(fp.sec_edgar, "get_fundamentals", lambda *a, **k: (ANNUAL, [], {}))
    monkeypatch.setattr(sp, "data_as_of", lambda: None)
    monkeypatch.setattr("yfinance.Ticker", _FailingTicker)
    monkeypatch.setattr(cal, "_now", lambda: NOW)
    return rows


def _age_all():
    with rawstore.connect() as conn:
        conn.execute("UPDATE swing_ohlc SET cached_at = ?", [time.time() - 2 * 86400])


@pytest.mark.parametrize("mod,render", MODS)
def test_stale_cache_plus_failing_yfinance_still_scores_and_counts_stale(scan_env, mod, render):
    """The picks' D01 bars stop on 9 Oct (scored, counted stale); SPY has no D01 session, so its fallback serves
    the aged cache when yfinance fails (``spy_stale``)."""
    from RevenueChaser.core.tests.screen_parity import _bars, seed_d01
    for t in ("AAA", "BBB"):
        seed_d01(t, _bars("up", 0.001, 300, end=date(2026, 10, 9)))
    _seed_ohlc("SPY", _df(300))
    _age_all()
    payload = mod.run_scan()
    assert not payload.get("is_fixture")             # #49 A2: Swing payloads no longer carry the key
    assert not payload.get("no_data")
    assert payload["data_quality"]["stale_ohlc"] == 2
    assert payload["data_quality"]["spy_stale"] is True
    assert "stale" in render(payload).lower()


@pytest.mark.parametrize("mod,render", MODS)
def test_no_data_anywhere_is_an_explicit_empty_state_not_the_fixture(scan_env, monkeypatch, mod, render):
    persisted = []
    monkeypatch.setattr("RevenueChaser.module_fundamentals.scores.persist", lambda r: persisted.append(r))
    monkeypatch.setattr("RevenueChaser.module_swing_potential.scores.persist", lambda r: persisted.append(r))
    payload = mod.run_scan()
    assert not payload.get("is_fixture")             # #49 A2: Swing payloads no longer carry the key
    assert payload["no_data"] is True
    assert "3" in payload["no_data_message"]
    assert payload["data_quality"]["dropped"]["no_bars"] >= 1                 # no D01 bars at all
    assert payload["candidates"] == []
    assert persisted == []
    html = render(payload)
    assert "[DEMO/FIXTURE]" not in html
    assert "No data" in html


@pytest.mark.parametrize("mod,render", MODS)
def test_empty_universe_says_so(rawstore_tmp_db, monkeypatch, mod, render):
    monkeypatch.setattr(mod, "fetch_universe_stats", lambda **kw: ([], {}))
    payload = mod.run_scan()
    assert payload["no_data"] is True and payload["is_fixture"] is False
    assert payload["no_data_reason"] == "no_universe"
    assert "universe" in payload["no_data_message"]


@pytest.mark.parametrize("mod,render", MODS)
def test_no_data_message_is_escaped(rawstore_tmp_db, monkeypatch, mod, render):
    payload = no_data_payload(mod.build_payload([], run_stats={}), "x", {"<script>alert(1)</script>": 1}, 1)
    html = render(payload)
    assert "<script>alert(1)</script>" not in html


def test_ohlc_drop_reason_252_bar_threshold():
    from RevenueChaser.core.scan_quality import ohlc_drop_reason
    assert ohlc_drop_reason(_df(251)) == "short_bars"
    assert ohlc_drop_reason(_df(252)) == ""


@pytest.mark.parametrize("mod,render", MODS)
def test_no_universe_warning_has_no_literal_format_marker(rawstore_tmp_db, monkeypatch, caplog, mod, render):
    monkeypatch.setattr(mod, "fetch_universe_stats", lambda **kw: ([], {}))
    with caplog.at_level("WARNING"):
        mod.run_scan()
    assert caplog.records and all("%s" not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("render,count", [(render_fundamental_dashboard, "0 names shown"),
                                          (render_swing_dashboard, "0 setups shown")])
def test_the_page_script_keeps_the_server_side_no_data_message_DF111(render, count, tmp_path):
    """DF111 under node: with no candidates render() leaves the server's no-data message alone."""
    from RevenueChaser.core.tests.board_js import run_board_js
    html = render({"is_fixture": False, "no_data": True, "no_data_message": "No universe stored.",
                   "no_data_reason": "", "candidates": [], "data_quality": {"spy_stale": False}})
    assert "No universe stored." in html
    out = run_board_js(html, 'console.log(JSON.stringify({g: (_els["grid"]||{innerHTML:""}).innerHTML, '
                             'c: (_els["count"]||{textContent:"(untouched)"}).textContent}))', tmp_path)
    assert out == {"g": "", "c": count}
