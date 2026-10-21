"""Sector and industry by source precedence (issue #85 Part 2, reworked in #265, order extended in #272).

Classification is UW's two levels: the sector (one of the 11 Morningstar names) and the industry text of the
provider row. The order is manual, the S&P 1500 list (its own names only, through ``canonical_sector``), UW, Yahoo
(only where UW has no usable sector), the explicit SIC table, the type rule, ``Unclassified``. The industry is always
UW's (then Yahoo's) text: the list's GICS industry names are never written. No GICS rename, no corrections.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.identity import gics

LEVELS = ("sector", "industry")
MORNINGSTAR = ("Basic Materials", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Energy",
               "Financial Services", "Healthcare", "Industrials", "Real Estate", "Technology", "Utilities")


def run(inputs, typ="common", **kw):
    return gics.classify("X", inputs, typ, **kw)


def vals(res):
    return tuple(res[lv][0] for lv in LEVELS)


def srcs(res):
    return tuple(res[lv][1] for lv in LEVELS)


def test_there_are_two_levels_sector_and_industry():
    assert gics.LEVELS == LEVELS
    assert tuple(gics.MORNINGSTAR_SECTORS) == MORNINGSTAR


def test_uw_sector_and_industry_are_stored_as_the_provider_names_them():
    res = run({"uw": {"sector": "Technology", "industry": "Software - Application"}})
    assert vals(res) == ("Technology", "Software - Application")
    assert srcs(res) == ("provider_uw", "provider_uw")


@pytest.mark.parametrize("name", MORNINGSTAR)
def test_every_morningstar_sector_name_is_taken_unchanged(name):
    assert run({"uw": {"sector": name, "industry": "x"}})["sector"] == (name, "provider_uw")
    assert run({"yahoo": {"sector": name, "industry": "x"}})["sector"] == (name, "provider_yf")


@pytest.mark.parametrize("name", ["Information Technology", "Health Care", "Financials", "Materials", "Financial",
                                  "Software", "", None, 5])
def test_a_sector_outside_the_eleven_names_is_not_usable(name):
    res = run({"uw": {"sector": name, "industry": "Whatever"}})
    assert vals(res) == ("Unclassified",) * 2 and srcs(res) == ("none",) * 2


def test_an_unusable_uw_sector_falls_through_to_yahoo():
    res = run({"uw": {"sector": "Health Care", "industry": "x"},
               "yahoo": {"sector": "Healthcare", "industry": "Biotechnology"}})
    assert res["sector"] == ("Healthcare", "provider_yf")
    assert res["industry"] == ("Biotechnology", "provider_yf")


def test_yahoo_used_only_when_uw_has_no_sector():
    y = {"sector": "Healthcare", "industry": "Biotechnology"}
    assert run({"yahoo": y})["sector"] == ("Healthcare", "provider_yf")
    assert run({"yahoo": y, "uw": {"sector": "Energy", "industry": "Oil"}})["sector"] == ("Energy", "provider_uw")
    assert run({"yahoo": y, "uw": {"sector": None, "industry": None}})["sector"] == ("Healthcare", "provider_yf")


def test_the_industry_comes_from_the_same_provider_row_as_the_sector():
    # UW has a sector but no industry: Yahoo's industry is NOT mixed in under UW's sector
    res = run({"uw": {"sector": "Energy", "industry": None}, "yahoo": {"sector": "Technology", "industry": "Software"}})
    assert res["sector"] == ("Energy", "provider_uw")
    assert res["industry"] == ("Unclassified", "none")


def test_the_industry_is_not_taken_from_yahoo_even_when_both_providers_name_the_same_sector():
    res = run({"uw": {"sector": "Energy", "industry": None}, "yahoo": {"sector": "Energy", "industry": "Oil & Gas E&P"}})
    assert res["sector"] == ("Energy", "provider_uw")
    assert res["industry"] == ("Unclassified", "none")


def test_an_industry_text_is_trimmed_and_a_blank_one_is_unclassified():
    assert run({"uw": {"sector": "Energy", "industry": "  Oil & Gas E&P "}})["industry"][0] == "Oil & Gas E&P"
    assert run({"uw": {"sector": "Energy", "industry": "   "}})["industry"] == ("Unclassified", "none")


# -- the S&P 1500 list: a sector source for its own names (#272) --------------------------------
GICS_TO_MORNINGSTAR = {
    "Information Technology": "Technology", "Financials": "Financial Services", "Health Care": "Healthcare",
    "Consumer Discretionary": "Consumer Cyclical", "Consumer Staples": "Consumer Defensive",
    "Materials": "Basic Materials", "Communication Services": "Communication Services", "Energy": "Energy",
    "Industrials": "Industrials", "Real Estate": "Real Estate", "Utilities": "Utilities"}


@pytest.mark.parametrize("gics_name, morningstar", sorted(GICS_TO_MORNINGSTAR.items()))
def test_the_list_sector_is_written_as_the_morningstar_name(gics_name, morningstar):
    res = run({"sp1500": {"sector": gics_name, "sub_industry": "Whatever", "index": "sp500"}})
    assert res["sector"] == (morningstar, "sp1500")


def test_the_list_sector_beats_uw_and_yahoo_where_they_disagree():
    uw = {"sector": "Technology", "industry": "Software - Application"}
    res = run({"sp1500": {"sector": "Industrials", "sub_industry": "Research & Consulting Services"},
               "uw": uw, "yahoo": {"sector": "Energy", "industry": "Oil & Gas E&P"}})
    assert res["sector"] == ("Industrials", "sp1500")


def test_the_industry_stays_uws_text_and_the_lists_sub_industry_is_never_written():
    res = run({"sp1500": {"sector": "Industrials", "sub_industry": "Research & Consulting Services"},
               "uw": {"sector": "Technology", "industry": "Software - Application"}})
    assert res["industry"] == ("Software - Application", "provider_uw"), "UW's text, even under the list's sector"
    assert "Research & Consulting Services" not in vals(res)
    # the list row alone gives a sector and no industry
    assert run({"sp1500": {"sector": "Industrials", "sub_industry": "Airlines"}})["industry"] == ("Unclassified", "none")


def test_where_the_list_and_uw_agree_the_sector_is_the_lists_and_the_industry_uws():
    res = run({"sp1500": {"sector": "Information Technology", "sub_industry": "Semiconductors"},
               "uw": {"sector": "Technology", "industry": "Semiconductors"}})
    assert res["sector"] == ("Technology", "sp1500") and res["industry"] == ("Semiconductors", "provider_uw")


def test_a_list_name_without_a_uw_industry_takes_yahoos_text_then_nothing():
    lst = {"sector": "Financials", "sub_industry": "Regional Banks"}
    res = run({"sp1500": lst, "uw": {"sector": "Financial Services", "industry": None},
               "yahoo": {"sector": "Financial Services", "industry": "Banks - Regional"}})
    assert res["sector"] == ("Financial Services", "sp1500") and res["industry"] == ("Banks - Regional", "provider_yf")
    res = run({"sp1500": lst, "uw": {}, "yahoo": {}})
    assert res["industry"] == ("Unclassified", "none")
    res = run({"sp1500": lst, "uw": {"sector": "Healthcare", "industry": "  Biotechnology "},
               "yahoo": {"sector": "Healthcare", "industry": "Drug Manufacturers"}})
    assert res["industry"] == ("Biotechnology", "provider_uw"), "UW first, trimmed"


def test_a_provider_industry_is_not_taken_from_a_row_whose_sector_is_not_one_of_the_11_names():
    """The industry rides on a usable provider sector, as before the list came back: a row with a GICS spelling or no
    sector gives no industry text, so UW's industry cannot sit next to a sector it never named."""
    lst = {"sector": "Industrials", "sub_industry": "Airlines"}
    res = run({"sp1500": lst, "uw": {"sector": "Information Technology", "industry": "Software - Application"}})
    assert res["sector"] == ("Industrials", "sp1500") and res["industry"] == ("Unclassified", "none")
    res = run({"sp1500": lst, "uw": {"sector": None, "industry": "Software - Application"},
               "yahoo": {"sector": "Industrials", "industry": "Airlines"}})
    assert res["industry"] == ("Airlines", "provider_yf"), "the next provider with a usable sector gives it"


