"""#202: a legacy double-encoded ``edgar_financials.meta`` must not drop EDGAR-backed tickers
out of Asymmetric Growth, and a scoring failure must be counted and shown, not swallowed."""
import json
import logging

import duckdb
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_asymmetric_growth import engine
from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
from RevenueChaser.module_asymmetric_growth.tests.test_score_ticker import _fin, _tech

_ANNUAL = _fin()["annual"]


def _quote():
    return {"regularMarketPrice": 100.0, "fiftyTwoWeekHigh": 105.0, "fiftyDayAverage": 90.0,
            "averageDailyVolume10Day": 160.0, "averageDailyVolume3Month": 100.0}


def _seed_legacy_edgar(meta_cell: str):
    """A row exactly as the live store holds it: JSON-typed columns, meta double-encoded."""
    with duckdb.connect(str(rawstore.edgar_db_path())) as c:
        c.execute("CREATE TABLE edgar_financials (ticker TEXT PRIMARY KEY, "
                  "annual JSON, quarterly JSON, meta JSON)")
        c.execute("INSERT INTO edgar_financials VALUES ('TEST', ?, '[]', ?)",
                  [json.dumps(json.dumps(_ANNUAL)), meta_cell])


def _no_yfinance_cache(monkeypatch):
    fin = {k: v for k, v in _fin().items() if k != "annual"}
    monkeypatch.setattr(engine.cache_manager, "load", lambda t, ignore_ttl=False, allow_info_only=False: dict(fin))


def test_double_encoded_meta_keeps_the_ticker(isolate_rawstore, monkeypatch):
    from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import seed_momentum
    _seed_legacy_edgar(json.dumps(json.dumps({"source": "sec_edgar", "is_us": True})))
    _no_yfinance_cache(monkeypatch)
    seed_momentum(["TEST"])                                   # the momentum signal comes from D01 (#380 S4)
    card = engine.enrich_and_score("TEST", {"quote": _quote()}, no_fetch=True, spine={})
    assert card is not None and card["ticker"] == "TEST"
    assert next(s for s in card["signals"] if s["key"] == "momentum")["passed"] == 3


def test_get_fundamentals_meta_is_always_a_dict(monkeypatch):
    monkeypatch.setattr(sec_edgar.rawstore, "get", lambda *a, **k: {
        "annual": _ANNUAL, "quarterly": [], "meta": '{"is_us": true}'})
    assert sec_edgar.get_fundamentals("TEST", no_fetch=True)[2] == {}


def test_a_programming_error_is_not_swallowed(monkeypatch):
    def boom(*a, **k):
        raise AttributeError("'str' object has no attribute 'get'")
    monkeypatch.setattr(engine, "_cached_financials", boom)
    with pytest.raises(AttributeError):
        engine.enrich_and_score("TEST", {"quote": _quote()}, no_fetch=True, spine={})


def test_a_provider_error_still_just_skips_the_ticker(monkeypatch):
    def down(*a, **k):
        raise ConnectionError("offline")
    monkeypatch.setattr(engine, "_cached_financials", down)
    assert engine.enrich_and_score("TEST", {"quote": _quote()}, no_fetch=True, spine={}) is None


def _run(monkeypatch, rows, fake):
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    monkeypatch.setattr(engine, "enrich_and_score", fake)
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})
    return engine.run_asymmetric_growth_hunter()


def test_runner_counts_and_logs_dropped_tickers(monkeypatch, caplog):
    def fake(t, r, nf=False, spine=None, preload=None):
        if t == "BAD":
            raise AttributeError("boom")
        return engine.score_ticker(t, _fin(), _tech(), {}, spine or {})
    with caplog.at_level(logging.WARNING):
        out = _run(monkeypatch, [{"ticker": "OK"}, {"ticker": "BAD"}], fake)
    assert out["summary"]["scanned"] == 1
    assert out["summary"]["score_errors"] == 1
    assert out["summary"]["score_error_types"] == {"AttributeError": 1}
    assert any("BAD" in m and "AttributeError" in m for m in caplog.messages)


def test_board_shows_the_dropped_count_escaped():
    base = {"asof": "2026-10-02T00:00:00", "universe_total": 1, "cards": [], "thresholds": engine.THRESHOLDS,
            "summary": {"scanned": 0, "score_errors": 3,
                        "score_error_types": {"<img src=x>": 3}}}
    page = render_asymmetric_growth_hunter_html(base)
    assert "3 ticker(s) could not be scored" in page
    assert "<img src=x>" not in page and "&lt;img src=x&gt;" in page
    clean = render_asymmetric_growth_hunter_html({**base, "summary": {"scanned": 0}})
    assert "could not be scored" not in clean
