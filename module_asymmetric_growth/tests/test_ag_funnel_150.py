"""#150 (#232 folded in): the AG header says how many names were SCORED of how many were screened, and why
the others have no card. Synthetic store, no network.

The ledger's freeze guard computes attempted = universe_total - excluded_types and reads score_errors
(``freeze.py::_score_error_problem``): those three inputs keep their meaning.
"""
from __future__ import annotations

import copy

import pytest

from RevenueChaser.core import funnel, rawstore
from RevenueChaser.module_asymmetric_growth import engine, freeze
from RevenueChaser.module_asymmetric_growth.report import render_asymmetric_growth_hunter_html
from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _ANNUAL, _QUOTE, _edgar
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import seed_momentum


def _universe(monkeypatch, rows):
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})


def _row(t, quote=True):
    return {"ticker": t, "quote": dict(_QUOTE) if quote else {}, "market_cap": 5e9}


@pytest.fixture
def mixed(rawstore_tmp_db, monkeypatch):
    """GOOD1-2 score; FORN is a foreign filer; GHOST has no data at all; NBAR has a price but no D01 bars, so it
    scores with unknown momentum (decision 3); NOPRC has EDGAR years but no price in any source, so it drops;
    BOOM raises while scoring; ETFX is an excluded type."""
    rawstore.write_rows("edgar_financials", [_edgar("GOOD1"), _edgar("GOOD2"), _edgar("FORN", is_us=False),
                                              _edgar("NOPRC"), _edgar("NBAR"), _edgar("BOOM")])
    seed_momentum(["GOOD1", "GOOD2"])                         # their momentum comes from D01 (#380 S4)
    rows = [_row("GOOD1"), _row("GOOD2"), _row("FORN"), _row("GHOST"), _row("NBAR"), _row("NOPRC", quote=False),
            _row("BOOM"), _row("ETFX")]
    _universe(monkeypatch, rows)
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: {"ETFX"})
    real = engine.score_ticker

    def flaky(ticker, *a, **k):
        if ticker == "BOOM":
            raise AttributeError("boom")
        return real(ticker, *a, **k)
    monkeypatch.setattr(engine, "score_ticker", flaky)
    return engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)


def _steps(result):
    return {s["key"]: s for s in result["funnel_steps"]}


def test_the_funnel_adds_up_and_is_a_valid_shared_funnel(mixed):
    assert funnel.validate(mixed["funnel_steps"]) == []


def test_every_reason_is_counted_and_named(mixed):
    steps = _steps(mixed)
    assert steps["universe"]["count"] == 8 and steps["universe"]["kind"] == "start"
    assert steps["excluded_types"]["count"] == 1 and steps["excluded_types"]["by_design"] is True
    assert steps["foreign"]["count"] == 1 and steps["foreign"]["tickers"] == ["FORN"]
    assert steps["no_annual"]["count"] == 1 and steps["no_annual"]["tickers"] == ["GHOST"]
    assert steps["no_price"]["count"] == 1 and steps["no_price"]["tickers"] == ["NOPRC"]
    assert steps["score_errors"]["count"] == 1 and steps["score_errors"]["tickers"] == ["BOOM"]
    assert steps["scored"]["count"] == 3 and steps["scored"]["kind"] == "result"
    assert [s["key"] for s in mixed["funnel_steps"]][-1] == "scored"


def test_a_price_without_bars_is_not_a_drop_but_an_unknown_momentum(mixed):
    """Decision 3 (#380 S4): only a name with no price in any source drops as ``no_price``."""
    cards = {c["ticker"]: c for c in mixed["cards"]}
    assert "NBAR" in cards and "NOPRC" not in cards
    momentum = next(s for s in cards["NBAR"]["signals"] if s["key"] == "momentum")
    assert momentum["passed"] == 0 and momentum["known"] == 0            # unknown, never a fail
    assert "NBAR" not in (_steps(mixed)["no_price"]["tickers"] or [])


def test_a_reason_with_no_names_has_no_row(mixed):
    assert "fetch_failed" not in _steps(mixed) and "unrecorded" not in _steps(mixed)


def test_the_freeze_guard_inputs_keep_their_meaning(mixed):
    assert mixed["universe_total"] == 8
    assert mixed["summary"]["excluded_types"] == 1
    assert mixed["summary"]["score_errors"] == 1
    assert mixed["summary"]["scanned"] == 3
    assert freeze._score_error_problem(mixed) is not None       # 1 error of 7 attempted is above 1%


def test_names_listed_are_capped_at_the_shared_limit(rawstore_tmp_db, monkeypatch):
    rows = [_row(f"G{i:03d}") for i in range(funnel.MAX_TICKERS + 20)]
    _universe(monkeypatch, rows)
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)
    step = _steps(res)["no_annual"]
    assert step["count"] == funnel.MAX_TICKERS + 20 and len(step["tickers"]) == funnel.MAX_TICKERS
    assert funnel.validate(res["funnel_steps"]) == []


def test_a_name_dropped_by_a_stubbed_scorer_still_balances_the_funnel(rawstore_tmp_db, monkeypatch):
    _universe(monkeypatch, [_row("AAA"), _row("BBB")])
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    monkeypatch.setattr(engine, "enrich_and_score", lambda t, r, nf=False, spine=None, preload=None: None)
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)
    steps = _steps(res)
    assert steps["unrecorded"]["count"] == 2 and funnel.validate(res["funnel_steps"]) == []


