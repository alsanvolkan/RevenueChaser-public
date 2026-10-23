"""#80: a board rendered from a run whose provider health has a failure shows the banner.

The five data boards (and Settings) call ``provider_health_html(payload.get("provider_health"))``
and nothing used to write that key, so the banner was always empty. Each test builds the board
from a run that already has a failure and asserts the banner is non-empty. Offline; temp store.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from RevenueChaser.core import history, rawstore, screen_common
from RevenueChaser.core.providers import yfinance as yfp

COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
_BANNER = "rc-provider-health"


def _yahoo_failure():
    yfp.reset_health()
    yfp._HEALTH.call(error="HTTP 429", endpoint="AAA.info")


# ── Asymmetric Growth: the run's Yahoo ProviderHealth ────────────────────────────────────────────
def test_ag_rendered_from_a_failed_yahoo_run_shows_the_banner(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_asymmetric_growth import engine, report

    def universe(*_a, **_k):
        _yahoo_failure()
        return [], {}

    monkeypatch.setattr(engine, "fetch_universe_stats", universe)
    result = engine.run_asymmetric_growth_hunter(no_fetch=False)
    html = report.render_asymmetric_growth_hunter_html(result)
    assert _BANNER in html
    assert "429" in html
    assert "Yahoo Finance" in html


# ── Swing: the scan's Yahoo reads ────────────────────────────────────────────────────────────────
def test_swing_rendered_from_a_failed_yahoo_run_shows_the_banner(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_swing_potential import pipeline, report

    def screen(**_k):
        _yahoo_failure()
        return {"generated_at": "2026-10-06T22:00:00+00:00", "candidates": [], "summary": {},
                "no_data": True, "no_data_message": "none"}

    monkeypatch.setattr(pipeline.screen_common, "run_screen", screen)
    payload = pipeline.run_scan(no_fetch=False)
    html = report.render_swing_dashboard(payload)
    assert _BANNER in html
    assert "429" in html


def test_a_failed_swing_ohlc_read_is_counted_for_the_banner(rawstore_tmp_db, monkeypatch):
    """The board's own Yahoo read, not a stand-in: a raised history() is a failed call."""
    import time

    from RevenueChaser.module_swing_potential import indicators

    yfp.reset_health()
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(indicators.yf_session, "recover", lambda *_a, **_k: False)

    class _Boom:
        def history(self, **_k):
            raise RuntimeError("HTTP 429")

    monkeypatch.setattr("yfinance.Ticker", lambda _t: _Boom())
    indicators.load_ohlc("AAA", retries=1)
    health = yfp.health()
    assert health["failed"] >= 1
    assert health["failures"][0]["endpoint"].startswith("AAA")
    assert "429" in health["failures"][0]["error"]


# ── Fundamentals: an EDGAR fetch that raised ─────────────────────────────────────────────────────
def test_fundamentals_rendered_from_a_failed_edgar_run_shows_the_banner(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_fundamentals import pipeline, report

    frame = pd.DataFrame({"Close": [10.0, 11.0, 12.0]})

    monkeypatch.setattr(pipeline, "fetch_universe_stats",
                        lambda **_k: ([{"ticker": "AAA", "market_cap": 1e9}], {}))
    monkeypatch.setattr(pipeline, "prefilter",
                        lambda rows: screen_common.Prefiltered(list(rows), {}, None, 0, []))
    monkeypatch.setattr(screen_common, "load_bars", lambda _t: (frame, frame))
    monkeypatch.setattr(pipeline, "ohlc_drop_reason", lambda _df: None)
    monkeypatch.setattr(pipeline._universe, "excluded_tickers", lambda: set())
    monkeypatch.setattr(pipeline._universe, "spine_gics", lambda *_a, **_k: {})

    class _Gics:
        sector = "Technology"
        industry = "Software"

    monkeypatch.setattr(pipeline._universe, "gics_of", lambda *_a, **_k: _Gics())

    def boom(*_a, **_k):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(pipeline.sec_edgar, "get_fundamentals", boom)
    payload = pipeline.run_scan(no_fetch=False)
    html = report.render_fundamental_dashboard(payload)
    assert _BANNER in html
    assert "503" in html


# ── Co-movement: the fetch report the run already has ───────────────────────────────────────────
def test_comovement_rendered_from_a_failed_fetch_shows_the_banner(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.module_comovement import engine, report

    monkeypatch.setattr(engine.ohlc, "fetch_bulk", lambda *_a, **_k: {
        "requested": 4, "cached": 1, "pending": 1, "fetched": 0, "failed": 2, "no_fetch": False})
    monkeypatch.setattr(engine.ohlc, "price_frame", lambda *_a, **_k: pd.DataFrame())
    payload = engine.run(no_fetch=False, tickers=["AAA", "BBB"])
    html = report.render_comovement_dashboard(payload)
    assert _BANNER in html
    assert "2 of 4" in html


# ── Relatedness: a provider stop the run already records ────────────────────────────────────────
def test_relatedness_rendered_from_an_auth_stop_shows_the_banner(rawstore_tmp_db, monkeypatch, tmp_path):
    from RevenueChaser.module_relatedness import engine, report

    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *_a, **_k: {"no_fetch": True})

    def auth(*_a, **_k):
        raise engine.RunStopped("auth", None, "DeepSeek rejected the API key (HTTP 401)")

    monkeypatch.setattr(engine, "build_profiles", auth)
    payload = engine.run(no_fetch=False, tickers=["AAA"], run_id="r1")
    html = report.render_relatedness_dashboard(payload)
    assert _BANNER in html
    assert "401" in html


# ── Settings: the stored column, not a live client ──────────────────────────────────────────────
def test_settings_banner_comes_from_a_seeded_result_run(rawstore_tmp_db):
    from RevenueChaser.module_settings import inventory, report

    bad = {"provider": "yfinance", "calls": 4, "failed": 2, "auth_error": None,
           "failures": [{"endpoint": "AAA.info", "error": "HTTP 429"}],
           "breaker_open": False, "skipped_calls": 0}
    history.record_run("module_settings", run_id="s1", run_at=COMPLETE, model_version="v",
                       provider_health=bad)
    html = report.render_settings_dashboard(inventory.collect(now=datetime(2026, 10, 6, 22, tzinfo=timezone.utc)))
    assert _BANNER in html
    assert "429" in html
    assert rawstore.query("SELECT 1 FROM result_runs WHERE board = ?", ["module_settings"], db="history")
