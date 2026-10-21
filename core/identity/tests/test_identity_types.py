"""Security-type rules (issue #85 Part 2): R1's cases as plain-dict tests."""
from __future__ import annotations

import pytest

from RevenueChaser.core.identity import types as T


def inp(name="Some Co", figi=("Common Stock", "Common Stock"), forms=None, sic="", shell_closed=False,
        etf=None, uw=None, fdic=None, sec_name=None, nasdaq_name=None, sec=True):
    return {"name": name,
            "openfigi": {"security_type": figi[0], "security_type2": figi[1]} if figi else None,
            "sec": ({"status": "ok", "forms": forms if forms is not None else ["10-K", "10-Q"], "sic": sic,
                     "shell_closed": shell_closed, "name": sec_name or name} if sec else None),
            "nasdaq": {"is_etf": etf, "security_name": nasdaq_name or name} if etf is not None else None,
            "uw": {"security_type": uw} if uw else None,
            "fdic": fdic}


def test_type_sets_are_the_user_decision():
    assert T.OPERATING_TYPES == {"common", "adr", "reit", "bank_nonsec", "bdc", "mlp", "royalty_trust"}
    assert T.EXCLUDED_TYPES == {"cef", "etp", "spac", "preferred", "unit", "warrant"}
    assert "bdc" in T.OPERATING_TYPES and "spac" in T.EXCLUDED_TYPES
    assert not (T.OPERATING_TYPES & T.EXCLUDED_TYPES)


@pytest.mark.parametrize("st,st2,want", [
    ("Common Stock", "Common Stock", "common"), ("ADR", None, "adr"), ("NY Reg Shrs", "Depositary Receipt", "adr"),
    ("GDR", None, "adr"), ("Closed-End Fund", "Mutual Fund", "cef"), ("ETP", "Mutual Fund", "etp"),
    ("REIT", None, "reit"), ("MLP", None, "mlp"), ("Ltd Part", None, "mlp"), ("Preference", None, "preferred"),
    ("Unit", None, "unit"), ("Royalty Trst", None, "royalty_trust"), ("Equity WRT", None, "warrant"),
    ("Weird", None, "other"), (None, None, None), ("", "", None)])
def test_openfigi_mapping(st, st2, want):
    assert T.openfigi_type(st, st2) == want


def test_sec_class():
    assert T.sec_class(["N-54A"]) == "bdc"
    assert T.sec_class(["10-K", "N-2"]) == "bdc"
    assert T.sec_class(["10-Q", "N-2"]) == "domestic_filer"     # N-2 without a 10-K is not a BDC
    assert T.sec_class(["N-CSR"]) == "cef"
    assert T.sec_class(["20-F"]) == "foreign_filer"
    assert T.sec_class(["S-1"]) == "new_registrant"
    assert T.sec_class(["8-K"]) == "no_periodic_filings"


def test_lazr_is_etp_from_nasdaq_flag():
    assert T.classify_type("LAZR", inp(figi=("ETP", "Mutual Fund"), forms=[], etf=True)) == ("etp", "nasdaq_etf_flag")


def test_numbered_name_fund_is_cef():
    got = T.classify_type("UTG", inp(name="1234", figi=("Closed-End Fund", "Mutual Fund"), forms=["N-CSR", "NPORT-P"]))
    assert got == ("cef", "sec_forms(N-CSR/N-CSRS/NPORT-P)")


def test_cef_forms_do_not_override_openfigi_etp():
    assert T.classify_type("X", inp(figi=("ETP", None), forms=["N-CSR"]))[0] == "etp"


def test_arcc_is_bdc():
    assert T.classify_type("ARCC", inp(forms=["10-K", "10-Q", "N-2"])) == ("bdc", "sec_forms(N-54A or 10-K+N-2)")


def test_tcpc_like_filer_with_n54a_and_ncsr_is_bdc_not_cef():
    assert T.classify_type("TCPC", inp(forms=["N-54A", "N-CSR", "10-K"])) == ("bdc", "sec_forms(N-54A or 10-K+N-2)")


def test_ozk_like_bank_without_sec_filings_is_bank_nonsec():
    f = {"match": {"NAME": "Bank OZK", "CERT": 110, "score": 1.0}}
    got = T.classify_type("OZK", inp(forms=[], sec=False, fdic=f))
    assert got[0] == "bank_nonsec" and got[1].startswith("fdic_name_match(")


def test_fdic_match_ignored_when_sec_periodic_filer():
    f = {"match": {"NAME": "Bank OZK"}}
    assert T.classify_type("X", inp(forms=["10-K"], fdic=f))[0] == "common"


def test_fdic_only_for_common_like():
    f = {"match": {"NAME": "Bank"}}
    assert T.classify_type("X", inp(figi=("ADR", None), forms=[], fdic=f))[0] == "adr"


def test_spac_by_sic_6770():
    assert T.classify_type("SP", inp(name="Blank Check Ltd", sic="6770")) == ("spac", "sec_sic_6770")


def test_spac_by_name_rule():
    got = T.classify_type("SP", inp(name="Foo Acquisition Corp", sic="6199", forms=["10-K"]))
    assert got == ("spac", "name_rule(Acquisition/Merger Corp)")


def test_spac_shell_closed_becomes_common():
    got = T.classify_type("SP", inp(name="Foo Acquisition Corp", sic="6770", shell_closed=True))
    assert got[0] == "common"


def test_spac_rule_only_replaces_common_like_types():
    assert T.classify_type("SP", inp(figi=("ADR", None), sic="6770"))[0] == "adr"


def test_asml_is_adr():
    assert T.classify_type("ASML", inp(figi=("NY Reg Shrs", "Depositary Receipt"), forms=["20-F"]))[0] == "adr"


def test_azn_common_from_openfigi_even_when_uw_says_adr():
    assert T.classify_type("AZN", inp(uw="ADR", forms=["20-F"])) == ("common", "openfigi")


def test_mlp_from_openfigi():
    assert T.classify_type("EPD", inp(figi=("MLP", None), uw="Unit"))[0] == "mlp"


def test_preferred_and_warrant_name_rules_when_nothing_else_answers():
    assert T.classify_type("ABC-PA", inp(figi=None, forms=[], sec=False, name="ABC Corp"))[0] == "preferred"
    assert T.classify_type("ABCW", inp(figi=None, forms=[], sec=False, name="ABC Corp Warrants"))[0] == "warrant"


def test_uw_issue_type_is_the_last_fallback():
    assert T.classify_type("X", inp(figi=None, uw="ADR", forms=[], sec=False)) == ("adr", "uw_issue_type")
    assert T.classify_type("X", inp(figi=None, uw="Common Stock", forms=[], sec=False)) == ("common", "uw_issue_type")
    assert T.classify_type("X", inp(figi=None, uw="ETF", forms=[], sec=False)) == ("etp", "uw_issue_type")


def test_nothing_known_is_unknown():
    assert T.classify_type("X", inp(figi=None, forms=[], sec=False)) == ("unknown", "none")


def test_openfigi_source_label():
    assert T.classify_type("AAPL", inp(forms=["10-K"])) == ("common", "openfigi")


def test_spac_signals():
    assert T.spac_signals("SP", inp(name="Foo Acquisition Corp", sic="6770", shell_closed=True)) == (True, True, True)
