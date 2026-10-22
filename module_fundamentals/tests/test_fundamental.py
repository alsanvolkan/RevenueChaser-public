"""Unit tests for the Fundamental Analysis module (pure functions, no network)."""
from __future__ import annotations

import pytest

from RevenueChaser.module_fundamentals import screeners
from RevenueChaser.module_fundamentals.pipeline import (
    _quality_metrics,
    _ttm_metrics,
    build_payload,
)
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard


def _m(**kw) -> dict:
    base = {
        "rev_growth_ttm": None, "fcf_margin": None, "ro40": None, "ttm_revenue": None,
        "gross_margin": None, "roic": None, "net_debt_ebitda": None,
        "dist_52w_pct": None, "rs_6m": None, "above_200d": None,
        "pe": None, "pb": None,
    }
    base.update(kw)
    return base


# ── screeners ──
def test_rule_of_40_pass():
    g = screeners.evaluate_rule_of_40(_m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0))
    assert g["passed"] is True
    assert g["score"] == "50"
    assert len(g["rules"]) == 1  # one test: the sum (#148 U12)


def test_rule_of_40_fail():
    g = screeners.evaluate_rule_of_40(_m(rev_growth_ttm=0.10, fcf_margin=0.10, ro40=20.0))
    assert g["passed"] is False


def test_quality_full_pass():
    g = screeners.evaluate_quality(_m(rev_growth_ttm=0.25, roic=0.10, gross_margin=0.35, net_debt_ebitda=2.0))
    assert g["passed"] is True
    assert g["score"] == "3/3"


def test_quality_fail():
    g = screeners.evaluate_quality(_m(rev_growth_ttm=0.10, roic=0.10, gross_margin=0.20, net_debt_ebitda=4.0))
    assert g["passed"] is False
    assert g["score"] == "0/3"


def test_momentum_pass():
    g = screeners.evaluate_momentum(_m(dist_52w_pct=0.90, rs_6m=0.05, above_200d=True))
    assert g["passed"] is True
    assert g["score"] == "3/3"


def test_momentum_fail():
    g = screeners.evaluate_momentum(_m(dist_52w_pct=0.60, rs_6m=-0.05, above_200d=False))
    assert g["passed"] is False


# ── pipeline metrics helpers ──
def _quarters():
    revs = [100, 100, 100, 100, 200, 200, 200, 200]
    ends = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
            "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
    return [{"period": e, "revenue": r, "operating_cash_flow": 50.0, "capex": 10.0, "net_income": 20.0}
            for e, r in zip(ends, revs)]


def test_ttm_metrics():
    ttm = _ttm_metrics(_quarters(), [])
    assert ttm["ttm_revenue"] == pytest.approx(800.0)
    assert ttm["rev_growth_ttm"] is None and ttm["ro40"] is None   # the Rule of 40 needs a YTD point (#148)


def test_quality_metrics():
    annual = [{"revenue": 1000.0, "gross_profit": 400.0, "ebit": 200.0,
               "depreciation": 50.0, "total_debt": 300.0, "cash": 100.0,
               "total_equity": 500.0, "net_income": 150.0}]
    q = _quality_metrics(annual)
    assert q["gross_margin"] == pytest.approx(0.40)
    assert q["net_debt_ebitda"] == pytest.approx(0.80)
    assert q["roic"] == pytest.approx(200 * 0.79 / 700.0, abs=0.01)
    assert q["total_equity"] == 500.0


# ── build_payload + render ──
def _row(ticker, metrics):
    return {"ticker": ticker, "name": "Test", "price": 50.0, "market_cap": 1e9,
            "sector": "Technology", "industry": "Software", "themes": ["Software"],
            "earnings_date": None, "period": "Q2/2026 vs Q2/2025", "metrics": metrics}


def test_build_payload_shape():
    rows = [
        _row("AAA", _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0,
                       gross_margin=0.35, roic=0.20, net_debt_ebitda=2.0,
                       dist_52w_pct=0.90, rs_6m=0.05, above_200d=True,
                       pe=15.0, pb=2.0)),
        _row("BBB", _m(rev_growth_ttm=0.10, fcf_margin=0.10, ro40=20.0,
                       gross_margin=0.20, net_debt_ebitda=4.0,
                       dist_52w_pct=0.60, rs_6m=-0.05, above_200d=False)),
    ]
    p = build_payload(rows, top_n=10)
    assert p["universe_total"] == 2
    assert len(p["candidates"]) == 1
    c = p["candidates"][0]
    assert c["rules_passed"] == ["rule_of_40", "quality", "momentum"]      # AAA passes all 3 (#386 F2)
    assert c["passed_count"] == 3 and c["bucket"] == "rule_of_40+quality+momentum"
    assert p["passing"] == {"rule_of_40": 1, "quality": 1, "momentum": 1}
    assert "multiplier" not in c and "tiers" not in p
    assert c["pe"] == 15.0
    assert set(c["gates"].keys()) == {"rule_of_40", "quality", "momentum"}
    assert (c["sector"], c["industry"]) == ("Technology", "Software")
    assert "industry_group" not in c and "sub_industry" not in c


def test_build_payload_ro40_only():
    rows = [_row("AAA", _m(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0,
                           gross_margin=0.20, net_debt_ebitda=4.0,
                           dist_52w_pct=0.60, rs_6m=-0.05, above_200d=False))]
    p = build_payload(rows, top_n=10)
    assert p["candidates"][0]["rules_passed"] == ["rule_of_40"]
    assert p["candidates"][0]["passed_count"] == 1 and p["candidates"][0]["bucket"] == "rule_of_40"


def test_render_dashboard():
    p = build_payload([], run_stats={})
    html = render_fundamental_dashboard(p)
    assert "Fundamental Analysis" in html


def test_render_uses_the_shared_filter_kit():
    """#383 C1c/#386 F2: the universal filters are the rc_filters bar plus the rule toggles, not the old
    Sector cascade / MC / EWEEK / INDEX controls nor the removed gate tabs."""
    html = render_fundamental_dashboard(build_payload([], run_stats={}))
    for frag in ('id="rc-filters"', "RCFilters.mount", 'id="rule-rule_of_40"', 'class="toggles"', "setRule("):
        assert frag in html, f"missing shared filter fragment: {frag}"
    for gone in ("sectorOk(", "mcRange(", 'id="mc-tabs"', 'id="eweek-tabs"', 'id="index-tabs"',
                 'id="theme"', 'id="sector"', 'id="industry"',
                 'id="industry-group"', 'id="sub-industry"', "IGROUP", "ISUB",
                 "industry_group", "sub_industry",
                 'class="tabs"', 'data-pt'):
        assert gone not in html, f"the old filter markup is gone, found: {gone}"


def test_render_dashboard_universe_provenance():
    p = build_payload([], run_stats={})
    p["universe_refreshed_by"] = "Fundamentals"
    p["universe_generated_at"] = "2026-08-24T10:00:00+00:00"
    html = render_fundamental_dashboard(p)
    assert "Universe refreshed by" in html
    assert "Fundamentals" in html
    assert "Flow not scanned" in html
