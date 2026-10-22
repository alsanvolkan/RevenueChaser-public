"""#386 wave 2.3: the Fundamentals data-health gates (F1), the scoring basis and rule toggles (F2), the card face (F3).

Synthetic rows/metrics and monkeypatched readers; no store, no network (the one persist test uses ``rawstore_tmp_db``).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from RevenueChaser.core import fiscal_year, rawstore
from RevenueChaser.core.gics_map import GicsPath
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_fundamentals import pipeline, scores, screeners
from RevenueChaser.module_fundamentals.pipeline import _quality_metrics, _ttm_metrics, build_payload
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_fundamentals.tests.test_fundamental import _m

AS_OF = date(2026, 10, 16)


# ── F1: the data-health gates ─────────────────────────────────────────────────

def _patch_row(monkeypatch, annual, *, edgar=None):
    """The store-free inputs ``_compute_row`` reads: bars, ticker info, indicators, EDGAR."""
    frame = pd.DataFrame({"Close": [1.0]})
    if edgar is None:
        monkeypatch.setattr(pipeline.sec_edgar, "get_fundamentals",
                            lambda t, no_fetch=False: (annual, [], {}))
    else:
        monkeypatch.setattr(pipeline.sec_edgar, "get_fundamentals", edgar)
    monkeypatch.setattr(pipeline.screen_common, "load_bars", lambda *a, **k: (frame, frame))
    monkeypatch.setattr(pipeline, "load_ticker_info", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "ohlc_drop_reason", lambda df: None)
    monkeypatch.setattr(pipeline, "compute_indicators",
                        lambda df, spy, **k: {"price": 10.0, "sma200": 5.0, "within_52w_high_pct": 0.9})
    return frame


def _annual_pair(rev0=100.0, rev1=130.0, ocf1=160.0, capex1=30.0, *, year0=2023, year1=2024):
    return [{"fiscal_year": year0, "revenue": rev0, "operating_cash_flow": 10.0, "capex": 1.0, "net_income": 5.0},
            {"fiscal_year": year1, "revenue": rev1, "operating_cash_flow": ocf1, "capex": capex1, "net_income": 6.0}]


def _compute(monkeypatch, annual, *, spine=None, edgar=None):
    _patch_row(monkeypatch, annual, edgar=edgar)
    drops: list = []
    row = pipeline._compute_row({"ticker": "T"}, None, spine=spine if spine is not None else {}, drops=drops)
    return row, drops


def test_an_edgar_raise_is_edgar_fetch_failed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("rate limited")
    row, drops = _compute(monkeypatch, [], edgar=boom)
    assert row is None and drops == ["edgar_fetch_failed"]


def test_an_empty_edgar_reply_stays_no_edgar_annual(monkeypatch):
    row, drops = _compute(monkeypatch, [])
    assert row is None and drops == ["no_edgar_annual"]


def test_a_swallowed_edgar_download_failure_is_edgar_fetch_failed(monkeypatch):
    """#386 review: sec_edgar.get_fundamentals swallows a failed download and answers empty, with
    ``fetch_failed`` in its meta; that is not "EDGAR has nothing"."""
    row, drops = _compute(monkeypatch, [], edgar=lambda *a, **k: ([], [], {"available": False, "fetch_failed": True}))
    assert row is None and drops == ["edgar_fetch_failed"]


def test_get_fundamentals_marks_a_failed_download(monkeypatch, rawstore_tmp_db):
    from RevenueChaser.core.providers import sec_edgar
    monkeypatch.setattr(sec_edgar, "_cik_map", lambda: {"ZZZ": {"cik": "1", "name": "Z"}})

    def boom(_cik):
        raise RuntimeError("503")
    monkeypatch.setattr(sec_edgar, "_fetch_facts", boom)
    annual, quarterly, meta = sec_edgar.get_fundamentals("ZZZ")
    assert annual == [] and meta.get("fetch_failed") is True and meta["available"] is False


def test_a_filer_whose_newest_record_is_500_days_old_is_stale(monkeypatch):
    monkeypatch.setattr(pipeline, "_today", lambda: AS_OF)
    end = (AS_OF - timedelta(days=500)).isoformat()
    annual = _annual_pair()
    annual[-1]["period_ends"] = {"revenue": end}
    row, drops = _compute(monkeypatch, annual)
    assert row is None and drops == ["stale_annual"]


def test_a_filer_whose_newest_record_is_400_days_old_is_kept(monkeypatch):
    monkeypatch.setattr(pipeline, "_today", lambda: AS_OF)
    end = (AS_OF - timedelta(days=400)).isoformat()
    annual = _annual_pair()
    annual[-1]["period_ends"] = {"revenue": end}
    row, drops = _compute(monkeypatch, annual)
    assert row is not None and drops == []


def test_exactly_470_days_is_not_stale(monkeypatch):
    """Mutation guard: the rule is ``> STALE_DAYS``, not ``>=``."""
    monkeypatch.setattr(pipeline, "_today", lambda: AS_OF)
    annual = _annual_pair()
    annual[-1]["period_ends"] = {"revenue": (AS_OF - timedelta(days=470)).isoformat()}
    row, drops = _compute(monkeypatch, annual)
    assert row is not None and drops == []


def test_staleness_reads_the_newest_record_not_the_anchor(monkeypatch):
    """Mutation guard: a fresh newest record keeps a name whose revenue anchor is old (the #380 finding)."""
    monkeypatch.setattr(pipeline, "_today", lambda: AS_OF)
    annual = [{"fiscal_year": 2023, "revenue": 1000.0, "operating_cash_flow": 10.0, "capex": 1.0,
               "period_ends": {"revenue": (AS_OF - timedelta(days=600)).isoformat()}},
              {"fiscal_year": 2024, "revenue": None, "gross_profit": 500.0,
               "period_ends": {"revenue": (AS_OF - timedelta(days=100)).isoformat()}}]
    row, drops = _compute(monkeypatch, annual)
    assert row is not None and drops == []


def test_an_excluded_sector_drops_before_any_edgar_read(monkeypatch):
    seen: list = []
    _patch_row(monkeypatch, [], edgar=lambda t, no_fetch=False: seen.append(t) or ([], [], {}))
    spine = {"T": GicsPath("Real Estate", "REITs")}
    row = pipeline._compute_row({"ticker": "T"}, None, spine=spine, drops=(drops := []))
    assert row is None and drops == ["sector_excluded"] and seen == []


def test_a_normal_sector_does_not_drop(monkeypatch):
    spine = {"T": GicsPath("Technology", "Software")}
    row, drops = _compute(monkeypatch, _annual_pair(), spine=spine)
    assert row is not None and "sector_excluded" not in drops


def test_a_rule_of_40_above_the_barrier_is_dropped(monkeypatch):
    # growth 0.30, FCF margin (160 - 2.7) / 130 = 1.21 -> Rule of 40 = 151.0
    annual = _annual_pair(rev0=100.0, rev1=130.0, ocf1=160.0, capex1=2.7)
    row, drops = _compute(monkeypatch, annual)
    assert row is None and drops == ["ro40_barrier"]


def test_a_rule_of_40_under_the_barrier_is_kept(monkeypatch):
    # growth 0.30, FCF margin (160 - 5.3) / 130 = 1.19 -> Rule of 40 = 149.0
    annual = _annual_pair(rev0=100.0, rev1=130.0, ocf1=160.0, capex1=5.3)
    row, drops = _compute(monkeypatch, annual)
    assert row is not None and drops == []


def test_the_barrier_is_strictly_above_150(monkeypatch):
    # growth 0.30, FCF margin exactly 1.20 -> Rule of 40 = 150.0
    annual = _annual_pair(rev0=100.0, rev1=130.0, ocf1=156.0, capex1=0.0)
    row, _drops = _compute(monkeypatch, annual)
    assert row is not None


def test_the_scored_row_carries_the_anchor_period_end(monkeypatch):
    monkeypatch.setattr(pipeline, "_today", lambda: AS_OF)
    annual = _annual_pair()
    annual[-1]["period_ends"] = {"revenue": (AS_OF - timedelta(days=60)).isoformat()}
    row, _drops = _compute(monkeypatch, annual)
    assert row["anchor_period_end"] == (AS_OF - timedelta(days=60)).isoformat()


def _score_result(**kw):
    base = {"ticker": "A", "period": "FY2024 vs FY2023", "anchor_period_end": "2024-12-31",
            "price": 12.5, "market_cap": 2.0e9,
            "metrics": _m(ro40=50.0, ro40_basis="FY", rev_growth_ttm=0.30, fcf_margin=0.20)}
    base.update(kw)
    return base


def test_score_rows_carry_the_four_new_keys():
    r = scores.score_rows([_score_result()])[0]
    assert r["ro40_basis"] == "FY" and r["anchor_period_end"] == "2024-12-31"
    assert r["price"] == 12.5 and r["market_cap"] == 2.0e9


def test_persist_writes_the_four_new_columns(rawstore_tmp_db):
    scores.persist([_score_result()])
    row = rawstore.query("SELECT * FROM fundamentals_scores")[0]
    assert row["ro40_basis"] == "FY" and row["anchor_period_end"] == "2024-12-31"
    assert row["price"] == 12.5 and row["market_cap"] == 2.0e9


# ── F2: Quality on the anchor and the EBITDA fallback ─────────────────────────

def test_quality_reads_the_anchor_not_the_newest_record():
    annual = [{"fiscal_year": 2023, "revenue": 1000.0, "gross_profit": 400.0, "total_equity": 500.0},
              {"fiscal_year": 2024, "revenue": None, "gross_profit": 999.0, "total_equity": 900.0}]
    assert fiscal_year.anchor(annual) == 0
    assert _quality_metrics(annual)["gross_margin"] == pytest.approx(0.40)
    assert _quality_metrics(annual)["total_equity"] == 500.0


def test_a_missing_capex_falls_back_to_the_ebitda_margin():
    annual = [{"fiscal_year": 2023, "revenue": 1000.0, "operating_cash_flow": 100.0, "capex": 10.0,
               "ebit": 150.0, "depreciation": 20.0},
              {"fiscal_year": 2024, "revenue": 1100.0, "operating_cash_flow": 120.0,
               "ebit": 180.0, "depreciation": 25.0}]
    t = _ttm_metrics([], annual)
    assert t["ro40_basis"] == "FY EBITDA"
    assert t["fcf_margin"] == pytest.approx(205.0 / 1100.0)
    assert t["ro40"] == pytest.approx((0.10 + 205.0 / 1100.0) * 100.0)
    g = screeners.evaluate_rule_of_40(t)
    assert g["rules"][0]["passed"] is None      # shown, never decides
    assert g["passed"] is False                 # the gate cannot pass on an unknown rule


def test_an_ebitda_fallback_without_depreciation_uses_ebit():
    annual = [{"fiscal_year": 2023, "revenue": 1000.0, "operating_cash_flow": 100.0, "capex": 10.0},
              {"fiscal_year": 2024, "revenue": 1100.0, "ebit": 220.0}]
    t = _ttm_metrics([], annual)
    assert t["ro40_basis"] == "FY EBITDA" and t["fcf_margin"] == pytest.approx(0.20)


def test_a_missing_operating_cash_flow_also_falls_back_to_the_ebitda_margin():
    """Review (#386, the #246 dry-run property): a capex figure alone gives no free-cash-flow margin, so a year with
    capex but no operating cash flow reads the EBITDA margin too, and a capex backfill never loses the shown value."""
    annual = [{"fiscal_year": 2023, "revenue": 1000.0, "operating_cash_flow": 100.0, "capex": 10.0},
              {"fiscal_year": 2024, "revenue": 1100.0, "capex": 30.0, "ebit": 220.0}]
    t = _ttm_metrics([], annual)
    assert t["ro40_basis"] == "FY EBITDA" and t["fcf_margin"] == pytest.approx(0.20)
    assert t["ro40"] is not None


def _row(ticker, metrics, **kw):
    row = {"ticker": ticker, "name": ticker, "price": 50.0, "market_cap": 1e9, "sector": "Technology",
           "industry": "Software", "themes": [], "earnings_date": None, "period": "FY2024 vs FY2023",
           "metrics": metrics}
    row.update(kw)
    return row


_RO40 = dict(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0, ttm_revenue=2e9)
_T3 = dict(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=45.0, ttm_revenue=2e9,
           roic=0.20, gross_margin=0.40, net_debt_ebitda=1.0,
           dist_52w_pct=0.95, rs_6m=0.1, above_200d=True)
_C2 = dict(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=90.0, ttm_revenue=2e9,
           roic=0.20, gross_margin=0.40, net_debt_ebitda=1.0)
_D2 = dict(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=95.0, ttm_revenue=5e6,   # a higher Ro40 than C2: only DISTORTED puts it last
           roic=0.20, gross_margin=0.40, net_debt_ebitda=1.0)


def test_the_sort_is_count_then_distorted_then_rule_of_40():
    p = build_payload([_row("D2", _m(**_D2)), _row("C2", _m(**_C2)), _row("T3", _m(**_T3))], top_n=10)
    assert [c["ticker"] for c in p["candidates"]] == ["T3", "C2", "D2"]
    assert [c.get("distorted") for c in p["candidates"]] == [False, False, True]


def test_a_none_rule_of_40_sorts_last_within_a_count():
    a = _row("A", _m(ro40=None, rev_growth_ttm=None, fcf_margin=None, roic=0.20, gross_margin=0.40,
                     net_debt_ebitda=1.0))
    b = _row("B", _m(ro40=None, rev_growth_ttm=None, fcf_margin=None, roic=0.20, gross_margin=0.40,
                     net_debt_ebitda=1.0, dist_52w_pct=0.95, rs_6m=0.1, above_200d=True))
    p = build_payload([a, b], top_n=10)
    assert [c["ticker"] for c in p["candidates"]] == ["B", "A"]


def test_the_bucket_is_the_rules_passed():
    p = build_payload([_row("A", _m(**_RO40, dist_52w_pct=0.95, rs_6m=0.1, above_200d=True))], top_n=10)
    c = p["candidates"][0]
    assert c["rules_passed"] == ["rule_of_40", "momentum"]
    assert c["bucket"] == "rule_of_40+momentum" and c["passed_count"] == 2


# ── F2.8/F2.9: the rule toggles and the old-payload notice ────────────────────

def _known_payload():
    return build_payload([_row("AAA", _m(**_T3), price=50.0, market_cap=1e9)], top_n=10)


def test_the_page_has_the_three_rule_toggles_with_the_rule_of_40_checked():
    html = render_fundamental_dashboard(_known_payload())
    assert 'id="rule-rule_of_40" checked' in html
    assert 'id="rule-quality"' in html and 'id="rule-momentum"' in html
    for gone in ("Triple Pass", "Dual Pass", 'class="tabs"', "data-pt", "setPt("):
        assert gone not in html, gone


def test_an_old_payload_renders_no_toggles_and_the_notice():
    p = _known_payload()
    del p["selection_version"]
    html = render_fundamental_dashboard(p)
    assert "Scored before v6 (data-health gates and the rule toggles). Run a Fetch to rescore." in html
    assert 'id="rule-rule_of_40"' not in html
    assert 'class="toggles"' not in html


def test_an_old_payload_with_candidates_that_lack_the_new_keys_does_not_crash():
    row = {"ticker": "OLD", "name": "Old", "price": 10.0, "market_cap": 1e9, "sector": "Technology",
           "industry": "Software", "themes": [], "period": "FY2023", "gates": {}}
    payload = {"candidates": [row], "passing": {}, "summary": {}, "universe_total": 1, "is_fixture": False}
    html = render_fundamental_dashboard(payload)
    assert "Scored before v6" in html and "Fundamental Analysis" in html


def test_a_hostile_ticker_is_escaped_on_the_card(tmp_path):
    evil = "<img src=x onerror=alert(1)>"
    p = _known_payload()
    p["candidates"][0]["ticker"] = evil
    out = run_board_js(render_fundamental_dashboard(p),
                       "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)
    assert "<img" not in out and "&lt;img" in out


# ── F3: the card face ─────────────────────────────────────────────────────────

def test_the_card_face_carries_the_period_basis_growth_margin_price_and_cap(tmp_path):
    p = _known_payload()
    out = run_board_js(render_fundamental_dashboard(p),
                       "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)
    assert "FY2024 vs FY2023" in out          # the period
    assert "Basis" in out and "FY" in out      # the Rule-of-40 basis
    assert "30.0%" in out and "20.0%" in out   # growth / FCF margin
    assert "$50" in out and "$1.00B" in out    # price / market cap


def test_the_page_has_none_of_the_retired_words():
    html = render_fundamental_dashboard(_known_payload())
    for gone in ("LONG", "setups", "broader universe"):
        assert gone not in html, gone


def test_the_summary_states_the_new_ranking():
    html = render_fundamental_dashboard(_known_payload())
    assert "ranked by rules passed, then Rule of 40" in html
    assert "gate tier" not in html


def test_the_summary_names_the_health_drops_and_keeps_them_out_of_data_short():
    """#386 review: run_screen counts every unscored name as data_short; the four F1 drops are named, not "data<252d"."""
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    stats = {"regime": "RISK-ON", "data_short": 6, "no_edgar": 1,
             "dropped": {"short_bars": 1, "no_edgar_annual": 1, "sector_excluded": 2, "stale_annual": 1,
                         "edgar_fetch_failed": 1, "ro40_barrier": 1}}
    p = build_payload([_row("T3", _m(**_T3))], top_n=10, run_stats=stats)
    s = p["summary"]
    assert s["data_short"] == 1
    assert s["health_drops"] == {"sector_excluded": 2, "edgar_fetch_failed": 1, "stale_annual": 1, "ro40_barrier": 1}
    html = render_fundamental_dashboard(p)
    assert "data&lt;252d 1" in html
    for text in ("Financial Services / Real Estate 2", "EDGAR read failed 1", "stale annual report 1",
                 "Rule of 40 above 150 1"):
        assert text in html, text
