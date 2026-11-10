"""#400 10-K package WP6: the profile's input is the pilot's B input (avo's decision C, 8 Nov 2026).

The pilot (claude-coordinator pilots/400-relatedness-evren, rounds 1-3) gave the writer Item 1 without the
sections that say nothing about the business (Available Information, Executive Officers, Human Capital), without
a tail of XBRL tags or number tables (ONEG, RCON), and beside it the filing's other customer / supplier /
concentration sentences (at most 40) and its XBRL major customers.
"""
from __future__ import annotations

from RevenueChaser.module_relatedness import engine, profile_v3

PROSE = "We make widgets for customers across many markets and we sell them through distributors worldwide.\n" * 30


def test_the_sections_that_say_nothing_about_the_business_are_dropped():
    text = ("Overview\n" + PROSE + "Human Capital\n" + "We have 5,000 employees in ten countries.\n" * 20
            + "Products\n" + PROSE + "Available Information\n" + "Our website is example.com.\n")
    out = engine.clean_input(text)
    assert "5,000 employees" not in out and "example.com" not in out and out.count("We make widgets") == 60


def test_a_tail_of_tags_and_tables_is_trimmed():
    tail = "xbrli:shares iso4217:HKD us-gaap:Revenue 0001234\n" * 20 + "¥ 1,234 ¥ 5,678 12.3 45,600\n" * 20
    assert engine.clean_input(PROSE + tail).rstrip().endswith("worldwide.")


def test_the_filings_other_customer_sentences_are_given_once_and_at_most_forty():
    item = "Apple accounted for 22% of our net sales in fiscal 2025, the largest single share."
    whole = (item + " " + "Our supplier TSMC is the sole source of our wafers and we depend on it entirely. " * 3
             + " ".join(f"Customer number {i} accounted for {i}% of our revenues in the year we report." for i in range(60)))
    got = engine.filing_sentences(whole, item)
    assert len(got) == 40 and not any(s.startswith("Apple") for s in got)
    assert sum("TSMC" in s for s in got) == 1


def test_the_prompt_carries_the_sentences_and_the_xbrl_customers():
    user = profile_v3.build_user("Acme", "text", {"filing_sentences": ["TSMC is our sole source of wafers."],
                                                  "xbrl_customers": ["Mc Kesson Corporation: 22.0% of revenue"]})
    assert "TSMC is our sole source" in user and "Mc Kesson Corporation: 22.0% of revenue" in user
