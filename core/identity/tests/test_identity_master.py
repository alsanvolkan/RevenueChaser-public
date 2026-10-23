"""The master-row builder (issue #85 Part 2; sector order and raw SIC columns #272): pure, never blank, asof moves only
on confirmation."""
from __future__ import annotations

import copy

from RevenueChaser.core.identity import master

T1 = "2026-09-01T00:00:00Z"
T2 = "2026-09-30T00:00:00Z"

COLUMNS = {"ticker", "cik", "cik_source", "exchange", "name", "name_source", "security_type", "type_source",
           "type_asof", "sector", "sector_source", "industry_group", "group_source", "industry", "industry_source",
           "sub_industry", "sub_source", "gics_asof", "updated_at",
           "figi", "composite_figi", "share_class_figi", "exch_code", "security_description", "figi_source",
           "sic", "sic_description", "sic_group2", "sic_source",
           "cusip", "isin", "cusip_source", "isin_source"}


def full(**over):
    d = {"name": "Nvidia Corp", "market_cap": 5e12, "cik": "1045810", "cik_source": "sec_exchange", "exchange": "Nasdaq",
         "sec": {"status": "ok", "name": "NVIDIA CORP", "forms": ["10-K", "10-Q"], "sic": "3674",
                 "sic_description": "Semiconductors & Related Devices", "shell_closed": False},
         "openfigi": {"security_type": "Common Stock", "security_type2": "Common Stock", "name": "NVIDIA CORP",
                      "figi": "BBG000BBJQV0", "composite_figi": "BBG000BBJQV0", "share_class_figi": "BBG001S5TZJ6",
                      "exch_code": "US", "security_description": "NVDA"},
         "nasdaq": {"is_etf": False, "security_name": "NVIDIA Corporation - Common Stock"},
         "uw": {"sector": "Technology", "industry": "Semiconductors", "security_type": "Common Stock",
                "market_cap": 5.1e12},
         "yahoo": None, "fdic": None}
    d.update(over)
    return d


def one(inputs, prev=None, now=T1, manual=None):
    rows, conf = master.build({"NVDA": inputs}, {"NVDA": prev} if prev else {}, manual=manual, now=now)
    assert len(rows) == 1
    return rows[0], conf


def test_row_has_every_security_master_column():
    row, conf = one(full())
    assert set(row) == COLUMNS
    assert row["ticker"] == "NVDA" and row["security_type"] == "common" and row["type_source"] == "openfigi"
    assert row["type_asof"] == T1 and row["gics_asof"] == T1 and row["updated_at"] == T1
    assert (row["sector"], row["sector_source"]) == ("Technology", "provider_uw")
    assert (row["industry"], row["industry_source"]) == ("Semiconductors", "provider_uw")
    # the two GICS-only levels are written empty: the columns stay (no schema bump), nothing fills them
    assert (row["industry_group"], row["group_source"], row["sub_industry"], row["sub_source"]) == (None,) * 4
    assert row["cik"] == "1045810" and row["exchange"] == "Nasdaq" and row["name"] == "Nvidia Corp"
    assert conf == []


FIGI_COLS = ("figi", "composite_figi", "share_class_figi", "exch_code", "security_description")


def figis(row):
    return tuple(row[c] for c in FIGI_COLS)


def test_the_figi_fields_come_from_the_openfigi_row_with_their_source():
    row, _ = one(full())
    assert figis(row) == ("BBG000BBJQV0", "BBG000BBJQV0", "BBG001S5TZJ6", "US", "NVDA")
    assert row["figi_source"] == "openfigi"


def test_a_ticker_openfigi_never_answered_has_empty_figi_fields_and_no_source():
    for no_answer in (None, {}):
        row, _ = one(full(openfigi=no_answer))
        assert figis(row) == (None,) * 5 and row["figi_source"] is None


def test_the_figi_fields_are_never_blanked_when_openfigi_gives_no_answer_this_run():
    prev, _ = one(full(), now=T1)
    for no_answer in (None, {}):                      # source down, or answered with no match
        row, _ = one(full(openfigi=no_answer), prev=prev, now=T2)
        assert figis(row) == figis(prev) and row["figi_source"] == "openfigi"


