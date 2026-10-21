"""Conflict detection (issue #85 Part 2)."""
from __future__ import annotations

from RevenueChaser.core.identity import conflicts as C


def base(**kw):
    d = {"name": "Apple Inc.", "market_cap": 1e9,
         "sec": {"status": "ok", "name": "APPLE INC", "forms": ["10-K"]},
         "openfigi": {"security_type": "Common Stock", "security_type2": "Common Stock", "name": "APPLE INC"},
         "nasdaq": {"is_etf": False, "security_name": "Apple Inc. - Common Stock"},
         "uw": {"security_type": "Common Stock", "market_cap": 1.1e9}}
    d.update(kw)
    return d


def kinds(rows):
    return {(r["kind"], r["field"]) for r in rows}


def test_clean_ticker_has_no_conflicts():
    assert C.detect("AAPL", base(), "common", "openfigi") == []


def test_row_shape_and_unique_keys():
    rows = C.detect("X", base(name="123", uw={"security_type": "ADR"}), "common", "openfigi")
    assert rows
    for r in rows:
        assert set(r) == {"ticker", "kind", "field", "side_a", "side_b", "note"}
    keys = [(r["ticker"], r["kind"], r["field"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_name_matching_none_of_the_sources_is_a_conflict():
    rows = C.detect("X", base(name="Zebra Mining"), "common", "openfigi")
    assert {r["kind"] for r in rows} == {"name"}
    assert any(r["side_a"] == "Zebra Mining" for r in rows)


def test_name_matching_one_source_is_fine():
    assert C.detect("X", base(name="Apple Inc.", sec={"status": "ok", "name": "Orange Holdings", "forms": []}),
                    "common", "openfigi") == []


def test_numeric_stored_name_is_a_conflict():
    rows = C.detect("X", base(name=" 4321 "), "common", "openfigi")
    assert any("number" in r["note"] for r in rows if r["kind"] == "name")


def test_no_name_sources_no_name_conflict():
    assert C.detect("X", {"name": "Zebra", "sec": None, "openfigi": None, "nasdaq": None}, "common", "none") == []


def test_uw_adr_vs_common_is_type_conflict():
    rows = C.detect("AZN", base(uw={"security_type": "ADR"}), "common", "openfigi")
    assert ("type", "security_type:uw_issue_type") in kinds(rows)


def test_mlp_vs_uw_unit_is_type_conflict():
    rows = C.detect("EPD", base(uw={"security_type": "Unit"}), "mlp", "openfigi")
    assert ("type", "security_type:uw_issue_type") in kinds(rows)


def test_uw_unit_vs_spac_is_fine():
    rows = C.detect("SP", base(uw={"security_type": "Unit"}), "spac", "sec_sic_6770")
    assert [r for r in rows if r["field"] == "security_type:uw_issue_type"] == []
    assert [r["note"] for r in rows if r["field"] == "security_type:openfigi"] == ["expected override"]


def test_nasdaq_etf_vs_openfigi():
    rows = C.detect("X", base(nasdaq={"is_etf": True, "security_name": "Apple Inc."}), "etp", "nasdaq_etf_flag")
    assert ("type", "security_type:nasdaq_etf_flag") in kinds(rows)
    rows = C.detect("X", base(nasdaq={"is_etf": False, "security_name": "Apple Inc."},
                              openfigi={"security_type": "ETP", "security_type2": "", "name": "APPLE INC"}),
                    "etp", "openfigi")
    assert ("type", "security_type:nasdaq_etf_flag") in kinds(rows)


def test_fund_forms_not_typed_as_fund():
    rows = C.detect("X", base(sec={"status": "ok", "name": "APPLE INC", "forms": ["N-CSR"]}), "common", "openfigi")
    assert ("type", "security_type:sec_forms") in kinds(rows)


def test_openfigi_disagreement_marks_expected_override_or_check():
    cef = {"security_type": "Closed-End Fund", "security_type2": "Mutual Fund", "name": "APPLE INC"}
    rows = C.detect("TCPC", base(openfigi=cef), "bdc", "sec_forms(N-54A or 10-K+N-2)")
    assert next(r for r in rows if r["field"] == "security_type:openfigi")["note"] == "expected override"
    rows = C.detect("X", base(openfigi=cef), "common", "uw_issue_type")
    assert next(r for r in rows if r["field"] == "security_type:openfigi")["note"] == "check"


def test_spac_shell_closed_with_sic_6770_is_flagged():
    b = base(sec={"status": "ok", "name": "APPLE INC", "forms": ["10-K"], "sic": "6770", "shell_closed": True})
    rows = C.detect("X", b, "common", "openfigi")
    assert ("type", "security_type:spac_shell_closed") in kinds(rows)


def test_cap_more_than_10x_either_way_is_flagged_not_changed():
    hi = C.detect("X", base(market_cap=2e10, uw={"security_type": "Common Stock", "market_cap": 1e9}),
                  "common", "openfigi")
    lo = C.detect("X", base(market_cap=1e9, uw={"security_type": "Common Stock", "market_cap": 2e10}),
                  "common", "openfigi")
    assert ("cap", "market_cap:uw") in kinds(hi) and ("cap", "market_cap:uw") in kinds(lo)
    # #143b: the note says the caps differ and which side is higher; it must not claim the stored cap is the wrong one
    # (SKHY's stored $1.36T is Yahoo's own figure; UW's $34B looks like the ADS float).
    hi_note = next(r for r in hi if r["kind"] == "cap")["note"]
    lo_note = next(r for r in lo if r["kind"] == "cap")["note"]
    assert hi_note.startswith("caps differ > 10x") and "stored is higher" in hi_note
    assert lo_note.startswith("caps differ > 10x") and "provider is higher" in lo_note
    assert "stored cap > 10x provider" not in hi_note and "provider cap > 10x stored" not in lo_note
    assert C.detect("X", base(market_cap=5e9, uw={"security_type": "Common Stock", "market_cap": 1e9}),
                    "common", "openfigi") == []