def test_an_empty_universe_has_a_valid_funnel(rawstore_tmp_db, monkeypatch):
    _universe(monkeypatch, [])
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)
    assert funnel.validate(res["funnel_steps"]) == []
    assert _steps(res)["scored"]["count"] == 0


# ── the board ───────────────────────────────────────────────────────────────

_THR = {"rev_growth": 0.2, "gross_margin": 0.4, "net_debt_assets": 0.1, "runway": 2.0, "dist_52w": 0.1,
        "vol_ratio": 1.4, "rd_intensity": 0.15, "dilution": 0.2, "accelerating": True, "fcf": True,
        "above_sma50": True}


def _html(result_extra=None, **summary):
    r = {"asof": "2026-08-23T00:00:00", "universe_total": 5000, "universe_generated_at": "2026-08-24T10:00:00+00:00",
         "universe_refreshed_by": "Asymmetric Growth",
         "summary": {"scanned": 3000, "excluded_types": 1000, "pts_9": 0, "pts_8_7": 0, "pts_6_5": 0, "pts_lt5": 3000,
                     "disruptive_themed": 0, "tech_breakthrough": 0, **summary},
         "thresholds": _THR, "cards": []}
    r.update(result_extra or {})
    return render_asymmetric_growth_hunter_html(r)


def _steps_for(**over):
    S = funnel.step
    return [S("universe", "Universe, all market caps", 5000, "start"),
            S("excluded_types", "Excluded security types", 1000, "drop", by_design=True),
            S("no_annual", "No annual financials", 400, "drop", by_design=False, tickers=["AAA", "BBB"]),
            S("foreign", "Non-US filers", 600, "drop", by_design=True),
            S("scored", "Scored", 3000, "result")]


def test_the_header_tile_is_the_scored_count_with_the_screened_count(rawstore_tmp_db):
    page = _html({"funnel_steps": _steps_for()})
    assert "Scanned Universe" not in page
    assert '<div class="v">3000</div><div class="l">Scored</div>' in page
    assert "of 4000 screened" in page and "5000 in universe" in page


def test_the_funnel_rows_name_every_reason_and_list_the_names(rawstore_tmp_db):
    page = _html({"funnel_steps": _steps_for()})
    for label in ("Universe, all market caps", "Excluded security types", "No annual financials", "Non-US filers"):
        assert label in page
    assert "AAA" in page and "BBB" in page


def test_a_run_without_a_funnel_keeps_the_old_tile(rawstore_tmp_db):
    page = _html()
    assert "Scanned Universe" in page and '<div class="v">5000</div>' in page


def test_funnel_text_is_escaped(rawstore_tmp_db):
    steps = _steps_for()
    steps[2]["label"] = "<img src=x onerror=alert(1)>"
    steps[2]["tickers"] = ["<script>alert(2)</script>"]
    page = _html({"funnel_steps": steps})
    assert "<img src=x" not in page and "<script>alert(2)" not in page
    assert "&lt;img src=x" in page


def test_a_provider_error_is_its_own_reason(rawstore_tmp_db, monkeypatch):
    _universe(monkeypatch, [_row("DOWN")])
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())

    def down(*a, **k):
        raise ConnectionError("offline")
    monkeypatch.setattr(engine, "_cached_financials", down)
    res = engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)
    assert _steps(res)["fetch_failed"]["tickers"] == ["DOWN"]
    assert funnel.validate(res["funnel_steps"]) == []


def test_a_funnel_that_does_not_add_up_is_not_shown(rawstore_tmp_db):
    steps = _steps_for()
    steps[-1]["count"] = 2999
    page = _html({"funnel_steps": steps})
    assert "funnel-panel" not in page and "Scanned Universe" in page


# ── #383 C16: the shared funnel panel (byte-identical to the board's old markup) ──

_GOLDEN_STEPS = [
    funnel.step("universe", "Universe", 3, "start"),
    funnel.step("no_annual", "No annual financials", 2, "drop", by_design=False, tickers=["AAA", "BBB"]),
    funnel.step("scored", "Scored", 1, "result"),
]
_GOLDEN_PANEL = (
    '<details class="rules" id="funnel-panel"><summary>Universe to scored: where names drop out</summary>'
    '<div class="rules-body"><table><tr><th>Step</th><th>Names</th><th></th></tr>'
    '<tr><td>Universe</td><td>3</td><td></td></tr>'
    '<tr><td>No annual financials<div class="note">AAA, BBB</div></td><td>&minus;2</td><td>needs a look</td></tr>'
    '<tr><td>Scored</td><td>1</td><td></td></tr></table></div></details>')


def test_the_ag_funnel_panel_is_byte_identical_to_today(rawstore_tmp_db):
    """#383 C16: the shared ``funnel_panel_html`` keeps the exact markup AG rendered before the move."""
    from RevenueChaser.core import funnel as f
    from RevenueChaser.module_asymmetric_growth.report import _universe_tile_and_funnel
    assert f.funnel_panel_html(_GOLDEN_STEPS) == _GOLDEN_PANEL
    _, panel = _universe_tile_and_funnel({"universe_total": 3, "funnel_steps": _GOLDEN_STEPS}, {})
    assert panel == _GOLDEN_PANEL