def test_a_new_openfigi_row_replaces_the_previous_figi_fields():
    prev, _ = one(full(), now=T1)
    new = dict(full()["openfigi"], composite_figi="BBG000NEW0", exch_code="UN")
    row, _ = one(full(openfigi=new), prev=prev, now=T2)
    assert row["composite_figi"] == "BBG000NEW0" and row["exch_code"] == "UN"
    assert row["figi"] == prev["figi"] and row["updated_at"] == T2


def test_an_openfigi_row_cached_before_the_extra_fields_existed_keeps_the_figi_it_has():
    old = {"security_type": "Common Stock", "security_type2": "Common Stock", "name": "NVIDIA CORP",
           "figi": "BBG000BBJQV0"}
    row, _ = one(full(openfigi=old))
    assert figis(row) == ("BBG000BBJQV0", None, None, None, None) and row["figi_source"] == "openfigi"


def test_the_figi_fields_do_not_change_the_type_or_the_sector():
    with_figi, _ = one(full())
    bare, _ = one(full(openfigi={"security_type": "Common Stock", "security_type2": "Common Stock",
                                 "name": "NVIDIA CORP"}))
    for col in ("security_type", "type_source", "sector", "sector_source", "industry", "industry_source"):
        assert with_figi[col] == bare[col], col


def test_columns_match_the_manifest():
    from RevenueChaser.core import rawstore
    cols = {c.split()[0] for c in rawstore.MANIFEST["security_master"]["columns"] if not c.startswith("PRIMARY")}
    assert cols == COLUMNS


def test_build_is_pure_and_returns_dicts():
    inputs, prev = {"NVDA": full()}, {}
    snap = copy.deepcopy(inputs)
    rows, conf = master.build(inputs, prev, now=T1)
    assert inputs == snap and prev == {}
    assert isinstance(rows, list) and isinstance(rows[0], dict) and isinstance(conf, list)


def test_conflicts_are_returned():
    _, conf = one(full(uw={"sector": "Technology", "industry": "x", "security_type": "ADR", "market_cap": 5e12}))
    assert [c for c in conf if c["field"] == "security_type:uw_issue_type"]
    assert all(c["ticker"] == "NVDA" for c in conf)


def test_cap_conflict_never_changes_a_value():
    row, conf = one(full(uw={"sector": "Technology", "industry": "Semiconductors", "security_type": "Common Stock",
                             "market_cap": 1e9}))
    assert [c for c in conf if c["kind"] == "cap"]
    clean, _ = one(full())
    assert {k: v for k, v in row.items() if k != "gics_asof"} == {k: v for k, v in clean.items() if k != "gics_asof"}


def test_never_blank_openfigi_missing_keeps_previous_value_source_and_asof():
    prev, _ = one(full(), now=T1)
    row, _ = one(full(openfigi=None), prev=prev, now=T2)
    assert row["security_type"] == "common" and row["type_source"] == "openfigi"
    assert row["type_asof"] == T1                       # not confirmed this run


def test_never_blank_uw_missing_keeps_previous_sector_and_industry():
    prev, _ = one(full(), now=T1)
    row, _ = one(full(uw=None), prev=prev, now=T2)
    assert row["sector"] == "Technology" and row["sector_source"] == "provider_uw"
    assert row["industry"] == "Semiconductors" and row["industry_source"] == "provider_uw"
    assert row["gics_asof"] == T1


def test_a_weaker_present_source_does_not_replace_a_kept_value():
    """UW is down, Yahoo answers: Yahoo ranks below UW, so the UW value stays until UW is back."""
    prev, _ = one(full(), now=T1)
    row, _ = one(full(uw=None, yahoo={"sector": "Energy", "industry": "Oil & Gas E&P"}), prev=prev, now=T2)
    assert (row["sector"], row["sector_source"]) == ("Technology", "provider_uw")


