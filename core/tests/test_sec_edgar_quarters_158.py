"""#158: discrete quarters from YTD facts (Q2 = 6M − Q1, Q3 = 9M − 6M, Q4 = FY − 9M).

All facts are synthetic companyfacts payloads, no network. The derivation covers additive USD
flows only; share counts (weighted averages) and balance-sheet instants keep their old path.
"""
from __future__ import annotations

import itertools
import random

import pytest

from RevenueChaser.core.providers import sec_edgar as se

OCF = "NetCashProvidedByUsedInOperatingActivities"
REV = "Revenues"
REV_ALT = "RevenueFromContractWithCustomerExcludingAssessedTax"


def _f(start, end, val, filed, accn, form="10-Q", fp="Q1"):
    return {"start": start, "end": end, "val": val, "form": form, "fp": fp,
            "filed": filed, "accn": accn}


# Calendar fiscal year 2023: cumulative OCF 100 / 230 / 380 / 560 -> quarters 100 / 130 / 150 / 180.
def _ytd_2023(prefix="0000-23-"):
    return [
        _f("2023-01-01", "2023-03-31", 100.0, "2023-05-01", prefix + "01", fp="Q1"),
        _f("2023-01-01", "2023-06-30", 230.0, "2023-08-01", prefix + "02", fp="Q2"),
        _f("2023-01-01", "2023-09-30", 380.0, "2023-11-01", prefix + "03", fp="Q3"),
        _f("2023-01-01", "2023-12-31", 560.0, "2024-02-15", prefix + "04", form="10-K", fp="FY"),
    ]


def _facts(unit="USD", **concepts) -> dict:
    return {"entityName": "Synthetic Co", "facts": {"us-gaap": {
        name: {"units": {unit: list(rows)}} for name, rows in concepts.items()}}}


def _q(facts) -> dict:
    return {r["period"]: r for r in se.extract_quarterly(facts)}


def test_q2_q3_q4_are_derived_from_ytd_differences():
    recs = _q(_facts(**{OCF: _ytd_2023()}))
    assert {p: recs[p]["operating_cash_flow"] for p in sorted(recs)} == {
        "2023-03-31": 100.0, "2023-06-30": 130.0, "2023-09-30": 150.0, "2023-12-31": 180.0}


def test_derived_values_are_marked_and_the_reported_q1_is_not():
    recs = _q(_facts(**{OCF: _ytd_2023()}))
    assert "derived" not in recs["2023-03-31"]
    for p in ("2023-06-30", "2023-09-30", "2023-12-31"):
        assert recs[p]["derived"] == ["operating_cash_flow"]


def test_quarters_sum_to_the_fiscal_year():
    recs = _q(_facts(**{OCF: _ytd_2023()}))
    assert sum(r["operating_cash_flow"] for r in recs.values()) == pytest.approx(560.0)


def test_a_reported_discrete_quarter_wins_over_the_derived_one():
    # the reported fact is filed BEFORE the YTD facts: only the rule, not the filing date, makes it win
    rows = _ytd_2023() + [_f("2023-04-01", "2023-06-30", 131.0, "2023-07-15", "0000-23-02", fp="Q2")]
    rec = _q(_facts(**{OCF: rows}))["2023-06-30"]
    assert rec["operating_cash_flow"] == 131.0  # reported 131, not 230 − 100
    assert "derived" not in rec


def test_derived_q2_equals_the_reported_3m_q2_when_both_agree():
    reported = _q(_facts(**{OCF: _ytd_2023() + [_f("2023-04-01", "2023-06-30", 130.0, "2023-08-01", "x", fp="Q2")]}))
    derived = _q(_facts(**{OCF: _ytd_2023()}))
    assert reported["2023-06-30"]["operating_cash_flow"] == derived["2023-06-30"]["operating_cash_flow"]


def test_a_6m_fact_never_overwrites_the_3m_fact_with_the_same_end():
    """Trap (a): the series is keyed by (start, end), not by end alone."""
    rows = [
        _f("2023-01-01", "2023-03-31", 100.0, "2023-05-01", "a1", fp="Q1"),
        _f("2023-04-01", "2023-06-30", 130.0, "2023-08-01", "a2", fp="Q2"),
        _f("2023-01-01", "2023-06-30", 230.0, "2023-08-01", "a2", fp="Q2"),
    ]
    recs = _q(_facts(**{REV: rows}))
    assert recs["2023-06-30"]["revenue"] == 130.0
    assert recs["2023-03-31"]["revenue"] == 100.0


