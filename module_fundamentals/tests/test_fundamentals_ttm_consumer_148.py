"""#148 consumer commit: the Rule of 40 on a TTM of fiscal year + this year's YTD - last year's YTD.

Records come from the real extractor (``sec_edgar.extract_annual`` / ``extract_quarterly``) through synthetic filings
(``ttm_filings_148``), so the reader is tested against what the extractor really writes (``ytd``,
``period_starts``). Decisions (avo, card taps 2026-10-08 16:16Z): D1 A (a negative piece -> FY, "(FY)"),
D2 B (TTM only for Ro40 and Quality's growth; ``ttm_revenue`` and ``ttm_net_income`` keep the "4 reported
quarters, else the year" rule), D3 A (growth = TTM vs the TTM one year earlier). No store, no network.
"""
from __future__ import annotations

import random

import pytest

from RevenueChaser.core.providers import sec_edgar as se
from RevenueChaser.module_fundamentals import config
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_fundamentals.tests import ttm_filings_148 as ft


def _run(facts):
    a, q = se.extract_annual(facts), se.extract_quarterly(facts)
    return fp._ttm_metrics(q, a), fp._period_label(q, a), q, a


def _case(prefix):
    (name,) = [k for k in ft.CASES if k.startswith(prefix + " ")]
    return ft.CASES[name]


# ── the cases C1-C8 (spec section 8; numbers = ``scope="ro40"`` of the prototype run on main cfc3b89) ──

def test_c1_a_december_filer_after_its_q2_10q_is_on_ttm():
    t, label, _q, _a = _run(_case("C1"))
    assert t["ro40_basis"] == "TTM"
    assert t["rev_growth_ttm"] == pytest.approx(0.10) and t["fcf_margin"] == pytest.approx(0.25)
    assert t["ro40"] == pytest.approx(35.0)
    assert label == "Q2/2026 vs Q2/2025"


def test_c1_ttm_revenue_and_net_income_stay_on_todays_rule():
    # D2 B: the derived quarters of this extractor never form a 4-quarter run, so both are the fiscal year's.
    t, *_ = _run(_case("C1"))
    assert t["ttm_revenue"] == pytest.approx(484.0) and t["ttm_net_income"] == pytest.approx(48.4)


def test_c2_the_latest_filing_being_the_10k_is_fy():
    t, label, *_ = _run(_case("C2"))
    assert t["ro40_basis"] == "FY" and t["ro40"] == pytest.approx(35.0)
    assert label == "FY2025 vs FY2024"


def test_c3a_a_restated_year_with_a_negative_rest_of_last_year_falls_back_to_fy():
    t, label, q, _a = _run(_case("C3a"))
    assert any(k.endswith("_suspect") for rec in q for k in rec)          # the extractor flagged the Q4
    assert t["ro40_basis"] == "FY" and t["ro40"] == pytest.approx(15.43, abs=0.01)   # not the mixed-basis 6.65
    assert label == "FY2025 vs FY2024"


def test_c3b_a_10q_that_restates_the_comparative_is_a_clean_ttm():
    t, label, *_ = _run(_case("C3b"))
    assert t["ro40_basis"] == "TTM"
    assert t["rev_growth_ttm"] == pytest.approx(0.2876, abs=1e-4)          # TTM vs TTM a year earlier (D3 A)
    assert t["ro40"] == pytest.approx(55.35, abs=0.01)
    assert label == "Q3/2026 vs Q3/2025"


def test_c4_a_52_53_week_year_ending_mid_february_reads_its_ytd_pieces():
    t, label, *_ = _run(_case("C4"))
    assert t["ro40_basis"] == "TTM" and t["ro40"] == pytest.approx(33.33, abs=0.01)
    assert label == "Q1/2026 vs Q1/2025"                                  # calendar quarter of the period end


def test_c5_a_june_fiscal_year_after_its_q1_10q():
    t, label, *_ = _run(_case("C5"))
    assert t["ro40_basis"] == "TTM" and t["ro40"] == pytest.approx(46.69, abs=0.01)
    assert label == "Q3/2026 vs Q3/2025"


@pytest.mark.parametrize("prefix", ["C6", "C7", "C8"])
def test_a_gap_in_the_ytd_chain_keeps_the_fiscal_year_basis(prefix):
    # C6 missing 6M cash flow, C7 a sign error that dropped the 6M capex, C8 no prior-year 10-Qs for the year before
    t, label, *_ = _run(_case(prefix))
    assert t["ro40_basis"] == "FY" and t["ro40"] == pytest.approx(35.0)
    assert label == "FY2025 vs FY2024"


