"""The Insider % rule: the security key, the divisor match, the split direction and the cell's states (#646 PR 4).

The key and the divisor are what four cross-reviews (Grok and Claude, 11 Nov 2026) kept sending back, so the
cases below are theirs: the titles that must not collapse, the titles that must not split, the class that has no
figure of its own, and the sum that is mostly years old. Pure functions and one assembled cell; no store.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.rcdata import read

TODAY = date(2026, 11, 11)


# ── the security key ──

def test_a_preferred_never_reaches_the_key():
    """Round 2's blocker: ``Class A Preferred Stock`` keyed as ``class a`` replaced a real common balance."""
    assert read.security_key("Class A Preferred Stock") is None
    assert read.security_key("7.875% Series D Preferred Stock") is None
    assert read.security_key("Auction Preferred Stock") is None


def test_the_other_securities_the_column_is_not_about():
    for title in ("Employee Stock Option (right to buy)", "Warrant to Purchase Common Stock",
                  "Restricted Stock Units", "Phantom Stock Units", "6.25% Senior Notes"):
        assert read.security_key(title) is None, title


def test_one_class_in_its_many_spellings_is_one_key():
    """The 12 commonest spellings of plain common in the live corpus must meet on one key."""
    keys = {read.security_key(t) for t in (
        "Common Stock", "COMMON STOCK", "Common stock", "Common Shares", "Ordinary Shares", "Common",
        "Common Stock, par value $0.01 per share", "Common Stock, $0.01 par value",
        "Common Stock, par value $0.001 per share", "Capital Stock",
        # Par value phrased the other way round, and the bare forms the first draft of the test dropped:
        # measured over the whole corpus, these are 10,881 rows of plain common (11 Nov 2026).
        "Common Stock, no par value", "Common Stock, No Par Value", "Common shares without par value",
        "Registered Shares")}
    assert len(keys) == 1, keys
    assert keys.pop() == ("", "", "", "")


def test_a_class_letter_is_kept_wherever_it_sits_in_the_title():
    assert read.security_key("Class A Common Stock") == read.security_key("Class A common stock")
    assert read.security_key("Common Stock, Class A")[0] == "a"
    assert read.security_key("Class A Common Stock")[0] == "a"
    assert read.security_key("Class B Common Stock")[0] == "b"
    assert read.security_key("Class A Common Stock") != read.security_key("Class B Common Stock")


def test_a_series_is_kept_and_is_not_always_a_letter():
    assert read.security_key("Series A Common Stock")[1] == "a"
    assert read.security_key("Series 1 Common Stock")[1] == "1"
    assert read.security_key("Series 1 Common Stock, $0.001 par value") == read.security_key("Series 1 Common Stock")
    assert read.security_key("Series A Common Stock") != read.security_key("Series C Common Stock")


def test_a_class_suffix_stays_apart_from_the_bare_letter():
    assert read.security_key("Class A-1 Common Stock")[0] == "a-1"
    assert read.security_key("Class A-1 Common Stock") != read.security_key("Class A Common Stock")


def test_non_voting_common_does_not_merge_with_voting_common():
    assert read.security_key("Non-Voting Common Stock")[2] == "nonvoting"
    assert read.security_key("Nonvoting Common Stock") == read.security_key("Non-Voting Common Stock")
    assert read.security_key("Non-Voting Common Stock") != read.security_key("Common Stock")


def test_a_tracking_stock_stays_apart_from_the_plain_series_of_the_same_letter():
    """Round 4: a tracking stock is a different security, on the class branch as well as the series branch."""
    liberty = read.security_key("Series C Liberty SiriusXM Common Stock")
    formula = read.security_key("Series C Liberty Formula One Common Stock")
    plain = read.security_key("Series C Common Stock")
    assert liberty != plain and formula != plain and liberty != formula
    assert read.security_key("Class C Liberty SiriusXM Common Stock") != read.security_key("Class C Common Stock")


def test_the_canadian_voting_share_forms_are_common_equity_and_differ_by_their_vote():
    """One vote against ten: subordinate and multiple voting shares are not the same security, and neither is
    plain common. The voting form is part of the key, not a word the remainder swallows."""
    assert read.security_key("Subordinate Voting Shares")[2] == "subordinate"
    assert read.security_key("Multiple Voting Shares")[2] == "multiple"
    assert read.security_key("Subordinate Voting Shares") != read.security_key("Multiple Voting Shares")
    assert read.security_key("Subordinate Voting Shares") != read.security_key("Common Stock")


def test_a_class_marker_with_bare_shares_is_common_equity():
    """``Class A Shares`` and ``Registered Shares`` are 5,026 rows the first common test missed (11 Nov 2026)."""
    assert read.security_key("Class A Shares")[0] == "a"
    assert read.security_key("Registered Shares") is not None


def test_a_depositary_share_is_common_equity_with_its_own_key():
    """``shares_class`` holds an ADR issuer's count in ordinary shares, not in ADSs, so an ADS may never be
    divided by it: it gets a key of its own and matches only an ADR shares row."""
    ads = read.security_key("American Depositary Shares")
    assert ads is not None and ads[3].startswith("ads")
    assert ads != read.security_key("Ordinary Shares")


# ── the divisor match ──