def test_non_december_fiscal_year():
    rows = [
        _f("2022-07-01", "2022-09-30", 10.0, "2022-11-01", "n1", fp="Q1"),
        _f("2022-07-01", "2022-12-31", 25.0, "2023-02-01", "n2", fp="Q2"),
        _f("2022-07-01", "2023-03-31", 45.0, "2023-05-01", "n3", fp="Q3"),
        _f("2022-07-01", "2023-06-30", 70.0, "2023-08-20", "n4", form="10-K", fp="FY"),
    ]
    recs = _q(_facts(**{OCF: rows}))
    assert [(p, recs[p]["operating_cash_flow"]) for p in sorted(recs)] == [
        ("2022-09-30", 10.0), ("2022-12-31", 15.0), ("2023-03-31", 20.0), ("2023-06-30", 25.0)]


def test_52_53_week_year_matches_the_fy_to_the_9m_by_start_date():
    """Trap (d): a 53-week FY (371 days) still pairs with its own 9M fact through the shared start."""
    rows = [
        _f("2022-01-03", "2022-04-02", 10.0, "2022-05-01", "w1", fp="Q1"),
        _f("2022-01-03", "2022-07-02", 30.0, "2022-08-01", "w2", fp="Q2"),
        _f("2022-01-03", "2022-10-01", 60.0, "2022-11-01", "w3", fp="Q3"),
        _f("2022-01-03", "2023-01-07", 100.0, "2023-02-20", "w4", form="10-K", fp="FY"),
        # next year starts the day after, and has its own Q1
        _f("2023-01-08", "2023-04-08", 7.0, "2023-05-01", "w5", fp="Q1"),
    ]
    recs = _q(_facts(**{OCF: rows}))
    assert recs["2023-01-07"]["operating_cash_flow"] == 40.0  # 100 − 60
    assert recs["2023-04-08"]["operating_cash_flow"] == 7.0
    assert recs["2022-07-02"]["operating_cash_flow"] == 20.0


def test_a_restated_10q_the_latest_filed_wins():
    rows = _ytd_2023() + [_f("2023-01-01", "2023-06-30", 240.0, "2023-12-01", "restated", form="10-Q/A", fp="Q2")]
    recs = _q(_facts(**{OCF: rows}))
    assert recs["2023-06-30"]["operating_cash_flow"] == 140.0  # 240 − 100
    assert recs["2023-09-30"]["operating_cash_flow"] == 140.0  # 380 − 240


def test_share_counts_are_never_derived():
    """Trap (b): weighted averages are not additive; only the reported 3M value is kept."""
    name = "WeightedAverageNumberOfDilutedSharesOutstanding"
    rows = [
        _f("2023-01-01", "2023-03-31", 50.0, "2023-05-01", "s1", fp="Q1"),
        _f("2023-01-01", "2023-06-30", 51.0, "2023-08-01", "s2", fp="Q2"),
        _f("2023-01-01", "2023-09-30", 52.0, "2023-11-01", "s3", fp="Q3"),
        _f("2023-01-01", "2023-12-31", 53.0, "2024-02-15", "s4", form="10-K", fp="FY"),
    ]
    recs = _q(_facts(unit="shares", **{name: rows}))
    assert {p: r["diluted_shares"] for p, r in recs.items()} == {"2023-03-31": 50.0}
    assert all("derived" not in r for r in recs.values())


def test_the_fy_minus_9m_difference_uses_one_concept():
    """Trap (c): a 9M under one tag and the FY under another are never subtracted."""
    facts = _facts(**{
        REV: [_f("2023-01-01", "2023-12-31", 900.0, "2024-02-15", "c4", form="10-K", fp="FY")],
        REV_ALT: [_f("2023-01-01", "2023-09-30", 600.0, "2023-11-01", "c3", fp="Q3")],
    })
    recs = _q(facts)
    assert all(r["revenue"] is None for p, r in recs.items() if p == "2023-12-31")