def test_a_pre_265_correction_source_is_still_not_a_source():
    """``provider_uw+correction`` died with the correction list: nothing present can confirm it, so it goes."""
    prev = {"ticker": "NVDA", "sector": "Information Technology", "sector_source": "provider_uw+correction",
            "industry": "Semiconductors", "industry_source": "provider_uw+correction", "gics_asof": T1,
            "updated_at": T1}
    row, _ = one(full(), prev=prev, now=T2)
    assert (row["sector"], row["sector_source"]) == ("Technology", "provider_uw")
    no_sic = {"status": "ok", "name": "X", "forms": ["10-K"]}
    row, _ = one(full(uw=None, sec=no_sic), prev=prev, now=T2)
    assert row["sector"] == "Unclassified" and row["sector_source"] == "none"


# -- the S&P 1500 list and the SIC fallback in the master (#272) -------------------------------------
LISTED = {"sector": "Industrials", "sub_industry": "Research & Consulting Services", "index": "sp500"}


def test_a_list_name_takes_the_lists_sector_and_uws_industry_text():
    row, _ = one(full(sp1500=LISTED))
    assert (row["sector"], row["sector_source"]) == ("Industrials", "sp1500")
    assert (row["industry"], row["industry_source"]) == ("Semiconductors", "provider_uw")
    assert (row["industry_group"], row["sub_industry"]) == (None, None), "the list's GICS levels are never written"


def test_a_name_off_the_list_is_unchanged_by_the_list_being_present():
    row, _ = one(full(sp1500={}))
    assert (row["sector"], row["sector_source"]) == ("Technology", "provider_uw")


def test_the_sic_fallback_decides_a_sector_when_nothing_above_has_one():
    sec = {"status": "ok", "name": "X", "forms": ["10-K"], "sic": "3841", "sic_description": "Surgical & Medical"}
    row, _ = one(full(uw={}, yahoo={}, sp1500={}, sec=sec))
    assert (row["sector"], row["sector_source"]) == ("Healthcare", "sic_explicit")
    assert (row["industry"], row["industry_source"]) == ("Unclassified", "none")


def test_a_previous_list_sector_is_kept_while_the_list_is_missing_this_run():
    prev, _ = one(full(sp1500=LISTED), now=T1)
    row, _ = one(full(), prev=prev, now=T2)             # no sp1500 key: the list gave no answer this run
    assert (row["sector"], row["sector_source"]) == ("Industrials", "sp1500")
    row, _ = one(full(sp1500=None), prev=prev, now=T2)
    assert (row["sector"], row["sector_source"]) == ("Industrials", "sp1500")
    row, _ = one(full(uw=None, sp1500=None), prev=prev, now=T2)    # nothing confirmed either level this run
    assert (row["sector"], row["sector_source"], row["gics_asof"]) == ("Industrials", "sp1500", T1)


def test_a_list_that_answered_with_no_row_for_the_ticker_lets_the_provider_sector_back_in():
    prev, _ = one(full(sp1500=LISTED), now=T1)
    row, _ = one(full(sp1500={}), prev=prev, now=T2)    # healthy list, ticker dropped out of the indexes
    assert (row["sector"], row["sector_source"]) == ("Technology", "provider_uw") and row["gics_asof"] == T2


def test_a_changed_list_sector_replaces_the_previous_one():
    prev, _ = one(full(sp1500=LISTED), now=T1)
    row, _ = one(full(sp1500=dict(LISTED, sector="Financials")), prev=prev, now=T2)
    assert (row["sector"], row["sector_source"]) == ("Financial Services", "sp1500") and row["updated_at"] == T2


def test_a_previous_sic_sector_is_kept_while_the_submissions_source_is_missing_and_nothing_better_answers():
    sec = {"status": "ok", "name": "X", "forms": ["10-K"], "sic": "3841", "sic_description": "Surgical"}
    prev, _ = one(full(uw={}, yahoo={}, sec=sec), now=T1)
    assert prev["sector_source"] == "sic_explicit"
    row, _ = one(full(uw={}, yahoo={}, sec=None), prev=prev, now=T2)
    assert (row["sector"], row["sector_source"], row["gics_asof"]) == ("Healthcare", "sic_explicit", T1)
    # UW answers again: it outranks SIC
    row, _ = one(full(yahoo={}, sec=None), prev=prev, now=T2)
    assert (row["sector"], row["sector_source"]) == ("Technology", "provider_uw")