@pytest.mark.parametrize("raw", ["Unmapped Spelling", "", "   ", None, 5, "Unclassified"])
def test_a_list_sector_the_canonical_map_does_not_know_is_not_a_sector(raw):
    res = run({"sp1500": {"sector": raw, "sub_industry": "x"}, "uw": {"sector": "Energy", "industry": "Oil"}})
    assert res["sector"] == ("Energy", "provider_uw")
    assert run({"sp1500": {"sector": raw}})["sector"] == ("Unclassified", "none")


def test_a_list_row_without_a_sector_is_the_empty_answer():
    for empty in ({}, None):
        assert run({"sp1500": empty, "uw": {"sector": "Energy", "industry": "Oil"}})["sector"] == (
            "Energy", "provider_uw")


def test_manual_beats_the_list_and_an_etp_has_no_classification_even_on_the_list():
    res = run({"sp1500": {"sector": "Industrials", "sub_industry": "x"}}, manual={"sector": "Energy"})
    assert res["sector"] == ("Energy", "manual")
    res = run({"sp1500": {"sector": "Industrials", "sub_industry": "x"},
               "uw": {"sector": "Technology", "industry": "y"}}, typ="etp")
    assert vals(res) == ("Unclassified",) * 2 and all("etp" in s for s in srcs(res))