# ── the reasons, read from the point helper ──

def _point(prefix):
    a, q = se.extract_annual(_case(prefix)), se.extract_quarterly(_case(prefix))
    return fp._ro40_point(q, a)


@pytest.mark.parametrize("prefix, reason", [
    ("C1", None), ("C2", "latest_is_fy"), ("C3a", "negative:revenue"), ("C6", "ytd_incomplete_current"),
    ("C7", "ytd_incomplete_current"), ("C8", "ytd_incomplete_prior2")])
def test_the_fallback_reason_is_named(prefix, reason):
    assert _point(prefix)[2] == reason


def test_the_ttm_pieces_of_c1():
    p, growth, _ = _point("C1")
    assert p["months"] == 6 and p["end"] == "2026-06-30" and p["prior_end"] == "2025-06-30"
    assert p["ttm"]["revenue"] == pytest.approx(508.2) and p["ttm_prior_revenue"] == pytest.approx(462.0)
    assert growth == pytest.approx(508.2 / 462.0 - 1)


# ── properties ──

def test_the_order_of_the_quarterly_list_does_not_change_the_result():
    a, q = se.extract_annual(_case("C3b")), se.extract_quarterly(_case("C3b"))
    base = fp._ttm_metrics(q, a), fp._period_label(q, a)
    rng = random.Random(7)
    for _ in range(5):
        shuffled = q[:]
        rng.shuffle(shuffled)
        assert (fp._ttm_metrics(shuffled, a), fp._period_label(shuffled, a)) == base


def test_at_nine_months_ttm_revenue_is_last_years_derived_q4_plus_this_years_ytd():
    facts = ft.company(ft.base_years(cur_quarters=3))
    a, q = se.extract_annual(facts), se.extract_quarterly(facts)
    p, _g, why = fp._ro40_point(q, a)
    assert why is None and p["months"] == 9
    by_end = {r["period"]: r for r in q}
    q4_last_year = by_end["2025-12-31"]["revenue"]                        # derived: FY2025 - 9M/2025
    assert p["ttm"]["revenue"] == pytest.approx(q4_last_year + p["ytd"]["revenue"])


def test_the_whole_rule_of_40_is_on_one_basis_when_one_input_has_a_gap():
    # C6: revenue has every YTD, only the 6M cash flow is missing: growth never comes from a TTM then.
    t, *_ = _run(_case("C6"))
    assert t["ro40_basis"] == "FY" and t["rev_growth_ttm"] == pytest.approx(0.10)


def test_no_annual_record_gives_no_basis():
    t = fp._ttm_metrics([], [])
    assert t["ro40_basis"] is None and t["ro40"] is None


# ── the sign guard (D1 A) on the pieces ──

def _pieces_with(**edits):
    facts = ft.company(ft.base_years())
    a, q = se.extract_annual(facts), se.extract_quarterly(facts)
    return a, q


def test_a_negative_prior_year_ytd_is_a_fallback_even_with_a_positive_rest_of_year():
    a, q = _pieces_with()
    rec = next(r for r in q if r["period"] == "2025-06-30")
    rec["ytd"]["capex"]["val"] = -1.0
    p, _g, why = fp._ro40_point(q, a)
    assert p is None and why == "negative:capex"


def test_a_prior_year_ytd_above_the_fiscal_year_is_a_fallback():
    a, q = _pieces_with()
    rec = next(r for r in q if r["period"] == "2025-06-30")
    rec["ytd"]["revenue"]["val"] = a[-1]["revenue"] + 1.0                 # FY - prior YTD < 0
    p, _g, why = fp._ro40_point(q, a)
    assert p is None and why == "negative:revenue"


def test_a_negative_operating_cash_flow_is_not_a_fallback():
    a, q = _pieces_with()
    for rec in q:
        if rec["period"] in ("2026-06-30",):
            rec["ytd"]["operating_cash_flow"]["val"] = -5.0
    p, _g, why = fp._ro40_point(q, a)
    assert why is None and p["ttm"]["operating_cash_flow"] < p["fy"]["operating_cash_flow"]


def test_a_nonpositive_ttm_revenue_is_a_fallback():
    a, q = _pieces_with()
    for rec in q:
        if rec["period"] == "2026-06-30":
            rec["ytd"]["revenue"]["val"] = 0.0
    rec = next(r for r in q if r["period"] == "2025-06-30")
    rec["ytd"]["revenue"]["val"] = a[-1]["revenue"]                       # FY - prior YTD = 0, YTD = 0
    p, _g, why = fp._ro40_point(q, a)
    assert p is None and why == "nonpositive_revenue"