def test_a_weaker_present_source_does_not_replace_a_kept_list_sector():
    prev, _ = one(full(sp1500=LISTED), now=T1)
    row, _ = one(full(uw=None, yahoo={"sector": "Energy", "industry": "Oil & Gas E&P"}), prev=prev, now=T2)
    assert (row["sector"], row["sector_source"]) == ("Industrials", "sp1500")


# -- the raw SIC columns (#272) ---------------------------------------------------------------------
SIC_COLS = ("sic", "sic_description", "sic_group2", "sic_source")


def sics(row):
    return tuple(row[c] for c in SIC_COLS)


def test_the_raw_sic_comes_from_the_submissions_row_with_its_text_group_and_source():
    row, _ = one(full())
    assert sics(row) == ("3674", "Semiconductors & Related Devices", "36", "sec_submissions")


def test_the_store_code_is_the_fallback_when_the_submissions_row_has_none():
    for sec in (None, {"status": "ok", "name": "X", "forms": ["10-K"], "sic": ""}):
        row, _ = one(full(sec=sec, sic_store="2834", sic_store_description="Pharmaceutical Preparations"))
        assert sics(row) == ("2834", "Pharmaceutical Preparations", "28", "sec_company_sic")


def test_a_code_without_text_is_stored_without_text_and_a_matching_store_text_fills_it():
    bare = {"status": "ok", "name": "X", "forms": ["10-K"], "sic": "2834", "sic_description": ""}
    row, _ = one(full(sec=bare))
    assert sics(row) == ("2834", None, "28", "sec_submissions")
    row, _ = one(full(sec=bare, sic_store="2834", sic_store_description="Pharmaceutical Preparations"))
    assert sics(row) == ("2834", "Pharmaceutical Preparations", "28", "sec_submissions")
    row, _ = one(full(sec=bare, sic_store="3841", sic_store_description="Surgical"))
    assert row["sic_description"] is None, "the store's text belongs to its own code"


def test_a_short_code_is_zero_padded_and_its_group_has_two_digits():
    row, _ = one(full(sec={"status": "ok", "name": "X", "forms": ["10-K"], "sic": "100", "sic_description": "Crops"}))
    assert sics(row) == ("0100", "Crops", "01", "sec_submissions")


def test_a_ticker_with_no_sic_anywhere_has_empty_sic_columns():
    row, _ = one(full(sec={"status": "ok", "name": "X", "forms": ["N-CSR"]}))
    assert sics(row) == (None,) * 4
    row, _ = one({"name": None})
    assert sics(row) == (None,) * 4


def test_the_raw_sic_is_never_blanked_when_both_caches_give_no_code_this_run():
    prev, _ = one(full(), now=T1)
    for no_answer in (None, {"status": "ok", "name": "X", "forms": []}):
        row, _ = one(full(sec=no_answer), prev=prev, now=T2)
        assert sics(row) == sics(prev)


def test_a_new_code_replaces_the_previous_sic_group():
    prev, _ = one(full(), now=T1)
    new = {"status": "ok", "name": "X", "forms": ["10-K"], "sic": "7372", "sic_description": "Prepackaged Software"}
    row, _ = one(full(sec=new), prev=prev, now=T2)
    assert sics(row) == ("7372", "Prepackaged Software", "73", "sec_submissions") and row["updated_at"] == T2


def test_a_new_code_without_text_does_not_inherit_the_previous_codes_text_but_the_same_code_keeps_its_own():
    prev, _ = one(full(), now=T1)                 # 3674, "Semiconductors & Related Devices"
    other = {"status": "ok", "name": "X", "forms": ["10-K"], "sic": "7372", "sic_description": ""}
    row, _ = one(full(sec=other), prev=prev, now=T2)
    assert sics(row) == ("7372", None, "73", "sec_submissions"), "the text of 3674 does not belong to 7372"
    same = {"status": "ok", "name": "X", "forms": ["10-K"], "sic": "3674", "sic_description": ""}
    row, _ = one(full(sec=same), prev=prev, now=T2)
    assert sics(row) == sics(prev), "the same code without text this run keeps the text it had"