# -- the SIC fallback (#272) -------------------------------------------------------------------
def test_sic_gives_a_sector_to_a_name_no_other_source_has():
    res = run({"sec": {"sic": "3841", "status": "ok"}})
    assert res["sector"] == ("Healthcare", "sic_explicit")
    assert res["industry"] == ("Unclassified", "none"), "SIC gives a sector only; its text is a separate raw column"


def test_the_sic_code_comes_from_the_submissions_row_then_from_the_store():
    assert run({"sic_store": "7372"})["sector"] == ("Technology", "sic_explicit")
    assert run({"sec": None, "sic_store": "6022"})["sector"] == ("Financial Services", "sic_explicit")
    assert run({"sec": {"sic": "", "status": "ok"}, "sic_store": "2834"})["sector"] == ("Healthcare", "sic_explicit")
    both = {"sec": {"sic": "7372", "status": "ok"}, "sic_store": "2834"}
    assert run(both)["sector"] == ("Technology", "sic_explicit"), "the submissions code first, as before #265"


def test_an_unlisted_submissions_code_does_not_fall_through_to_the_stores_listed_one():
    """One code per ticker, as measured: the store's code is only used when the submissions row has none."""
    assert vals(run({"sec": {"sic": "7389", "status": "ok"}, "sic_store": "2834"})) == ("Unclassified",) * 2


def test_sic_ranks_below_the_list_uw_and_yahoo():
    sec = {"sic": "7372", "status": "ok"}
    assert run({"sec": sec, "sp1500": {"sector": "Financials"}})["sector"] == ("Financial Services", "sp1500")
    assert run({"sec": sec, "uw": {"sector": "Energy", "industry": "x"}})["sector"] == ("Energy", "provider_uw")
    assert run({"sec": sec, "yahoo": {"sector": "Utilities", "industry": "y"}})["sector"] == ("Utilities", "provider_yf")


def test_an_unlisted_or_malformed_sic_code_gives_nothing():
    for inputs in ({"sec": {"sic": "7389", "status": "ok"}}, {"sic_store": "8880"}, {"sic_store": "abc"},
                   {"sec": {"sic": None}}, {"sec": {}}):
        res = run(inputs)
        assert vals(res) == ("Unclassified",) * 2 and srcs(res) == ("none",) * 2


def test_a_blank_check_company_reads_its_sic_sector_as_before_265():
    """6770 is in the explicit table (Financial Services). SPACs are an excluded type: no board reads their sector."""
    res = run({"sec": {"sic": "6770", "status": "ok"}}, typ="spac")
    assert res["sector"] == ("Financial Services", "sic_explicit")


def test_sic_applies_to_every_type_except_the_etp_which_has_no_classification():
    assert run({"sec": {"sic": "3841", "status": "ok"}}, typ="adr")["sector"] == ("Healthcare", "sic_explicit")
    assert run({"sec": {"sic": "3841", "status": "ok"}}, typ="etp")["sector"][1].startswith("type_rule(etp")


def test_sic_comes_before_the_type_rule():
    # a fund whose SIC is listed: the SIC sector stands, and the type rule's industry only fits Financial Services
    res = run({"sic_store": "6726"}, typ="cef")
    assert res["sector"] == ("Financial Services", "sic_explicit")
    assert res["industry"] == ("Asset Management", "type_rule")
    res = run({"sic_store": "7372"}, typ="bdc")
    assert res["sector"] == ("Technology", "sic_explicit") and res["industry"] == ("Unclassified", "none")
    # an unlisted SIC leaves the type rule in charge
    assert run({"sic_store": "7389"}, typ="bdc")["sector"] == ("Financial Services", "type_rule")