# ── the windows ──

def test_the_prior_year_point_must_end_a_year_before_the_current_one():
    a, q = _pieces_with()
    rec = next(r for r in q if r["period"] == "2025-06-30")
    rec["period"] = "2025-04-30"                                          # 427 days before 2026-06-30
    assert fp._ro40_point(q, a)[2] == "ytd_incomplete_prior"


def test_the_ytd_must_start_one_to_seven_days_after_the_fiscal_year_ended():
    a, q = _pieces_with()
    for rec in q:
        if rec["period"] > "2026":                                        # every 10-Q of the new year
            for f in ("revenue", "operating_cash_flow", "capex"):
                rec["ytd"][f]["start"] = a[-1]["period_ends"]["revenue"]  # starts the day the year ended: not next year
    assert fp._ro40_point(q, a)[2] == "ytd_incomplete_current"


def test_the_ytd_months_of_the_three_fields_must_agree():
    a, q = _pieces_with()
    for rec in q:
        if rec["period"] == "2026-06-30":
            rec["ytd"]["capex"]["months"] = 3
    assert fp._ro40_point(q, a)[2] == "ytd_incomplete_current"


def test_the_prior_year_point_needs_the_same_months():
    a, q = _pieces_with()
    rec = next(r for r in q if r["period"] == "2025-06-30")
    rec["ytd"]["revenue"]["months"] = 3
    assert fp._ro40_point(q, a)[2] == "ytd_incomplete_prior"


def test_the_prior_ttm_needs_an_adjacent_prior_fiscal_year():
    a, q = _pieces_with()
    del a[-2]                                                             # FY2024 gone: FY2023 is two years back
    a2 = [a[0], a[-1]]
    why = fp._ro40_point(q, a2)[2]
    assert why in ("prior_fy_missing", "prior_fy_not_adjacent")


# ── D2 B: TTM revenue and net income keep today's rule ──

def _reported_quarters(n_extra=0):
    ends = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
    return [{"period": e, "revenue": 100.0 + 10 * i, "operating_cash_flow": 50.0, "capex": 10.0, "net_income": 20.0}
            for i, e in enumerate(ends)]


def test_four_reported_quarters_still_give_ttm_revenue_and_net_income():
    t = fp._ttm_metrics(_reported_quarters(), [])
    assert t["ttm_revenue"] == pytest.approx(460.0) and t["ttm_net_income"] == pytest.approx(80.0)
    assert t["ro40_basis"] is None and t["ro40"] is None                 # no annual record, no ytd: no Rule of 40


def test_quarters_without_ytd_never_make_a_ttm_rule_of_40():
    annual = [{"fiscal_year": "2024", "revenue": 1000.0, "operating_cash_flow": 200.0, "capex": 50.0},
              {"fiscal_year": "2025", "revenue": 1100.0, "operating_cash_flow": 300.0, "capex": 50.0}]
    q = [dict(r, period=e) for r, e in zip(_reported_quarters(), ["2026-03-31", "2026-06-30", "2026-09-30", "2026-12-31"])]
    t = fp._ttm_metrics(q, annual)
    assert t["ro40_basis"] == "FY" and t["rev_growth_ttm"] == pytest.approx(0.10)
    assert t["ttm_revenue"] == pytest.approx(460.0)


# ── versioning and wiring ──

def test_the_selection_version_is_bumped_for_the_new_scoring_basis():
    assert int(config.SELECTION_VERSION) >= 4          # 4: the TTM basis; later bumps keep it (5: C12, #383 R3)


def test_the_period_label_of_a_scored_row_comes_from_the_same_point():
    facts = _case("C1")
    a, q = se.extract_annual(facts), se.extract_quarterly(facts)
    assert fp._period_label(q, a) == "Q2/2026 vs Q2/2025"
    assert fp._period_label(q, []) == "latest"