def test_missing_links_leave_gaps_not_guesses():
    """No 3M: no Q2 (6M − nothing). No 6M: no Q3, but Q4 = FY − 9M is still there."""
    rows = [r for r in _ytd_2023() if r["end"] != "2023-03-31"]
    recs = _q(_facts(**{OCF: rows}))
    assert recs["2023-09-30"]["operating_cash_flow"] == 150.0
    assert recs["2023-12-31"]["operating_cash_flow"] == 180.0
    assert "2023-06-30" not in recs or recs["2023-06-30"]["operating_cash_flow"] is None

    rows = [r for r in _ytd_2023() if r["end"] != "2023-06-30"]
    recs = _q(_facts(**{OCF: rows}))
    assert recs["2023-12-31"]["operating_cash_flow"] == 180.0
    assert "2023-09-30" not in recs or recs["2023-09-30"]["operating_cash_flow"] is None


def test_output_does_not_depend_on_fact_order():
    rows = _ytd_2023() + [_f("2023-04-01", "2023-06-30", 130.0, "2023-08-01", "x", fp="Q2")]
    base = se.extract_quarterly(_facts(**{OCF: rows}))
    rng = random.Random(7)
    for _ in range(6):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        assert se.extract_quarterly(_facts(**{OCF: shuffled})) == base


def test_instants_keep_their_old_path():
    """A balance-sheet instant tagged fp=FY in a 10-K is still no quarter; a Q-instant still is."""
    rows = [
        {"end": "2023-03-31", "val": 10.0, "fp": "Q1", "form": "10-Q", "filed": "2023-05-01", "accn": "i1"},
        {"end": "2023-12-31", "val": 99.0, "fp": "FY", "form": "10-K", "filed": "2024-02-15", "accn": "i2"},
    ]
    recs = _q(_facts(Assets=rows))
    assert list(recs) == ["2023-03-31"] and recs["2023-03-31"]["total_assets"] == 10.0
    assert "derived" not in recs["2023-03-31"]


def test_the_ytd_facts_are_kept_with_start_and_months():
    recs = _q(_facts(**{OCF: _ytd_2023()}))
    expected = {"2023-03-31": 3, "2023-06-30": 6, "2023-09-30": 9, "2023-12-31": 12}
    for period, months in expected.items():
        ytd = recs[period]["ytd"]["operating_cash_flow"]
        assert ytd["months"] == months and ytd["start"] == "2023-01-01"
    assert recs["2023-09-30"]["ytd"]["operating_cash_flow"]["val"] == 380.0
    assert recs["2023-12-31"]["ytd"]["operating_cash_flow"]["val"] == 560.0


def test_a_q1_alone_is_marked_ytd_3_only_after_a_fiscal_year_end():
    """An interior 3M fact (not at a fiscal-year start) is no YTD; a Q1 right after the FY end is."""
    rows = [
        _f("2022-01-01", "2022-12-31", 400.0, "2023-02-15", "y1", form="10-K", fp="FY"),
        _f("2023-01-01", "2023-03-31", 100.0, "2023-05-01", "y2", fp="Q1"),
        _f("2023-04-01", "2023-06-30", 130.0, "2023-08-01", "y3", fp="Q2"),
    ]
    recs = _q(_facts(**{OCF: rows}))
    assert recs["2023-03-31"]["ytd"]["operating_cash_flow"]["months"] == 3
    assert "ytd" not in recs["2023-06-30"]


def test_period_start_is_stored_for_flows_and_derived_quarters_start_after_the_previous_end():
    recs = _q(_facts(**{OCF: _ytd_2023()}))
    assert recs["2023-03-31"]["period_starts"]["operating_cash_flow"] == "2023-01-01"
    assert recs["2023-06-30"]["period_starts"]["operating_cash_flow"] == "2023-04-01"
    assert recs["2023-12-31"]["period_starts"]["operating_cash_flow"] == "2023-10-01"