def test_a_cover_tag_maps_to_the_class_it_divides():
    assert read._member_key("us-gaap:CommonClassAMember") == "a"
    assert read._member_key("us-gaap:CommonClassBMember") == "b"
    assert read._member_key("us-gaap:CommonStockMember") == ""
    assert read._member_key(None) == "" and read._member_key("") == ""
    assert read._member_key("us-gaap:NonvotingCommonStockMember") == "nonvoting"
    assert read._member_key("dei:AdrMember") == "ads"


def test_a_cover_tag_this_rule_does_not_know_matches_nothing():
    """Better no number than a common holding divided by a preferred class's count."""
    assert read._member_key("us-gaap:SeriesAPreferredStockMember") is None


# ── the split direction ──

def test_shares_are_divided_by_the_split_factor_so_a_two_for_one_doubles_them():
    periods = [{"valid_from": date(2020, 1, 1), "valid_to": date(2024, 1, 1), "split_factor": 0.5},
               {"valid_from": date(2024, 1, 1), "valid_to": None, "split_factor": 1.0}]
    assert read._adjust(100.0, date(2021, 6, 1), periods) == 200.0
    assert read._adjust(100.0, date(2025, 6, 1), periods) == 100.0


def test_a_day_no_factor_period_covers_is_not_adjusted_at_a_guess():
    periods = [{"valid_from": date(2020, 1, 1), "valid_to": date(2024, 1, 1), "split_factor": 0.5}]
    assert read._adjust(100.0, date(1998, 3, 2), periods) is None
    assert read._adjust(100.0, date(1998, 3, 2), []) == 100.0        # a security with no action at all


# ── the cell ──

def _position(owner: str, shares: float, when: date, key=("", "", "", "")) -> dict:
    return {"owner": owner, "key": key,
            "row": {"shares_after": shares, "transaction_date": when, "filed": when, "accession": "a", "line": 0}}


def _cell(positions, divisors, today=TODAY):
    return read._insider_cell("AAA", positions, "s1", {}, {"s1": divisors}, today, 0)


def test_a_recent_sum_over_its_own_class_is_ok():
    got = _cell([_position("o1", 1_000_000.0, date(2026, 6, 1)),
                 _position("o2", 500_000.0, date(2026, 5, 1))], {"": 10_000_000.0})
    assert got["state"] == "ok" and round(got["pct"], 2) == 15.0
    assert got["owners"] == 2 and got["owners_recent"] == 2


def test_a_sum_that_is_mostly_years_old_is_partial_and_is_not_dropped():
    """82.6% of the reported shares are older than two years, so a cut would delete most of the number."""
    got = _cell([_position("o1", 900_000.0, date(2018, 3, 1)),
                 _position("o2", 100_000.0, date(2026, 6, 1))], {"": 10_000_000.0})
    assert got["state"] == "partial" and round(got["pct"], 2) == 10.0
    assert got["owners"] == 2 and got["owners_recent"] == 1 and round(got["stale_share"], 2) == 0.9


def test_a_sum_over_100_percent_is_shown_and_marked_not_capped_and_not_blanked():
    got = _cell([_position("o1", 2_000_000.0, date(2026, 6, 1))], {"": 1_000_000.0})
    assert got["state"] == "inconsistent" and got["pct"] > 100.0


def test_a_class_with_no_figure_of_its_own_leaves_the_ticker_without_a_number():
    """The matched subset must never print as the company's figure (round 4)."""
    a = _position("o1", 1_000_000.0, date(2026, 6, 1), key=("a", "", "", ""))
    b = _position("o2", 1_000_000.0, date(2026, 6, 1), key=("b", "", "", ""))
    got = _cell([a, b], {"a": 10_000_000.0})                 # class B has no cover row
    assert got["pct"] is None and got["state"] == "unmatched class"
    assert _cell([a, b], {"a": 10_000_000.0, "b": 5_000_000.0})["state"] == "ok"


def test_the_unlabelled_cover_figure_serves_one_kept_class_and_never_one_of_several():
    one = _position("o1", 1_000_000.0, date(2026, 6, 1), key=("a", "", "", ""))
    assert _cell([one], {"": 10_000_000.0})["state"] == "ok"          # a single class may use it
    other = _position("o2", 1_000_000.0, date(2026, 6, 1), key=("b", "", "", ""))
    assert _cell([one, other], {"": 10_000_000.0})["state"] == "unmatched class"


def test_one_issuer_whose_titles_disagree_gets_no_number():
    """Measured over the corpus, a leftover word is as often the issuer's own name (``Disney Common Stock``) as
    a tracking stock (``Series C Liberty SiriusXM``). Neither merging nor splitting is safe, so the cell says so."""
    plain = _position("o1", 1_000_000.0, date(2026, 6, 1), key=read.security_key("Common Stock"))
    named = _position("o2", 1_000_000.0, date(2026, 6, 1), key=read.security_key("Disney Common Stock"))
    got = _cell([plain, named], {"": 10_000_000.0})
    assert got["pct"] is None and got["state"] == "ambiguous titles"
    assert _cell([plain, _position("o2", 1.0, date(2026, 6, 1))], {"": 10_000_000.0})["state"] == "ok"


def test_no_qualifying_position_is_a_reason_never_a_zero():
    got = read._insider_cell("AAA", [], "s1", {}, {}, TODAY, 3)
    assert got["pct"] is None and got["state"] == "no positions" and got["other_securities"] == 3
