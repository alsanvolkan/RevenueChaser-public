"""#148 (27-4): tier count, Rule of 40 rows, data_as_of, no-EDGAR count and the TTM contiguity guard.

Synthetic quarters and metrics, no store, no network.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import screen_common as sc
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_fundamentals import scores, screeners
from RevenueChaser.module_fundamentals.pipeline import _period_label, _ttm_metrics, build_payload

ENDS = ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
        "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]


def _q(period: str, rev: float) -> dict:
    return {"period": period, "revenue": rev, "operating_cash_flow": 50.0, "capex": 10.0, "net_income": 20.0}


def _quarters(revs, ends=ENDS[4:]):
    return [_q(e, r) for e, r in zip(ends, revs)]


_ANNUAL = [{"fiscal_year": "2023", "revenue": 1000.0}, {"fiscal_year": "2024", "revenue": 1100.0,
           "operating_cash_flow": 200.0, "capex": 50.0, "net_income": 90.0}]


def _m(**kw) -> dict:
    base = {"rev_growth_ttm": None, "fcf_margin": None, "ro40": None, "ttm_revenue": None,
            "gross_margin": None, "roic": None, "net_debt_ebitda": None,
            "dist_52w_pct": None, "rs_6m": None, "above_200d": None, "pe": None, "pb": None}
    base.update(kw)
    return base


def _row(ticker, metrics):
    return {"ticker": ticker, "name": "T", "price": 50.0, "market_cap": 1e9, "sector": "Technology",
            "industry": "Software", "themes": [], "earnings_date": None, "period": "x", "metrics": metrics}


# ── U11: the rules a candidate passed (#386 F2 replaced the 3x/2x/1x tier) ────

_NO_RO40 = dict(rev_growth_ttm=0.10, fcf_margin=0.05, ro40=15.0)
_RO40 = dict(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0)
_QUAL = dict(roic=0.20, gross_margin=0.40, net_debt_ebitda=1.0)
_MOM = dict(dist_52w_pct=0.95, rs_6m=0.1, above_200d=True)


def test_a_quality_only_name_carries_only_quality():
    p = build_payload([_row("Q", _m(**_NO_RO40, **_QUAL))], top_n=10)
    c = p["candidates"][0]
    assert c["rules_passed"] == ["quality"] and c["passed_count"] == 1 and c["bucket"] == "quality"


def test_a_momentum_only_name_carries_only_momentum():
    p = build_payload([_row("M", _m(**_NO_RO40, **_MOM))], top_n=10)
    assert p["candidates"][0]["rules_passed"] == ["momentum"]


def test_a_rule_of_40_only_name_carries_only_the_rule_of_40():
    p = build_payload([_row("R", _m(**_RO40))], top_n=10)
    assert p["candidates"][0]["rules_passed"] == ["rule_of_40"]
    assert p["candidates"][0]["bucket"] == "rule_of_40"


def test_the_passing_counts_add_up_to_the_candidates_rules():
    rows = [_row("R", _m(**_RO40)), _row("Q", _m(**_NO_RO40, **_QUAL)),
            _row("D", _m(**_RO40, **_QUAL)), _row("T", _m(**_RO40, **_QUAL, **_MOM))]
    p = build_payload(rows, top_n=10)
    assert len(p["candidates"]) == 4
    assert p["passing"] == {"rule_of_40": 3, "quality": 3, "momentum": 1}


def test_the_payload_has_no_multiplier_or_tier_counters():
    p = build_payload([_row("R", _m(**_RO40))], top_n=10)
    assert "tiers" not in p and "multiplier" not in p["candidates"][0]
    assert p["selection_version"] == fp.config.SELECTION_VERSION


# ── U12: the Rule of 40 gate is one real test ─────────────────────────────────

def test_rule_of_40_has_one_rule_row_with_a_real_pass_or_fail():
    g = screeners.evaluate_rule_of_40(_m(**_RO40))
    assert [r["id"] for r in g["rules"]] == ["ro40"]
    assert g["rules"][0]["passed"] is True and g["rules"][0]["actual"] == 50.0
    assert g["passed"] is True and g["score"] == "50" and (g["known"], g["total"]) == (1, 1)


def test_a_rule_of_40_below_the_line_fails_its_row():
    g = screeners.evaluate_rule_of_40(_m(**_NO_RO40))
    assert g["rules"][0]["passed"] is False and g["passed"] is False


def test_the_line_itself_passes():
    g = screeners.evaluate_rule_of_40(_m(rev_growth_ttm=0.25, fcf_margin=0.15, ro40=40.0))
    assert g["rules"][0]["passed"] is True


def test_a_missing_value_is_unknown_not_failed():
    g = screeners.evaluate_rule_of_40(_m(rev_growth_ttm=None, fcf_margin=0.2, ro40=None))
    assert g["rules"][0]["passed"] is None and g["rules"][0]["actual"] is None
    assert g["passed"] is False and g["score"] == "N/A" and (g["known"], g["total"]) == (0, 1)


def test_gate_rows_store_the_real_verdict_or_null():
    rows = scores.gate_rows([{"ticker": "A", "metrics": _m(**_NO_RO40, **_QUAL, **_MOM)},
                             {"ticker": "B", "metrics": _m(**_RO40, **_QUAL, **_MOM)},
                             {"ticker": "C", "metrics": _m(**_QUAL, **_MOM)}])
    ro = {r["ticker"]: r["passed"] for r in rows if r["rule_id"] == "ro40"}
    assert ro == {"A": 0, "B": 1, "C": None}
    assert len([r for r in rows if r["ticker"] == "A"]) == 7


# ── U19: data_as_of and the no-EDGAR count ────────────────────────────────────

def test_the_payload_carries_the_data_as_of_of_the_run():
    p = build_payload([_row("R", _m(**_RO40))], top_n=10, run_stats={"data_as_of": "2026-10-06"})
    assert p["data_as_of"] == "2026-10-06"
    assert build_payload([], top_n=10)["data_as_of"] is None


def test_run_scan_leaves_the_data_date_to_the_shared_scan(monkeypatch):
    """Since K7 (#383 R3) ``run_screen`` dates the payload by the D01 bars of the names the Fetch scored
    (``core/tests/test_provenance_k7_383.py``); the board's own ``extra_stats`` read the score table, which still
    held the previous run's names."""
    seen = {}
    monkeypatch.setattr(fp.screen_common, "run_screen", lambda **kw: seen.update(kw) or {})
    fp.run_scan(no_fetch=True)
    assert seen and seen.get("extra_stats") is None


def test_a_name_without_edgar_data_is_not_counted_as_short_history(monkeypatch):
    from RevenueChaser.core.tests.test_screen_common_147 import Board
    b = Board(monkeypatch)
    b.behaviour = {"B": None, "C": None, "D": None}
    b.reasons = {"B": "no_edgar_annual", "C": "no_edgar_annual", "D": "short_bars"}
    b.run()
    assert b.stats["no_edgar"] == 2
    assert b.stats["data_short"] == 1
    assert b.stats["dropped"] == {"no_edgar_annual": 2, "short_bars": 1}


def test_the_summary_and_the_board_show_the_no_edgar_count():
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    p = build_payload([_row("R", _m(**_RO40))], top_n=10, run_stats={"data_short": 3, "no_edgar": 7})
    assert p["summary"]["no_edgar"] == 7 and p["summary"]["data_short"] == 3
    html = render_fundamental_dashboard(p)
    assert "no EDGAR 7" in html and "data&lt;252d 3" in html


# ── U3 guard: TTM only from contiguous quarters ───────────────────────────────

def test_four_contiguous_quarters_give_ttm_revenue_but_no_rule_of_40_without_a_ytd_point():
    # D2 B (2026-10-08): TTM revenue keeps the "4 consecutive reported quarters" rule; the Rule of 40 moved to the
    # fiscal year + YTD point (test_fundamentals_ttm_consumer_148.py), so quarters alone never make it.
    q = _quarters([100, 100, 100, 100, 200, 200, 200, 200])
    ttm = _ttm_metrics(q, [])
    assert ttm["ttm_revenue"] == pytest.approx(800.0)
    assert ttm["rev_growth_ttm"] is None and ttm["fcf_margin"] is None and ttm["ro40"] is None
    assert _period_label(q, []) == "latest"


def test_year_ends_with_no_fourth_quarter_do_not_make_a_ttm():
    # Q1-Q3 of three years (what a 10-Q only run stores): the last four are Q3/24, Q1/25, Q2/25, Q3/25.
    ends = ["2023-03-31", "2023-06-30", "2023-09-30", "2024-03-31", "2024-06-30", "2024-09-30",
            "2025-03-31", "2025-06-30", "2025-09-30"]
    q = _quarters([100] * 9, ends)
    ttm = _ttm_metrics(q, _ANNUAL)
    assert ttm["rev_growth_ttm"] == pytest.approx(0.10)       # the fiscal-year figure, not a quarter sum
    assert ttm["ttm_revenue"] == 1100.0                         # the fiscal year's revenue
    assert ttm["fcf_margin"] == pytest.approx(150.0 / 1100.0)   # the annual cash flow
    assert _period_label(q, _ANNUAL) == "FY2024 vs FY2023"


def test_four_contiguous_quarters_give_a_ttm_but_growth_comes_from_the_years():
    ends = ["2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30"]
    ttm = _ttm_metrics(_quarters([100, 110, 120, 130], ends), _ANNUAL)
    assert ttm["ttm_revenue"] == pytest.approx(460.0)
    assert ttm["rev_growth_ttm"] == pytest.approx(0.10)
    assert ttm["fcf_margin"] == pytest.approx(150.0 / 1100.0)   # one basis: growth and margin both from the years
    assert ttm["ro40_basis"] == "FY"
    assert _period_label(_quarters([100] * 4, ends), _ANNUAL) == "FY2024 vs FY2023"


def test_a_missing_quarter_inside_the_window_breaks_the_run():
    q = _quarters([100, 100, 100, 100, 200, 200, 200, 200])
    del q[1]                                                    # 2024-06-30 gone: a hole below the newest four
    q.insert(0, _q("2023-12-31", 100))
    ttm = _ttm_metrics(q, _ANNUAL)
    assert ttm["rev_growth_ttm"] == pytest.approx(0.10)         # 8 records but not 8 consecutive quarters
    assert ttm["ttm_revenue"] == pytest.approx(800.0)           # the newest four are consecutive
    assert _period_label(q, _ANNUAL) == "FY2024 vs FY2023"


def test_cash_flow_quarters_must_end_where_the_revenue_quarters_end():
    q = _quarters([100, 100, 100, 100, 200, 200, 200, 200])
    q[-1]["operating_cash_flow"] = None                         # the newest cash-flow quarter is missing
    ttm = _ttm_metrics(q, _ANNUAL)
    assert ttm["fcf_margin"] == pytest.approx(150.0 / 1100.0)   # annual, not three quarters plus an older one


def test_an_unparseable_period_is_no_ttm():
    q = _quarters([100] * 8)
    q[3]["period"] = "2024-012-30"
    assert _ttm_metrics(q, _ANNUAL)["rev_growth_ttm"] == pytest.approx(0.10)
    assert _period_label(q, _ANNUAL) == "FY2024 vs FY2023"


def test_the_days_between_quarters_are_bounded_both_ways():
    near = _quarters([100] * 4, ["2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30"])   # monthly, not quarterly
    far = _quarters([100] * 4, ["2024-03-31", "2024-12-31", "2025-06-30", "2025-12-31"])
    for q in (near, far):
        assert _ttm_metrics(q, [])["ttm_revenue"] is None


def test_the_rule_of_40_modal_shows_one_row_with_the_value_in_points(tmp_path):
    from RevenueChaser.core.tests.board_js import run_board_js
    from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
    p = build_payload([_row("AAA", _m(rev_growth_ttm=0.30, fcf_margin=0.1234567, ro40=42.3456789))], top_n=10)
    out = run_board_js(render_fundamental_dashboard(p), """
openGate({getAttribute: function(k){ return k === "data-ticker" ? "AAA" : "rule_of_40"; }});
console.log(JSON.stringify({rules: _els['m-rules'].innerHTML}));
""", tmp_path)
    assert out["rules"].count('class="rule"') == 1 and "m-ok" in out["rules"]
    assert ">42.3<" in out["rules"] and "42.3456" not in out["rules"]


# ── review M3: the fiscal-year basis reads one anchor year and its adjacent prior year ──

def _fyrec(year, rev=None, **kw):
    return {"fiscal_year": str(year), "revenue": rev, "operating_cash_flow": 200.0, "capex": 50.0,
            "net_income": 90.0, **kw}


def test_years_two_apart_give_no_growth_and_no_rule_of_40():
    t = _ttm_metrics([], [_fyrec(2022, 1000.0), _fyrec(2024, 1500.0)])
    assert t["rev_growth_ttm"] is None and t["ro40"] is None
    assert _period_label([], [_fyrec(2022, 1000.0), _fyrec(2024, 1500.0)]) == "FY2024"


def test_a_newest_record_without_revenue_is_not_the_anchor():
    annual = [_fyrec(2022, 900.0), _fyrec(2023, 1000.0), _fyrec(2024, None)]
    t = _ttm_metrics([], annual)
    assert t["rev_growth_ttm"] == pytest.approx(1000.0 / 900.0 - 1)       # FY2023 vs FY2022
    assert t["ttm_revenue"] == 1000.0
    assert _period_label([], annual) == "FY2023 vs FY2022"


def test_the_cash_flow_margin_comes_from_the_anchor_year():
    annual = [_fyrec(2023, 1000.0, operating_cash_flow=100.0, capex=0.0),
              _fyrec(2024, 1100.0, operating_cash_flow=330.0, capex=0.0), _fyrec(2025, None)]
    assert _ttm_metrics([], annual)["fcf_margin"] == pytest.approx(0.30)


def test_a_year_record_without_a_fiscal_year_falls_back_to_the_previous_position():
    annual = [{"revenue": 100.0}, {"revenue": 120.0}]
    assert _ttm_metrics([], annual)["rev_growth_ttm"] == pytest.approx(0.20)


def test_quarters_without_a_cash_flow_put_the_whole_rule_of_40_on_the_year_basis():
    q = _quarters([100, 100, 100, 100, 200, 200, 200, 200])
    for rec in q:
        rec["operating_cash_flow"] = None
    t = _ttm_metrics(q, _ANNUAL)
    assert t["ro40_basis"] == "FY" and t["rev_growth_ttm"] == pytest.approx(0.10)
    assert _period_label(q, _ANNUAL) == "FY2024 vs FY2023"


def test_quarters_alone_never_make_a_ttm_basis():
    t = _ttm_metrics(_quarters([100, 100, 100, 100, 200, 200, 200, 200]), _ANNUAL)
    assert t["ro40_basis"] == "FY"
    assert _ttm_metrics([], _ANNUAL)["ro40_basis"] == "FY"
    assert _ttm_metrics([], [])["ro40_basis"] is None


def test_the_rule_row_names_the_basis_it_was_computed_on():
    for basis, label in (("TTM", "Revenue growth + FCF margin (TTM)"), ("FY", "Revenue growth + FCF margin (FY)"),
                         (None, "Revenue growth + FCF margin")):
        g = screeners.evaluate_rule_of_40(_m(**_RO40, ro40_basis=basis))
        assert g["rules"][0]["label"] == label


def test_the_basis_reaches_the_metrics_of_a_scored_row():
    import inspect
    assert '"ro40_basis": ttm["ro40_basis"]' in inspect.getsource(fp._compute_row)


def test_a_zero_revenue_ttm_is_not_a_ttm_basis_and_does_not_divide_by_zero():
    t = _ttm_metrics(_quarters([100, 100, 100, 100, 0, 0, 0, 0]), _ANNUAL)
    assert t["ro40_basis"] == "FY" and t["rev_growth_ttm"] == pytest.approx(0.10)


def _with_derived(q, **marks):
    for i, d in marks.items():
        q[int(i[1:])]["derived"] = d
    return q


def test_a_derived_quarter_keeps_the_rule_of_40_on_the_year_basis():
    rev = [100, 100, 100, 100, 200, 200, 200, 200]
    for derived in (["revenue"], ["2025-12-31"]):                      # the derived field, or the derived period
        q = _with_derived(_quarters(rev), q7=derived)
        t = _ttm_metrics(q, _ANNUAL)
        assert t["ro40_basis"] == "FY" and t["rev_growth_ttm"] == pytest.approx(0.10)
        assert _period_label(q, _ANNUAL) == "FY2024 vs FY2023"


def test_a_derived_cash_flow_quarter_also_keeps_the_year_basis():
    q = _with_derived(_quarters([100, 100, 100, 100, 200, 200, 200, 200]), q7=["operating_cash_flow"])
    assert _ttm_metrics(q, _ANNUAL)["ro40_basis"] == "FY"


def test_a_quarter_with_no_or_unrelated_derived_marks_changes_nothing():
    rev = [100, 100, 100, 100, 200, 200, 200, 200]
    base = _ttm_metrics(_quarters(rev), _ANNUAL)
    for derived in (None, [], ["2020-01-01"], ["gross_profit"]):
        q = _with_derived(_quarters(rev), q7=derived)
        assert _ttm_metrics(q, _ANNUAL) == base


@pytest.mark.parametrize("ro40, shown, passes", [
    (39.6, "39.6", False),     # DF86: used to show "40" on a failing card
    (39.96, "39.9", False),    # rounding to one decimal must not reach the line either
    (40.0, "40", True),
    (50.0, "50", True),
])
def test_ro40_score_text_never_shows_the_line_for_a_failing_value(ro40, shown, passes):
    g = screeners.evaluate_rule_of_40(_m(**{**_RO40, "ro40": ro40}))
    assert g["score"] == shown and g["passed"] is passes