def test_filed_and_accn_per_field_a_derived_value_takes_the_later_fact():
    recs = _q(_facts(**{OCF: _ytd_2023()}))
    assert recs["2023-03-31"]["filed"]["operating_cash_flow"] == "2023-05-01"
    assert recs["2023-03-31"]["accn"]["operating_cash_flow"] == "0000-23-01"
    assert recs["2023-09-30"]["filed"]["operating_cash_flow"] == "2023-11-01"  # max(6M, 9M filed)
    assert recs["2023-09-30"]["accn"]["operating_cash_flow"] == "0000-23-03"  # the 9M fact
    assert recs["2023-12-31"]["accn"]["operating_cash_flow"] == "0000-23-04"


def test_annual_records_carry_filed_and_accn_per_field():
    annual = se.extract_annual(_facts(**{OCF: _ytd_2023()}))
    rec = annual[-1]
    assert rec["operating_cash_flow"] == 560.0
    assert rec["filed"]["operating_cash_flow"] == "2024-02-15"
    assert rec["accn"]["operating_cash_flow"] == "0000-23-04"
    assert rec["period_ends"]["operating_cash_flow"] == "2023-12-31"
    assert rec["period_starts"]["operating_cash_flow"] == "2023-01-01"  # the fiscal year's first day


def test_annual_extraction_is_unchanged_by_the_new_keys():
    facts = _facts(**{OCF: _ytd_2023()})
    annual = se.extract_annual(facts)
    assert [r["fiscal_year"] for r in annual] == ["2023"]
    assert annual[0]["operating_cash_flow"] == 560.0


def test_buyback_and_sbc_are_new_fields_never_alternates_of_existing_ones():
    assert "share_repurchases" in se.CONCEPTS and "share_based_compensation" in se.CONCEPTS
    existing = {t for f, (tags, _u) in se.CONCEPTS.items()
                if f not in ("share_repurchases", "share_based_compensation") for t in tags}
    new_tags = set(se.CONCEPTS["share_repurchases"][0]) | set(se.CONCEPTS["share_based_compensation"][0])
    assert not (existing & new_tags)
    assert se.CONCEPTS["share_repurchases"][0][0] == "PaymentsForRepurchaseOfCommonStock"
    assert se.CONCEPTS["share_based_compensation"][0] == ["ShareBasedCompensation"]


def test_buyback_and_sbc_get_ytd_derived_quarters_and_annual_values():
    facts = _facts(**{REV: _ytd_2023(),  # they never add a record: a revenue gives the periods
                      "PaymentsForRepurchaseOfCommonStock": _ytd_2023(),
                      "ShareBasedCompensation": _ytd_2023()})
    q = _q(facts)
    assert q["2023-09-30"]["share_repurchases"] == 150.0
    assert q["2023-12-31"]["share_based_compensation"] == 180.0
    assert se.extract_annual(facts)[-1]["share_repurchases"] == 560.0


def test_buyback_alternate_is_resolved_per_period_in_priority_order():
    rows_a = [_f("2023-01-01", "2023-03-31", 1.0, "2023-05-01", "p1", fp="Q1")]
    rows_b = [_f("2023-01-01", "2023-03-31", 9.0, "2023-05-01", "p2", fp="Q1"),
              _f("2023-04-01", "2023-06-30", 2.0, "2023-08-01", "p3", fp="Q2")]
    facts = _facts(PaymentsForRepurchaseOfCommonStock=rows_a, PaymentsForRepurchaseOfEquity=rows_b,
                   Revenues=rows_b)
    q = _q(facts)
    assert q["2023-03-31"]["share_repurchases"] == 1.0  # the first listed tag wins the period
    assert q["2023-06-30"]["share_repurchases"] == 2.0  # the alternate fills the period the first lacks


def test_a_period_with_only_buyback_or_sbc_is_no_record():
    facts = _facts(PaymentsForRepurchaseOfCommonStock=_ytd_2023(), ShareBasedCompensation=_ytd_2023())
    assert se.extract_quarterly(facts) == [] and se.extract_annual(facts) == []
    assert {"share_repurchases", "share_based_compensation"} <= se._NEVER_A_RECORD