def test_ttm_revenue_and_net_income_are_the_four_reported_quarters_not_the_ytd_point():
    # D2 B on hand-built records: four reported quarters AND a complete YTD point. The Rule of 40 uses the point,
    # ttm_revenue / ttm_net_income the four quarters (they differ on purpose: Q3'25..Q2'26 vs FY25 + 6M26 - 6M25).
    S, E = "2025-01-01", "2025-12-31"
    annual = [
        {"fiscal_year": "2024", "revenue": 800.0, "operating_cash_flow": 160.0, "capex": 40.0, "net_income": 60.0,
         "period_starts": {"revenue": "2024-01-01"}, "period_ends": {"revenue": "2024-12-31"}},
        {"fiscal_year": "2025", "revenue": 1000.0, "operating_cash_flow": 250.0, "capex": 50.0, "net_income": 100.0,
         "period_starts": {f: S for f in ft_fields()}, "period_ends": {f: E for f in ft_fields()}}]

    def ytd(start, months, rev, ocf, capex):
        return {"revenue": {"start": start, "months": months, "val": rev},
                "operating_cash_flow": {"start": start, "months": months, "val": ocf},
                "capex": {"start": start, "months": months, "val": capex}}
    q = [
        {"period": "2024-06-30", "revenue": 180.0, "net_income": 14.0, "ytd": ytd("2024-01-01", 6, 380.0, 75.0, 18.0)},
        {"period": "2025-06-30", "revenue": 240.0, "net_income": 24.0, "ytd": ytd(S, 6, 460.0, 100.0, 22.0)},
        {"period": "2025-09-30", "revenue": 250.0, "net_income": 26.0},
        {"period": "2025-12-31", "revenue": 270.0, "net_income": 28.0},
        {"period": "2026-03-31", "revenue": 280.0, "net_income": 30.0},
        {"period": "2026-06-30", "revenue": 300.0, "net_income": 32.0, "ytd": ytd("2026-01-01", 6, 580.0, 140.0, 30.0)},
    ]
    t = fp._ttm_metrics(q, annual)
    assert t["ro40_basis"] == "TTM"
    assert t["ttm_revenue"] == pytest.approx(250 + 270 + 280 + 300) and t["ttm_net_income"] == pytest.approx(116.0)
    # the point: revenue 1000 + 580 - 460 = 1120, OCF 250 + 140 - 100 = 290, capex 50 + 30 - 22 = 58
    assert t["fcf_margin"] == pytest.approx((290 - 58) / 1120)


def ft_fields():
    return ("revenue", "operating_cash_flow", "capex")


def test_a_ytd_starting_more_than_seven_days_after_the_year_end_is_not_this_years_ytd():
    a, q = _pieces_with()
    for rec in q:
        if rec["period"] > "2026":
            for f in ("revenue", "operating_cash_flow", "capex"):
                rec["ytd"][f]["start"] = "2026-01-10"                     # 10 days after 2025-12-31
    assert fp._ro40_point(q, a)[2] == "ytd_incomplete_current"


def test_a_year_earlier_ytd_above_its_fiscal_year_makes_no_growth():
    # The prior TTM (D3 A) needs FY2024 - 6M/2024 >= 0 as well, else the growth base is a restatement artefact.
    a, q = _pieces_with()
    rec = next(r for r in q if r["period"] == "2024-06-29")
    rec["ytd"]["revenue"]["val"] = next(x for x in a if x["fiscal_year"] == "2024")["revenue"] + 1.0
    p, g, why = fp._ro40_point(q, a)
    assert p is None and g is None and why == "negative_prior:revenue"


def test_a_newest_10q_without_a_revenue_ytd_does_not_step_back_to_an_older_point():
    # avo's rule: a gap means the fiscal year. The Q1 10-Q is complete, but the newest one (6M) is not.
    a, q = _pieces_with()
    newest = next(r for r in q if r["period"] == "2026-06-30")
    del newest["ytd"]["revenue"]
    p, g, why = fp._ro40_point(q, a)
    assert p is None and why == "ytd_incomplete_current"
    assert fp._ttm_metrics(q, a)["ro40_basis"] == "FY"


def test_a_fiscal_year_whose_capex_ends_on_another_day_than_revenue_is_incomplete():
    # 52/53-week filers can tag two years on one record; the three inputs must cover the same year end.
    a, q = _pieces_with()
    a[-1]["period_ends"]["capex"] = "2025-12-28"
    p, g, why = fp._ro40_point(q, a)
    assert p is None and why == "fy_incomplete"
    assert fp._ttm_metrics(q, a)["ro40_basis"] == "FY"


def test_a_fiscal_year_whose_capex_starts_on_another_day_than_revenue_is_incomplete():
    a, q = _pieces_with()
    a[-1]["period_starts"]["capex"] = "2024-12-29"
    assert fp._ro40_point(q, a)[2] == "fy_incomplete"
