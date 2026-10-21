"""validator.py tests — EBITDA/capex derivation, missing-field warnings, latest_valid."""
from __future__ import annotations

from RevenueChaser.core.providers import validator as v


def _rec(**kw) -> dict:
    base = {
        "fiscal_year": "2023",
        "revenue": 100.0,
        "ebitda": 30.0,
        "capex": 5.0,
        "total_debt": 20.0,
        "cash": 10.0,
    }
    base.update(kw)
    return base


def test_validate_annual_passthrough_when_all_fields_present():
    out, warnings = v.validate_annual([_rec()])
    assert out[0]["ebitda"] == 30.0
    assert warnings == []


def test_validate_annual_derives_ebitda_from_ebit_plus_depreciation():
    rec = _rec(ebitda=None, ebit=20.0, depreciation=8.0)
    out, warnings = v.validate_annual([rec])
    assert out[0]["ebitda"] == 28.0
    assert any("ebitda <- ebit + depreciation" in w for w in warnings)


def test_validate_annual_cannot_derive_ebitda_without_depreciation():
    rec = _rec(ebitda=None, ebit=20.0, depreciation=None)
    out, warnings = v.validate_annual([rec])
    assert out[0]["ebitda"] is None
    assert any("missing" in w and "ebitda" in w for w in warnings)


def test_validate_annual_derives_capex_from_capital_expenditure():
    rec = _rec()
    del rec["capex"]
    rec["capital_expenditure"] = 12.0
    out, _ = v.validate_annual([rec])
    assert out[0]["capex"] == 12.0


def test_validate_annual_reports_all_missing_required_fields():
    rec = {"fiscal_year": "2023"}
    out, warnings = v.validate_annual([rec])
    assert len(warnings) == 1
    assert "2023: missing" in warnings[0]
    for field in v.REQUIRED_FIELDS:
        assert field in warnings[0]


def test_validate_annual_does_not_mutate_input_records():
    rec = _rec(ebitda=None, ebit=20.0, depreciation=8.0)
    original = dict(rec)
    v.validate_annual([rec])
    assert rec == original


def test_latest_valid_returns_most_recent_complete_record():
    incomplete = _rec(fiscal_year="2022", cash=None)
    complete_old = _rec(fiscal_year="2023")
    complete_new = _rec(fiscal_year="2024")
    result = v.latest_valid([incomplete, complete_old, complete_new])
    assert result["fiscal_year"] == "2024"


def test_latest_valid_skips_trailing_incomplete_records():
    complete = _rec(fiscal_year="2023")
    incomplete_latest = _rec(fiscal_year="2024", total_debt=None)
    result = v.latest_valid([complete, incomplete_latest])
    assert result["fiscal_year"] == "2023"


def test_latest_valid_returns_none_when_nothing_qualifies():
    assert v.latest_valid([_rec(revenue=None)]) is None


def test_latest_valid_empty_list_returns_none():
    assert v.latest_valid([]) is None