def test_every_step_names_itself_in_the_sector_source():
    cases = {
        "manual": run({}, manual={"sector": "Energy"}),
        "sp1500": run({"sp1500": {"sector": "Financials"}}),
        "provider_uw": run({"uw": {"sector": "Energy", "industry": "x"}}),
        "provider_yf": run({"yahoo": {"sector": "Energy", "industry": "x"}}),
        "sic_explicit": run({"sic_store": "3841"}),
        "type_rule": run({}, typ="cef"),
        "none": run({}),
    }
    assert {name: res["sector"][1] for name, res in cases.items()} == {n: n for n in cases}


def test_the_old_provider_corrections_are_gone():
    res = run({"uw": {"sector": "Technology", "industry": "Education & Training Services"}})
    assert res["sector"] == ("Technology", "provider_uw")           # was "Consumer Discretionary" via a correction
    with pytest.raises(TypeError):
        run({}, corrections=())


def test_etp_has_no_classification_at_all_even_with_stale_provider_values():
    res = run({"uw": {"sector": "Technology", "industry": "x"}, "yahoo": {"sector": "Energy", "industry": "y"}},
              typ="etp")
    assert vals(res) == ("Unclassified",) * 2
    assert all("etp" in s for s in srcs(res))


@pytest.mark.parametrize("typ", ["cef", "bdc"])
def test_cef_and_bdc_use_the_type_rule(typ):
    res = run({}, typ=typ)
    assert vals(res) == ("Financial Services", "Asset Management")
    assert srcs(res) == ("type_rule", "type_rule")


def test_bank_nonsec_uses_the_type_rule():
    res = run({}, typ="bank_nonsec")
    assert vals(res) == ("Financial Services", "Banks - Regional")
    assert srcs(res) == ("type_rule", "type_rule")


def test_type_rule_skipped_when_provider_sector_is_not_financial_services():
    res = run({"uw": {"sector": "Technology", "industry": "x"}}, typ="cef")
    assert res["sector"] == ("Technology", "provider_uw") and res["industry"] == ("x", "provider_uw")


def test_type_rule_fills_the_industry_below_a_financial_services_provider_sector_that_has_none():
    res = run({"uw": {"sector": "Financial Services", "industry": None}}, typ="bdc")
    assert res["sector"] == ("Financial Services", "provider_uw")
    assert res["industry"] == ("Asset Management", "type_rule")


def test_type_rule_keeps_a_provider_industry():
    res = run({"uw": {"sector": "Financial Services", "industry": "Asset Management - Income"}}, typ="bdc")
    assert res["industry"] == ("Asset Management - Income", "provider_uw")


def test_nothing_known_is_unclassified():
    res = run({})
    assert vals(res) == ("Unclassified",) * 2 and set(srcs(res)) == {"none"}


def test_manual_wins_per_level():
    uw = {"sector": "Technology", "industry": "Software"}
    res = run({"uw": uw}, manual={"industry": "Oil & Gas Drilling"})
    assert res["sector"] == ("Technology", "provider_uw") and res["industry"] == ("Oil & Gas Drilling", "manual")
    res = run({"uw": {"sector": "Energy", "industry": "Oil & Gas E&P"}}, manual={"sector": "Energy"})
    assert res["sector"] == ("Energy", "manual") and res["industry"] == ("Oil & Gas E&P", "provider_uw")


def test_a_manual_sector_is_not_mixed_with_the_industry_of_another_sector():
    """The provider's industry belongs to the provider's sector: under a different manual sector it is not used."""
    res = run({"uw": {"sector": "Technology", "industry": "Software"}}, manual={"sector": "Energy"})
    assert res["sector"] == ("Energy", "manual")
    assert res["industry"] == ("Unclassified", "none")
    # Yahoo agrees with the manual sector: its industry fits
    res = run({"uw": {"sector": "Technology", "industry": "Software"},
               "yahoo": {"sector": "Energy", "industry": "Oil & Gas Midstream"}}, manual={"sector": "Energy"})
    assert res["industry"] == ("Oil & Gas Midstream", "provider_yf")


def test_manual_full_path_and_over_etp():
    m = {"sector": "Energy", "industry": "Oil & Gas Drilling"}
    res = run({}, typ="etp", manual=m)
    assert vals(res) == ("Energy", "Oil & Gas Drilling") and set(srcs(res)) == {"manual"}