def test_the_raw_sic_does_not_depend_on_the_sector_that_was_chosen():
    """A name whose sector comes from UW still carries its SIC; an unlisted code is stored all the same."""
    row, _ = one(full(sec={"status": "ok", "name": "X", "forms": ["10-K"], "sic": "7389", "sic_description": "Misc"}))
    assert row["sector_source"] == "provider_uw" and sics(row) == ("7389", "Misc", "73", "sec_submissions")


def test_a_row_with_old_gics_group_and_sub_values_gets_them_cleared():
    prev, _ = one(full(), now=T1)
    prev = dict(prev, industry_group="Semiconductors & Semiconductor Equipment", group_source="sp1500",
                sub_industry="Semiconductors", sub_source="sp1500")
    row, _ = one(full(), prev=prev, now=T2)
    assert (row["industry_group"], row["group_source"], row["sub_industry"], row["sub_source"]) == (None,) * 4


def test_never_blank_keeps_previous_cik_exchange_and_name():
    prev, _ = one(full(), now=T1)
    row, _ = one(full(cik=None, cik_source=None, exchange=None, name=None, sec=None), prev=prev, now=T2)
    assert (row["cik"], row["cik_source"], row["exchange"], row["name"]) == \
        ("1045810", "sec_exchange", "Nasdaq", "Nvidia Corp")


def test_asof_moves_when_the_source_confirms():
    prev, _ = one(full(), now=T1)
    row, _ = one(full(), prev=prev, now=T2)
    assert row["type_asof"] == T2 and row["gics_asof"] == T2


def test_positive_evidence_from_a_present_source_beats_a_frozen_value():
    prev, _ = one(full(), now=T1)
    bdc = full(openfigi=None, sec={"status": "ok", "name": "X", "forms": ["10-K", "N-2"]})
    row, _ = one(bdc, prev=prev, now=T2)
    assert row["security_type"] == "bdc" and row["type_asof"] == T2


def test_changed_value_from_the_same_source_updates():
    prev, _ = one(full(), now=T1)
    row, _ = one(full(openfigi={"security_type": "REIT", "security_type2": None, "name": "x"}), prev=prev, now=T2)
    assert row["security_type"] == "reit" and row["type_asof"] == T2


def test_updated_at_only_moves_when_something_changed():
    prev, _ = one(full(), now=T1)
    row, _ = one(full(openfigi=None), prev=prev, now=T2)
    assert row["updated_at"] == T1
    row2, _ = one(full(openfigi={"security_type": "REIT", "security_type2": None, "name": "x"}), prev=prev, now=T2)
    assert row2["updated_at"] == T2


def test_no_previous_and_no_sources_yields_unknown_unclassified_not_none():
    row, _ = one({"name": None})
    assert row["security_type"] == "unknown" and row["type_source"] == "none"
    assert row["sector"] == "Unclassified" and row["sector_source"] == "none"


def test_manual_wins_over_everything():
    manual = {"NVDA": {"security_type": "etp", "sector": "Energy", "industry": "Oil & Gas Drilling"}}
    row, _ = one(full(), manual=manual)
    assert row["security_type"] == "etp" and row["type_source"] == "manual"
    assert (row["sector"], row["sector_source"], row["industry"]) == ("Energy", "manual", "Oil & Gas Drilling")


def test_previous_unclassified_is_replaced_when_a_source_appears():
    prev, _ = one({"name": "x"}, now=T1)
    row, _ = one(full(), prev=prev, now=T2)
    assert row["sector"] == "Technology" and row["security_type"] == "common"


def test_tickers_without_inputs_keep_their_previous_row():
    prev, _ = one(full(), now=T1)
    rows, _ = master.build({}, {"NVDA": prev}, now=T2)
    assert rows == []        # build only emits rows for tickers it was given; the caller keeps the rest