def test_extract_version_is_stored_in_meta(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import rawstore
    monkeypatch.setattr(se, "_cik_map", lambda: {"AAA": {"cik": 1, "name": "Aaa"}})
    monkeypatch.setattr(se, "_fetch_facts", lambda cik: _facts(**{OCF: _ytd_2023()}))
    _annual, quarterly, meta = se.get_fundamentals("AAA")
    assert meta["extract_version"] == se.EXTRACT_VERSION
    assert rawstore.get("edgar_financials", "AAA")["meta"]["extract_version"] == se.EXTRACT_VERSION
    assert quarterly[-1]["derived"] == ["operating_cash_flow"]


def test_a_cached_row_without_the_new_keys_is_served_as_it_is(rawstore_tmp_db):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("edgar_financials", [{
        "ticker": "OLD", "annual": [{"revenue": 1}], "quarterly": [{"period": "2023-03-31", "revenue": 1}],
        "meta": {"available": True}}])
    _a, q, meta = se.get_fundamentals("OLD", no_fetch=True)
    assert q == [{"period": "2023-03-31", "revenue": 1}] and "extract_version" not in meta


@pytest.mark.parametrize("order", list(itertools.permutations(["Revenues", REV_ALT])))
def test_concept_order_in_the_payload_does_not_matter(order):
    rows = {"Revenues": _ytd_2023(), REV_ALT: _ytd_2023("0000-99-")}
    facts = {"facts": {"us-gaap": {n: {"units": {"USD": rows[n]}} for n in order}}}
    q = _q(facts)
    assert q["2023-12-31"]["revenue"] == 180.0


def test_two_cumulative_facts_too_close_or_too_far_apart_derive_nothing():
    """A 3M fact ending 11 days before a 6M fact is no Q2: the difference must be one quarter long."""
    rows = [
        _f("2023-01-01", "2023-05-30", 100.0, "2023-07-01", "g1", fp="Q1"),   # 149 days, counts as a 3M
        _f("2023-01-01", "2023-06-10", 130.0, "2023-08-01", "g2", fp="Q2"),   # 160 days, a 6M
    ]
    recs = _q(_facts(**{OCF: rows}))
    assert "2023-06-10" not in recs
    assert recs["2023-05-30"]["operating_cash_flow"] == 100.0


# ── Opus review fixes (F1, F2, m1-m4) ──
def _v1_quarterly_facts(facts, concepts, unit):
    """A frozen copy of the pre-#158 ``_quarterly_facts`` (main 9ae22af): the differential reference."""
    from datetime import date
    best = {}
    for concept in concepts:
        by_end = {}
        for f in se._gaap(facts).get(concept, {}).get("units", {}).get(unit, []):
            if f.get("fp") not in ("Q1", "Q2", "Q3", "Q4"):
                continue
            end, start, val = f.get("end") or "", f.get("start") or "", se.to_float(f.get("val"))
            if not end or val is None:
                continue
            if start:
                d = (date.fromisoformat(end) - date.fromisoformat(start)).days
                if d < 60 or d > 150:
                    continue
            filed = f.get("filed") or ""
            cur = by_end.get(end)
            if cur is None or filed > cur[0]:
                by_end[end] = (filed, val)
        series = {e: (v, e) for e, (_, v) in by_end.items()}
        if not series:
            continue
        if not best or max(series) > max(best) or (max(series) == max(best) and len(series) > len(best)):
            best = series
    return best


def _random_payload(rng):
    """Two revenue tags over 2021-2023 with random gaps, restatements and 10-K quarterly notes."""
    out = {}
    serial = 0
    for tag in (REV, REV_ALT):
        rows = []
        for year in (2021, 2022, 2023):
            if rng.random() < 0.25:
                continue
            spans = [("Q1", f"{year}-01-01", f"{year}-03-31", 0.8), ("Q2", f"{year}-04-01", f"{year}-06-30", 0.5),
                     ("Q2", f"{year}-01-01", f"{year}-06-30", 0.8), ("Q3", f"{year}-07-01", f"{year}-09-30", 0.5),
                     ("Q3", f"{year}-01-01", f"{year}-09-30", 0.8), ("FY", f"{year}-01-01", f"{year}-12-31", 0.8),
                     ("FY", f"{year}-10-01", f"{year}-12-31", 0.3)]  # the last one: a 10-K quarterly note
            for fp, start, end, p in spans:
                for _ in range(2 if rng.random() < 0.2 else 1):  # sometimes restated
                    if rng.random() < p:
                        serial += 1
                        rows.append(_f(start, end, float(rng.randint(1, 900)), f"{year + 1}-{1 + serial % 12:02d}-15",
                                       f"a{serial:04d}", form="10-K" if fp == "FY" else "10-Q", fp=fp))
        if rows:
            out[tag] = rows
    return _facts(**out)


@pytest.mark.parametrize("seed", range(60))
def test_v2_only_adds_quarters_to_what_v1_extracted(seed):
    """F1: every quarter v1 reported keeps its value and its tag; v2 adds derived quarters around them."""
    facts = _random_payload(random.Random(seed))
    concepts = se.CONCEPTS["revenue"][0]
    v1 = _v1_quarterly_facts(facts, concepts, "USD")
    v2 = se._quarterly_facts(facts, concepts, "USD")
    for period, value in v1.items():
        assert v2.get(period) == value, (seed, period)


def test_a_newer_derived_only_tag_does_not_displace_the_tag_with_reported_quarters():
    older = [_f("2023-01-01", "2023-03-31", 100.0, "2023-05-01", "o1", fp="Q1"),
             _f("2023-04-01", "2023-06-30", 131.0, "2023-08-01", "o2", fp="Q2"),
             _f("2023-07-01", "2023-09-30", 150.0, "2023-11-01", "o3", fp="Q3")]
    newer = [r for r in _ytd_2023("0000-77-")]  # the same year as YTD facts: derives Q2 = 130 and a Q4
    recs = _q(_facts(**{REV: older, REV_ALT: newer}))
    assert recs["2023-06-30"]["revenue"] == 131.0  # the reported tag, not the derived 130
    assert recs["2023-09-30"]["revenue"] == 150.0


def test_a_field_with_no_reported_quarter_is_still_ranked_on_its_derived_ones():
    recs = _q(_facts(**{OCF: _ytd_2023()}))
    assert recs["2023-12-31"]["operating_cash_flow"] == 180.0


def test_a_negative_derived_value_of_a_non_negative_field_is_dropped_and_flagged():
    """F2: FY restated for discontinued operations while the 9M still holds the total: Q4 = −100."""
    rows = [r for r in _ytd_2023() if r["end"] != "2023-12-31"] + [
        _f("2023-01-01", "2023-12-31", 280.0, "2024-02-15", "r4", form="10-K", fp="FY")]
    facts = _facts(**{REV: rows, OCF: _ytd_2023()})
    rec = _q(facts)["2023-12-31"]
    assert rec["revenue"] is None and rec["revenue_suspect"] is True
    assert rec["operating_cash_flow"] == 180.0 and "operating_cash_flow_suspect" not in rec


def test_cash_flow_net_income_and_ebit_may_be_negative_when_derived():
    rows = [_f("2023-01-01", "2023-06-30", 10.0, "2023-08-01", "n2", fp="Q2"),
            _f("2023-01-01", "2023-03-31", 40.0, "2023-05-01", "n1", fp="Q1")]
    facts = _facts(NetIncomeLoss=rows, **{OCF: rows, "OperatingIncomeLoss": rows})
    rec = _q(facts)["2023-06-30"]
    assert rec["net_income"] == rec["operating_cash_flow"] == rec["ebit"] == -30.0
    assert not any(k.endswith("_suspect") for k in rec)


def test_the_only_reported_negative_value_is_kept():
    """Only a DERIVED negative is dropped; a reported (non-derived) value stays as the filer wrote it."""
    rec = _q(_facts(**{REV: [_f("2023-01-01", "2023-03-31", -5.0, "2023-05-01", "z1", fp="Q1")]}))["2023-03-31"]
    assert rec["revenue"] == -5.0 and "revenue_suspect" not in rec


def test_filed_and_accn_of_a_derived_value_come_from_the_same_later_filed_fact():
    rows = _ytd_2023() + [_f("2023-01-01", "2023-06-30", 240.0, "2024-12-05", "amend", form="10-Q/A", fp="Q2")]
    rec = _q(_facts(**{OCF: rows}))["2023-09-30"]  # 380 (filed 2023-11-01) − 240 (filed 2024-12-05)
    assert (rec["filed"]["operating_cash_flow"], rec["accn"]["operating_cash_flow"]) == ("2024-12-05", "amend")


def test_ytd_at_a_fiscal_year_end_is_the_twelve_month_fact_not_a_3m_note():
    # a filer that changed its year end: a 12M year ended 2023-09-30, so a 3M fact starting 2023-10-01 is
    # a "Q1" whose end coincides with the calendar-2023 FY end
    rows = _ytd_2023() + [
        _f("2022-10-01", "2023-09-30", 400.0, "2023-11-20", "old-fy", form="10-K", fp="FY"),
        _f("2023-10-01", "2023-12-31", 180.0, "2024-02-15", "note", form="10-K", fp="FY")]
    recs = _q(_facts(**{OCF: rows}))
    ytd = recs["2023-12-31"]["ytd"]["operating_cash_flow"]
    assert ytd["months"] == 12 and ytd["val"] == 560.0
    assert recs["2023-12-31"]["operating_cash_flow"] == 180.0


def test_costco_like_52_week_year_with_a_251_day_nine_month_fact():
    rows = [
        _f("2022-09-05", "2022-11-27", 10.0, "2023-01-01", "k1", fp="Q1"),   # 83 days
        _f("2022-09-05", "2023-02-19", 30.0, "2023-03-01", "k2", fp="Q2"),   # 167 days
        _f("2022-09-05", "2023-05-14", 60.0, "2023-06-01", "k3", fp="Q3"),   # 251 days
        _f("2022-09-05", "2023-09-03", 100.0, "2023-10-20", "k4", form="10-K", fp="FY"),  # 363 days
    ]
    recs = _q(_facts(**{OCF: rows}))
    assert [recs[p]["operating_cash_flow"] for p in sorted(recs)] == [10.0, 20.0, 30.0, 40.0]


def test_kroger_like_16_12_12_12_week_year():
    rows = [
        _f("2023-02-05", "2023-05-27", 16.0, "2023-07-01", "r1", fp="Q1"),   # 111 days
        _f("2023-02-05", "2023-08-19", 28.0, "2023-09-20", "r2", fp="Q2"),   # 195 days
        _f("2023-02-05", "2023-11-11", 40.0, "2023-12-15", "r3", fp="Q3"),   # 279 days
        _f("2023-02-05", "2024-02-03", 52.0, "2024-04-01", "r4", form="10-K", fp="FY"),   # 363 days
    ]
    recs = _q(_facts(**{OCF: rows}))
    assert [recs[p]["operating_cash_flow"] for p in sorted(recs)] == [16.0, 12.0, 12.0, 12.0]


def test_a_10q_trailing_twelve_month_column_is_no_ytd_and_no_fiscal_year():
    """A 10-Q lists a 9M YTD column and a 12M trailing column ending on the same day: the 12M is no FY."""
    rows = _ytd_2023() + [
        _f("2022-10-01", "2023-09-30", 480.0, "2023-11-01", "0000-23-03", fp="Q3"),  # TTM, form 10-Q
    ]
    recs = _q(_facts(**{OCF: rows}))
    ytd = recs["2023-09-30"]["ytd"]["operating_cash_flow"]
    assert ytd["months"] == 9 and ytd["start"] == "2023-01-01" and ytd["val"] == 380.0
    assert recs["2023-09-30"]["operating_cash_flow"] == 150.0
    assert recs["2023-12-31"]["operating_cash_flow"] == 180.0


def test_the_fundamentals_ttm_run_skips_what_this_extractor_marks_as_derived():
    """The #148 reader (module_fundamentals.pipeline._quarter_run) must read the `derived` list this module writes."""
    from RevenueChaser.module_fundamentals import pipeline
    recs = se.extract_quarterly(_facts(**{REV: _ytd_2023()}))
    assert pipeline._quarter_run(recs, "revenue", 4) is None  # Q2-Q4 are derived: no TTM run
    assert pipeline._quarter_run(recs, "revenue", 1) == ([100.0], ["2023-03-31"])  # the reported Q1 still counts
