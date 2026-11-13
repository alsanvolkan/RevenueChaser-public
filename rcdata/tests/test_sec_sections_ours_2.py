"""#755: ``ours-2``, the five defects of the plan's section 5 fixed, and the boards left on ``ours-1``.

Each test is named after the filing the defect was measured on, and every one of them is a **difference between
the two versions on the same input**: the input is the thing that was really seen, ``ours-1``'s answer is what the
census recorded, and ``ours-2``'s is what this issue promises. Three of the inputs are taken from
``sec_sections_cases.py``, because the move's golden file records those defects rather than hiding them
(``split_word_item_1a``, ``over_the_cap``, ``short_body``); taking them from there keeps one copy of each and
lets the golden go on proving that ``ours-1`` did not move. The PCVX test builds its own text instead: the shared
``item_1_01_in_exhibit_list`` case puts the sub-item *before* Item 1, where it tests nothing about ending a
slice, and the defect was a sub-item inside the body.

**What these tests cannot do.** The filings themselves are not in this tree and no test fetches one -- an SEC
request needs the owner's word. So the inputs are the shapes the census describes, built here, and a passing run
says the rule now does what #755 asked of it, not that the real VFF filing cuts correctly. That is #763's
hand-checked set, and the first full cut is what measures it.

Red on the merge-base: ``sec_sections.OURS_2`` does not exist there, so every test below fails at import or at
the first ``version=OURS_2``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from RevenueChaser.module_value_chain import splitter
from RevenueChaser.rcdata import sec_sections

from .sec_sections_cases import BODY, CASES, TOC

OURS_1, OURS_2 = sec_sections.OURS_1, sec_sections.OURS_2
_BY_NAME = {name: (text, form) for name, text, form in CASES}


def _cut(name_or_text: str, version: str, form: str = "10-K"):
    """``(item, quality)`` of a shared case by name, or of a string given here."""
    text, form = _BY_NAME.get(name_or_text, (name_or_text, form))
    return sec_sections.cut_item(text, form, version=version)


# ── the five defects ───────────────────────────────────────────────────────────────────────────────────────────
def test_vff_the_split_word_in_item_itself_no_longer_runs_the_slice_into_the_risk_factors():
    """**Defect 1.** ``ours-1`` spells every heading word with :func:`sec_sections.spaced` -- the filer's HTML
    breaks words over tags -- except the word ``item``. So VFF's "ITE M 1A. Risk Factors" is not a stop heading,
    Item 1A is missed, and the business slice runs on into the risk factors: 111,000 characters of them in the
    filing the census read.
    """
    text, form = _BY_NAME["split_word_item_1a"]
    old_item, old_quality = sec_sections.cut_item(text, form, version=OURS_1)
    new_item, new_quality = sec_sections.cut_item(text, form, version=OURS_2)
    assert old_quality == "overrun" and old_item.endswith("Risky.")
    assert new_quality == "ok", "the stop heading is a heading however the filer broke the word"
    assert "Risky." not in new_item and "ITE M 1A" not in new_item
    assert len(new_item) < len(old_item)


def test_vff_a_slice_that_swallowed_the_next_item_s_title_is_overrun_and_not_capped():
    """**Defect 2.** The label, which is what a later reader acts on. ``ours-1`` can only say "no stop matched",
    so a slice that did reach a stop -- a much later one -- is labelled by its length instead: ``capped``, which
    reads as "the start of a very long section" when it is really the wrong section. ``ours-2`` looks at what the
    slice holds: another item's own title line means ``overrun``, whatever ended it.
    """
    # A short business section, with Item 1A's heading closer to the start than ``MIN_ITEM_CHARS``. Both rules
    # look for the closing heading from there on, so both walk past this one and end at Item 2 -- and the slice
    # holds the whole of the risk factors with their heading still in it.
    swallowed = ("Item 1. Business\n" + "We make widgets. " * 30 + "\nItem 1A. Risk Factors\n" + "Risky. " * 600
                 + "\nItem 2. Properties\n" + BODY)
    item, quality = sec_sections.cut_item(swallowed, "10-K", version=OURS_2)
    assert quality == "overrun", "a stop was found, but only after the next item's own heading went by"
    assert "Item 1A. Risk Factors" in item and "Risky." in item, \
        "the text is kept -- it is the label that was wrong"
    old_item, old_quality = sec_sections.cut_item(swallowed, "10-K", version=OURS_1)
    assert old_quality == "ok" and old_item == item, \
        "ours-1 called the same cut a clean one, because a stop did eventually match"


def test_pcvx_a_sub_item_in_an_exhibit_list_does_not_end_item_1():
    """**Defect 3.** An exhibit list inside Item 1 quotes an 8-K's items -- "Item 1.01 Entry into a Material
    Definitive Agreement", "Item 2.01 Completion of Acquisition or Disposition of Assets". ``ours-1``'s end
    pattern reads "Item 2.01" as Item 2, because ``2`` is followed by a word boundary, and cuts the business
    section off at the exhibit list: 5,305 characters of the 10,599 the filing holds, labelled ``ok``.
    """
    exhibits = ("Item 1. Business\n" + BODY
                + "\nIncorporated by reference to the following reports:\n"
                + "Item 1.01 Entry into a Material Definitive Agreement\n"
                + "Item 2.01 Completion of Acquisition or Disposition of Assets\n"
                + "Item 5.02 Departure of Directors\n" + BODY
                + "\nItem 2. Properties\n" + BODY)
    old_item, old_quality = sec_sections.cut_item(exhibits, "10-K", version=OURS_1)
    new_item, new_quality = sec_sections.cut_item(exhibits, "10-K", version=OURS_2)
    assert (old_quality, new_quality) == ("ok", "ok"), "both call it a clean cut; only one of them is right"
    assert len(new_item) > len(old_item)
    assert new_item.rstrip().endswith(BODY.rstrip()[-40:]), "the body runs to the real Item 2"
    assert "Item 5.02" in new_item, "the exhibit list is part of the business section, not the end of it"


@pytest.mark.parametrize("sub_item,real_item", [
    ("Item 1.01 Entry into a Material Definitive Agreement", False),
    ("Item 2.01 Completion of Acquisition or Disposition of Assets", False),
    ("Item 5.02 Departure of Directors or Certain Officers", False),
    ("Item 9.01 Financial Statements and Exhibits", False),
    ("Item 1A. Risk Factors", True),
    ("Item 2. Properties", True),
    ("Item 10 Directors and Executive Officers", True),
])
def test_which_item_headings_end_a_section_and_which_are_a_filing_s_sub_items(sub_item, real_item):
    """The guard itself, on both sides. A number followed by a digit is a different item; a number followed by a
    letter, a full stop or nothing is the item. ``Item 10`` has to keep working: the guard reads what follows the
    number, not how many digits it has.
    """
    assert bool(sec_sections._LINE_END_2.search(f"\n{sub_item}\n")) is real_item


def test_six_of_twenty_item_8_bodies_are_a_pointer_and_not_a_short_cut():
    """**Defect 4.** A pointer item's body is a cross-reference: the prose is somewhere else in the filing, or in
    an exhibit. ``ours-1`` labels it ``short``, which reads as a cut that went wrong and invites another run to
    try again; six of the twenty Item 8 slices in the census were this. ``pointer`` says the filing holds no
    prose there, so there is nothing for a later run to find.
    """
    pointers = [
        "The information required by this item is incorporated herein by reference.",
        "See the financial statements beginning on page F-1 of this report.",
        "Reference is made to the consolidated financial statements included elsewhere in this report.",
        "Our consolidated financial statements are set forth beginning on page 45.",
    ]
    for body in pointers:
        text = f"Item 1. Business\n{body}\nItem 1A. Risk Factors\nRisky.\n"
        assert sec_sections.cut_item(text, "10-K", version=OURS_1)[1] == "short"
        item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
        assert quality == "pointer", body
        assert item.strip() == body, "the pointer's own words are kept: they say where the prose went"

    # And a body that is simply too short is still ``short``: the label says which of the two it is, so a
    # ``pointer`` that was really a failed cut would be a lie about the filing rather than about our rule.
    assert _cut("short_body", OURS_2)[1] == "short"
    assert _cut("short_body", OURS_1)[1] == "short"


def test_pcvx_and_glue_a_long_item_1_is_stored_whole_rather_than_cut_at_the_cap():
    """**Defect 5.** ``ours-1`` cuts its text at 200,000 characters and labels it ``capped`` -- 165 reports in the
    census. The ``sec_forms`` layer stores a section whole and lets its reader cap, so ``ours-2`` caps nothing and
    can never answer ``capped``: the quality a reader sees is about the *cut*, and a cut that is right should not
    be labelled by how much of it another layer wanted.
    """
    text, form = _BY_NAME["over_the_cap"]
    old_item, old_quality = sec_sections.cut_item(text, form, version=OURS_1)
    new_item, new_quality = sec_sections.cut_item(text, form, version=OURS_2)
    assert (old_quality, len(old_item)) == ("capped", sec_sections.MAX_ITEM_CHARS)
    assert new_quality == "ok" and len(new_item) > sec_sections.MAX_ITEM_CHARS
    assert new_item.startswith(old_item[:200]), "the same cut, just not truncated"
    assert not sec_sections.rule(OURS_2).caps
    assert sec_sections.rule(OURS_1).caps


def test_no_input_at_all_makes_ours_2_answer_capped():
    """The promise above, as a property rather than one case: whatever the cut does, this version never writes
    that label, because the layer reading it has nothing that caps."""
    for name, text, form in CASES:
        quality = sec_sections.cut_item(text, form, version=OURS_2)[1]
        assert sec_sections.base(quality) != "capped", name
        assert sec_sections.base(quality) in {"ok", "short", "overrun", "pointer", "missing"}, (name, quality)


# ── the overrun rule's own shapes, which a label this cheap could get wrong in three ways ──────────────────────
def test_a_slice_that_runs_to_the_end_of_the_document_is_overrun_even_with_no_other_item_in_it():
    """No closing heading anywhere: the slice is the rest of the filing. That was ``overrun`` before this issue
    and stays ``overrun``, and the two conditions are an ``or`` for that reason -- a label that only looked at
    what the slice *holds* would call this one clean, which is the shape the cap used to catch."""
    item, quality = sec_sections.cut_item("Item 1. Business\n" + BODY, "10-K", version=OURS_2)
    assert quality == "overrun"
    assert not sec_sections._holds_another_item(item, "1"), "nothing in it says so; its ending does"


def test_our_own_item_s_heading_inside_the_slice_is_not_another_item():
    """A filer repeats the section's own heading as a running header on every page, and a form served as text
    carries those headers inline. Counting our own item would call every such filing ``overrun``.

    On a **20-F**, because that is where the end pattern can match our own item at all: it reads ``1a``-``1c``,
    ``2``-``9`` and ``10``-``19``, so a 10-K's own "Item 1" is not a heading it can produce, while a 20-F's
    "Item 4" is. A 10-K case here would pass whatever the rule did with its own item.
    """
    text = ("Item 4. Information on the Company\n" + BODY + "\nItem 4. Information on the Company\n" + BODY
            + "\nItem 5. Operating Review\n" + BODY)
    item, quality = sec_sections.cut_item(text, "20-F", version=OURS_2)
    assert quality == "ok"
    assert "Item 4. Information on the Company" in item, "the repeated header is inside the slice, and is ours"
    assert [m.group(1) for m in sec_sections._LINE_END_2.finditer(item)] == ["4"], \
        "the end pattern does match it -- the rule knows it is ours, it does not fail to see it"


def test_a_quoted_cross_reference_at_the_start_of_a_line_is_not_that_item_s_heading():
    """A quotation that wrapped onto its own line -- ``Item 2. Properties" of our 2024 annual report`` -- starts a
    line and names an item, and is a sentence about Item 2 rather than the start of it. The slice is judged with
    the same :func:`sec_sections._is_ref` the cut itself uses, for the same reason: a filing that quotes its own
    later items would otherwise be ``overrun``, which reads as a cut that went wrong.
    """
    text = ("Item 1. Business\n" + BODY + "\nItem 2. Properties\u201d of our 2024 annual report says so\n" + BODY
            + "\nItem 1A. Risk Factors\n" + BODY)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and "of our 2024 annual report says so" in item
    inside = [(m.group(1), sec_sections._is_ref(item, m)) for m in sec_sections._LINE_END_2.finditer(item)]
    assert inside == [("2", True)], "the end pattern matches it, and the reference check is what answers"


# ── the line rule's two decisions, which only differ from ours-1 where no heading closes the body ──────────────
def test_a_line_start_body_with_no_closing_heading_is_kept_rather_than_handed_to_the_pattern_path():
    """``ours-1`` returns None here and lets the inline START/STOP patterns decide, which is how its cap came to
    bound the answer. Nothing caps in ``ours-2``, so the body simply runs to the end of the document -- and that
    matters, because the pattern path has no line-start rule and takes the *first* mention of the heading
    anywhere, including one inside a sentence.
    """
    text = "part one Item 1 - Business of the registrant follows.\n\nItem 1. Business\n" + BODY
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "overrun"
    assert item.startswith(BODY[:40]), "the heading on its own line is the start, not the sentence above it"
    assert "of the registrant follows" not in item


def test_a_start_whose_body_is_shorter_than_the_body_minimum_is_passed_over_for_a_later_one():
    """An index block the contents check missed: a heading, a few hundred words, the next heading. The rule keeps
    looking, and the real body is further down. Measured on TIGO and EC when the limit was added."""
    text = ("Item 1. Business\n" + "A small note. " * 180 + "\nItem 2. Properties\nland\n\n"
            + "Item 1. Business\n" + BODY + "\nItem 1A. Risk Factors\n" + BODY)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert item.startswith(BODY[:40]) and "A small note." not in item
    assert len(item) > sec_sections._BODY_MIN


# ── and the boards are left exactly where they were ────────────────────────────────────────────────────────────
def test_the_boards_stay_on_ours_1_and_their_text_does_not_move():
    """The reason ``ours-2`` is a version and not a fix in place. ``module_value_chain/edgar_text.py`` and
    ``rcdata/build/fundamentals/filings.py`` cut live text with the board's wrapper and store what they cut, with
    no version column to tell two rules' rows apart -- and the Relatedness profiles built on that text are a paid
    model run to rebuild. So this issue must not change a single answer the board gets.
    """
    assert splitter.VERSION == OURS_1
    for name, text, form in CASES:
        assert splitter.slice_item(text, form) == sec_sections.cut_item(text, form, version=OURS_1), name


def test_ours_2_is_declared_and_reachable_like_any_other_version():
    assert OURS_2 == "ours-2" and OURS_2 in sec_sections.VERSIONS
    assert sec_sections.DEFAULT_VERSION == OURS_1, "a new version does not become the default by existing"
    assert sec_sections.rule(OURS_2).version == OURS_2
    assert sec_sections.rule(OURS_2).slice_item is sec_sections._slice_item_ours_2
    # Until #756 this was ours-1's own function, so that one PR moved one rule; #756 gave ours-2 its own 40-F
    # business rule, and the two are now separate functions. test_sec_sections_forty_f.py is where it is tested.
    assert sec_sections.rule(OURS_2).aif_business is sec_sections._aif_business_ours_2
    assert sec_sections.rule(OURS_2).aif_business is not sec_sections.rule(OURS_1).aif_business


def test_ours_2_adds_its_own_patterns_rather_than_editing_the_shared_ones():
    """The module docstring's rule, as an assertion: ``ours-1``'s objects are still the ones the boards read, and
    ``ours-2``'s are its own. Editing a shared pattern in place is the one change this file cannot catch by
    comparing outputs, because both versions would move together.
    """
    assert sec_sections._LINE_START_2 is not sec_sections._LINE_START
    assert sec_sections._LINE_END_2 is not sec_sections._LINE_END
    assert sec_sections.START_PATTERNS_2 is not sec_sections.START_PATTERNS
    assert sec_sections.STOP_PATTERNS_2 is not sec_sections.STOP_PATTERNS
    assert "item" in sec_sections.START_PATTERNS["10-K"], "ours-1 still spells the word literally"
    assert sec_sections.spaced("item") in sec_sections.START_PATTERNS_2["10-K"]


def test_all_four_of_ours_2_s_patterns_read_the_spaced_word_and_only_the_end_one_guards_sub_items():
    """Structural, and deliberately so. ``ours-2`` has two paths -- the line rule and the pattern rule -- and on
    the shapes in this file they answer alike, so putting the literal ``"item"`` back in any ONE pattern changes
    no output: the other path finds the same section. That redundancy is a property worth having, and it is also
    what hides a half-finished fix, which the review of ``c3fc0d5`` showed by mutating each pattern in turn. So
    the spelling is asserted on the objects themselves.

    The behavioural tests above cover :data:`_LINE_END_2` (the heading VFF splits) and the fallback's stop copy;
    these assertions cover all four, and the sub-item guard's one legitimate home.
    """
    spaced_item = sec_sections.spaced("item")
    assert spaced_item in sec_sections._LINE_START_2["10-K"].pattern
    assert spaced_item in sec_sections._LINE_START_2["20-F"].pattern
    assert spaced_item in sec_sections._LINE_END_2.pattern
    for form in ("10-K", "20-F"):
        assert spaced_item in sec_sections.START_PATTERNS_2[form], form
        assert spaced_item in sec_sections.STOP_PATTERNS_2[form], form
        assert "item" not in sec_sections.START_PATTERNS_2[form].replace(spaced_item, ""), form

    # The guard belongs on the end pattern, which matches a bare number, and nowhere else: every start and stop
    # requires a title after the number, which a sub-item never carries.
    guard = sec_sections._NOT_SUBITEM
    assert guard == r"(?!\.[ \t\xa0\u202f\u3000]*\d\d\b)"
    # The spaces it reaches over are named one by one: the five named in the pattern. A class spelled by exclusion
    # read every separator it had not thought of -- U+2028, U+0085 -- as a space, and ate the page number
    # printed under a heading as if it were a sub-item number.
    assert "^" not in guard, "the class lists what it allows, not what it forbids"
    assert guard in sec_sections._LINE_END_2.pattern
    assert guard not in sec_sections._LINE_START_2["10-K"].pattern
    assert guard not in sec_sections._LINE_START_2["20-F"].pattern
    for form in ("10-K", "20-F"):
        assert guard not in sec_sections.START_PATTERNS_2[form], form
    # The 10-K stops carry a title after the number, so they need no guard. The 20-F stop does not, and a
    # sub-item can end a 20-F section there; that is a change to the cut and is measured on its own, so the
    # gap is asserted here rather than hidden.
    assert guard not in sec_sections.STOP_PATTERNS_2["10-K"]
    assert guard not in sec_sections.STOP_PATTERNS_2["20-F"], "still open: see this pattern's note"


# ── the review of c3fc0d5: three ways ours-2 answered WORSE than ours-1 ────────────────────────────────────────
# Each of these was measured on the commit under review, where ours-2 lost to ours-1 on the same input. They are
# here as named tests and not only as golden rows because the golden records "what ours-2 answers", while these
# say which answer is the right one and why -- and because a rule change that moved them would then have to argue
# with a sentence, not just recapture a file.
def test_a_line_start_with_no_line_start_stop_still_ends_at_the_titled_stop():
    """The worst of the three. "Item 1A. Risk Factors" written inline -- mid-line, after a page footer, behind a
    "PART I" -- is where Item 1 ends, and ``ours-1``'s pattern path stopped there. ``ours-2``'s line rule accepts
    a start that ``ours-1`` gave up on, so on this input it had no line-start heading to stop at and ran to the
    end of the filing: 55,022 characters where ``ours-1`` gave 26,999, all of it Item 1A and beyond. On a real
    filing, with no cap left to catch it, that is the whole document stored as Item 1.

    The label was honest (``overrun``), which is why only a length check finds this.
    """
    risk = "Risks abound. " * 2000
    for tail in (" Item 1A. Risk Factors ", "\nPART I Item 1A. Risk Factors\n"):
        text = "Item 1. Business\n" + BODY + tail + risk
        old_item, old_quality = sec_sections.cut_item(text, "10-K", version=OURS_1)
        new_item, new_quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
        assert old_quality == "ok"
        assert (new_quality, len(new_item)) == (old_quality, len(old_item)), tail
        assert "Risks abound" not in new_item, tail


def test_a_heading_with_its_page_number_under_it_still_ends_the_slice():
    """"Item 1A.\\n12\\nRisk Factors" -- a heading and the page number the filer printed beneath it. The guard
    that keeps "Item 1.01" from being read as Item 1 was spelled ``(?![.,]\\s*\\d)``, and ``\\s*`` crossed that
    newline, so the heading was not a heading: the slice ran through the risk factors and still said ``ok``,
    because :func:`_holds_another_item` reads the same pattern and was blind to the same heading.

    A clean label on a wrong slice is worse than ``overrun`` on one, which is what makes this the second fix.
    """
    text = ("Item 1. Business\n" + BODY + "\nItem 1A.\n12\nRisk Factors\n" + "Risks abound. " * 2000
            + "\nItem 2. Properties\n" + BODY)
    old_item, old_quality = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new_item, new_quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert (new_quality, len(new_item)) == ("ok", len(old_item)) and old_quality == "ok"
    assert "Risks abound" not in new_item


@pytest.mark.parametrize("heading,is_an_item", [
    ("\nItem 1A.\n12\nRisk Factors\n", True),   # the shape above: a page number on the next line
    ("\nItem 2.\n34\n", True),                  # the same, with nothing but the number after it
    ("\nItem 7. 2024 Results\n", True),         # a year is not a sub-item number
    ("\nItem 2, 3 and 4\n", True),              # combined items
    ("\nItem 2,10 and 11. Properties\n", True),  # the same, with two-digit numbers: a comma never introduces
                                                #   a sub-item, so the guard must not read one after it
    ("\nItem 1.01 Entry into a Material Definitive Agreement\n", False),   # an 8-K sub-item
    ("\nItem 2.01 Completion of Acquisition\n", False),                    # PCVX's, the defect of #755
])
def test_only_a_digit_glued_to_the_stop_makes_a_sub_item(heading, is_an_item):
    """The guard's whole job, as a table. ``ours-1``'s end pattern reads every row above as a heading, which is
    the PCVX defect on the last two; ``ours-2`` must reject exactly those two and keep the rest.
    """
    assert bool(sec_sections._LINE_END_2.search(heading)) is is_an_item


def test_items_1_and_2_written_with_a_comma_is_still_a_start():
    """"Items 1, 2. Business and Properties", one of the three spellings ``START_PATTERNS``'s ``(?:and|&|,)``
    branch exists for. The sub-item guard was on the start patterns too, where it rejected "1, 2" and made that
    branch dead: ``ours-2`` answered ``missing`` on a filing ``ours-1`` cut cleanly. A start needs no guard --
    every start pattern requires a title after the number ("business", "information on the company"), which
    "Item 1.01 Entry into a Material Definitive Agreement" never carries.
    """
    text = "Items 1, 2. Business and Properties\n" + BODY + "\nItem 1A. Risk Factors\n" + "Risky. " * 100
    old = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert old[1] == "ok" and new[1] == "ok"
    assert new[0] == old[0]
    for pattern in sec_sections.START_PATTERNS_2.values():
        assert "(?![.,]" not in pattern, "the guard belongs on the end pattern alone"


# ── the ours-2 baseline: every shared case's text, not just its label ──────────────────────────────────────────
# The review of c3fc0d5 found this gap and the first finding above hid in it. ``test_no_input_at_all_makes_ours_2
# _answer_capped`` asserts a label *set* over CASES, so ours-2's text on a shared case could move freely: dropping
# the contents-page skip or the 20-F regulation stop from ours-2 changes what it cuts and still passes. This file
# pins the text itself.
#
# It is a BASELINE and not a truth: it records what ours-2 answers today, captured from this very code, so it
# proves no answer is right. What it proves is that the next edit to ours-2 cannot move a shared case's text
# without a human seeing the number change. The three rows where it differs from ours-1's golden are the three
# named defects above, and the test below says so by name rather than letting them pass as mere difference.
_OURS_2_GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "sec_sections_ours_2_golden.json").read_text(encoding="utf-8"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_ours_2_baseline_covers_every_case_and_nothing_else():
    assert set(_OURS_2_GOLDEN["cases"]) == {name for name, _, _ in CASES}
    assert _OURS_2_GOLDEN["rule_version"] == OURS_2


@pytest.mark.parametrize("name,text,form", CASES, ids=[c[0] for c in CASES])
def test_ours_2_still_cuts_every_shared_case_the_way_it_was_captured(name, text, form):
    want = _OURS_2_GOLDEN["cases"][name]
    assert _sha(text) == want["input_sha256"], \
        f"the input {name!r} changed since the baseline was captured; recapture it or leave it alone"

    item, quality = sec_sections.cut_item(text, form, version=OURS_2)
    assert quality == want["quality"]
    if want["item_chars"] is None:
        assert item is None
    else:
        assert item is not None and len(item) == want["item_chars"]
        assert _sha(item) == want["item_sha256"]
        assert item[:80] == want["item_head"] and item[-80:] == want["item_tail"]

    aif = sec_sections.cut_aif_business(text, version=OURS_2)
    if want["aif_business_chars"] is None:
        assert aif is None
    else:
        assert aif is not None and len(aif) == want["aif_business_chars"]
        assert _sha(aif) == want["aif_business_sha256"]


def test_the_only_cases_where_ours_2_differs_from_ours_1_are_the_three_named_defects():
    """The baseline against the move's golden, row by row. A fourth row moving is a side effect nobody asked
    for, and this is the test that would say so -- the per-case numbers above would move together with the
    recapture that caused it, and the label-set property test would not notice at all.
    """
    ours_1 = json.loads((Path(__file__).parent / "fixtures" / "sec_sections_ours_1_golden.json")
                        .read_text(encoding="utf-8"))["cases"]
    keys = ("quality", "item_chars", "item_sha256", "aif_business_chars", "aif_business_sha256")
    moved = {name for name in ours_1
             if any(ours_1[name][k] != _OURS_2_GOLDEN["cases"][name][k] for k in keys)}
    assert moved == {
        "split_word_item_1a",        # VFF's "ITE M 1A": overrun 27,030 -> ok 26,999
        "over_the_cap",              # the cap is gone: capped 200,000 -> ok 239,999
        "suspect_inner_item_header",  # same text, honest label: ok -> overrun
        # #756, the 40-F rule: a title ours-1 does not know, and the AIF cap removed with the item one.
        "aif_description_of_business",  # one of the 28: None -> 26,999
        "aif_over_the_cap",             # 200,000 -> 239,999
    }


def test_the_titled_fallback_skips_a_stop_heading_named_in_the_first_paragraph():
    """A filer who writes "see Item 1A. Risk Factors below" in his opening paragraph has not ended Item 1
    there, and what got stored as the business section would otherwise be the introduction. What saves it is
    the reference word, not the mention's position: an earlier version of the fallback started its search at
    :data:`MIN_ITEM_CHARS` instead, and that is the version this file's "genuinely short item 1" test rejects.

    The mention has to sit more than 400 characters past the start, or the contents-page check skips the start
    altogether and the pattern path decides instead.
    """
    text = ("Item 1. Business\nWe begin. " + "Opening paragraph. " * 30
            + "See Item 1A. Risk Factors below for the risks.\n" + BODY
            + " Item 1A. Risk Factors " + "Risky. " * 100)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert len(item) > sec_sections.MIN_ITEM_CHARS
    assert "We begin" in item and "Risky." not in item


@pytest.mark.parametrize("name,text,form,needle", [
    # the line-start pattern: VFF's own spelling, but in the heading that OPENS the section
    ("line start", "ITE M 1. Business\n" + BODY + "\nItem 1A. Risk Factors\n" + "Risky. " * 100,
     "10-K", "Risky."),
    # the titled fallback's stop copy: the start begins a line, the stop does not, and its word is split
    ("stop copy", "Item 1. Business\n" + BODY + " Ite m 1A. Risk Factors " + "Risky. " * 100,
     "10-K", "Risky."),
    # the 20-F pattern start, reached because nothing here starts a line
    ("20-F start copy", "Cover page. Ite m 4. Information on the Company " + BODY
     + " Item 5. Operating Results " + "Results. " * 100, "20-F", "Results."),
])
def test_every_pattern_spells_item_the_spaced_way_not_just_the_end_one(name, text, form, needle):
    """Defect 1 is "``item`` is spelled with :func:`spaced` like every other heading word", and the filing it
    was measured on splits the word in its *stop* heading. The rule has four patterns that read the word, and a
    test on one of them lets the other three fall back to the literal spelling without a red run -- the review
    of ``c3fc0d5`` found exactly that, by putting ``"item"`` back in each of them in turn.
    """
    item, quality = sec_sections.cut_item(text, form, version=OURS_2)
    assert item is not None, name
    assert sec_sections.base(quality) in {"ok", "overrun"}, (name, quality)
    assert needle not in item, f"{name}: the slice ran past the heading whose word is split"


@pytest.mark.parametrize("spelling", ["Items 1,2. Business and Properties", "Items 1, 2. Business and Properties"])
def test_combined_items_are_a_start_with_or_without_a_space_after_the_comma(spelling):
    """The sub-item guard belongs on the end pattern alone, and both spellings show why. With the guard on a
    start, "1,2" is rejected outright and "1, 2" was rejected by the older ``\\s*`` spelling -- which is why the
    fix to that spelling masks this one unless both are tested.
    """
    text = spelling + "\n" + BODY + "\nItem 1A. Risk Factors\n" + "Risky. " * 100
    old = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert old[1] == "ok" and new[1] == "ok", spelling
    assert new[0] == old[0], spelling
    # both paths read the comma branch, so each masks the other's loss: count it in both.
    assert sec_sections._LINE_START_2["10-K"].pattern.count("(?:and|&|,)") == 1
    assert sec_sections.START_PATTERNS_2["10-K"].count("(?:and|&|,)") == 1


# A line starting with "Item 1. Business" is not always the section's heading, and the line rule has three
# skips that say so. The contents-page one is pinned by the shared cases; these two were not, and ours-2's
# line rule is reached far more often than ours-1's because it no longer gives up on an unclosed start.
_FILLER = "Forward-looking statements follow. " * 20 + "\n"   # over 400 characters, and no item heading in it


def test_a_contents_line_whose_page_number_is_underneath_it_is_not_the_heading():
    """"Item 1. Business" with "3" on the next line is a contents entry. The check for the item heading within
    400 characters does not see this one -- the contents page lists nothing else nearby -- so the page number is
    what gives it away. Taking it would start the slice in the contents page.
    """
    text = ("Item 1. Business\n3\n" + _FILLER + "Item 1. Business\n" + BODY
            + "\nItem 1A. Risk Factors\n" + "Risky. " * 100)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert item.startswith(BODY[:40]), "the slice begins at the real heading, not in the contents page"
    assert "Forward-looking" not in item


def test_a_cross_reference_broken_across_two_lines_is_not_the_heading_either():
    """A sentence the HTML-to-text conversion broke after "refer to" leaves "Item 1. Business, above." at the
    start of its own line, which the start pattern matches. What says it is a reference and not a heading is the
    line before it, which this skip reads past the newline.

    The decoy has to be a line-start match for the skip to be tested at all: a mention inside a line, such as
    ``See "Item 1. Business" for ...``, never matches the pattern and so proves nothing about it.
    """
    text = ("For a description of our operations, refer to\nItem 1. Business, above.\n" + _FILLER
            + "Item 1. Business\n" + BODY + "\nItem 1A. Risk Factors\n" + "Risky. " * 100)
    decoy = sec_sections._LINE_START_2["10-K"].search(text)
    assert sec_sections._is_ref(text, decoy), "the decoy must be a line-start match, or the skip is untested"

    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert item.startswith(BODY[:40])
    assert "Forward-looking" not in item


# ── the review of a8847db8: what the titled fallback itself gets right ─────────────────────────────────────────
def test_a_genuinely_short_item_1_ends_at_its_real_stop_and_is_short_not_ok():
    """The fallback's own defect, found in the round that added it. It used to start searching at
    :data:`MIN_ITEM_CHARS`, so a filing whose Item 1 is shorter than that had its real inline "Item 1A. Risk
    Factors" walked past; the slice closed on the Item 2 stop instead and said ``ok`` with every risk factor
    inside it. ``ours-1`` answered ``short`` on this filing, which is the honest answer and is what it means:
    the section really is this short.
    """
    short_body = "We make things. " * 94
    text = ("Item 1. Business\n" + short_body + " Item 1A. Risk Factors " + "Risks abound. " * 2000
            + " Item 1B. Unresolved Staff Comments None. Item 2. Properties " + BODY)
    old_item, old_quality = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new_item, new_quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert (old_quality, new_quality) == ("short", "short")
    assert new_item == old_item
    assert "Risks abound" not in new_item


def test_the_fallback_stops_at_the_first_heading_and_not_a_later_one():
    """Three stops follow the body; the section ends at the first. Taking the last would store Item 1A and
    Item 1B as part of Item 1 and still call it ``ok``, because every stop closes the slice.
    """
    text = ("Item 1. Business\n" + BODY + " Item 1A. Risk Factors " + "Risky. " * 400
            + " Item 1B. Unresolved Staff Comments None. Item 2. Properties " + "Land. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "Risky." not in item and "Land." not in item


def test_the_fallback_works_on_a_20_f_as_well_as_a_10_k():
    """Both forms have a stop list, and the 20-F path is the one no shared case reaches. Without the fallback
    here, a 20-F whose Item 5 does not begin a line runs to the end of the document.
    """
    text = ("Item 4. Information on the Company\n" + BODY + " Item 5. Operating Results " + "Results. " * 400)
    item, quality = sec_sections.cut_item(text, "20-F", version=OURS_2)
    assert quality == "ok"
    assert "Results." not in item
    assert len(item) < len(text) - 2000


def test_a_mention_in_the_body_does_not_end_the_slice_when_a_real_heading_follows():
    """``See "Item 1A. Risk Factors" of this report`` in the middle of Item 1 is a cross-reference. The
    fallback reads the words before a stop (:func:`_inline_ref`), not its position, which is what lets it keep
    this and still end a short Item 1 at its real stop.
    """
    text = ("Item 1. Business\n" + BODY[:3000] + " as discussed under “Item 1A. Risk Factors” of this "
            "report, which follows. " + BODY[:3000] + " Item 1A. Risk Factors " + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "as discussed under" in item, "the mention is part of the section, not its end"
    assert "Risky." not in item


def test_a_heading_on_its_own_line_is_kept_even_when_the_words_before_it_read_as_a_reference():
    """"See" as a page's last word and the heading as the next page's first line. :func:`_is_ref` strips the
    newline and so calls that heading a reference; the line test in :func:`_inline_ref` is what keeps it, and
    the shared ``suspect_tail_cross_reference`` case is the one that would move without it.
    """
    text, form = _BY_NAME["suspect_tail_cross_reference"]
    assert sec_sections.cut_item(text, form, version=OURS_2) == \
        sec_sections.cut_item(text, form, version=OURS_1)


@pytest.mark.parametrize("heading,is_an_item", [
    ("\nItem 2. 01 Completion of Acquisition\n", False),   # strip_html's spaces: PCVX's exhibit list again
    ("\nItem 5. 02 Departure of Directors\n", False),
    ("\nItem 2,3 and 4\n", True),            # a comma never introduced a sub-item
    ("\nItem 2. 15 properties are leased\n", False),   # the cost of the two above, written down
])
def test_a_tag_split_sub_item_number_is_still_a_sub_item(heading, is_an_item):
    """:func:`strip_html` turns a tag into a space, so "Item 2.</b><b>01" arrives with one in it. The guard
    allows for that the way :func:`spaced` allows for it in the heading words -- and the last row is what that
    costs: a section whose text opens with a two-digit number is not read as a heading.
    """
    assert bool(sec_sections._LINE_END_2.search(heading)) is is_an_item


def test_every_branch_of_every_stop_list_reads_the_spaced_word_and_is_still_there():
    """Three of ``STOP_PATTERNS_2``'s four 10-K and 20-F branches were unpinned: putting the literal ``"item"``
    into the 1B, Item 2 or 20-F Item 5 branch, or deleting any of them, moved no test. Asserting "the spaced
    word appears in this pattern" is satisfied by one branch of three, so this counts them.
    """
    spaced_item = sec_sections.spaced("item")
    for form, branches in (("10-K", 3), ("20-F", 2)):
        pattern = sec_sections.STOP_PATTERNS_2[form]
        assert pattern.count(spaced_item) == branches, form
        assert "item" not in pattern.replace(spaced_item, ""), f"{form}: a branch kept the literal word"
    # the letter may stand apart from the number on the same line ("Item 1 A.", Tempus AI, #820)
    assert r"1[^\S\n]?a" in sec_sections.STOP_PATTERNS_2["10-K"]
    assert r"1[^\S\n]?b" in sec_sections.STOP_PATTERNS_2["10-K"]
    assert "4a" in sec_sections.STOP_PATTERNS_2["20-F"]


def test_the_pattern_path_reads_ours_2_s_own_stops_and_not_ours_1_s():
    """An all-inline 10-K whose stop heading splits the word: only ``ours-2``'s stop copy sees it, and the
    pattern path has to be the one reading it. Pointing that path at ``ours-1``'s ``STOP_PATTERNS`` moved no
    test before this one.
    """
    text = ("Cover. Item 1. Business " + BODY + " Ite m 1A. Risk Factors " + "Risky. " * 400)
    assert sec_sections._line_item_ours_2(text, "10-K") is None, "this must reach the pattern path"
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert "Risky." not in item
    assert sec_sections.base(quality) == "ok"
    assert "Risky." in sec_sections.cut_item(text, "10-K", version=OURS_1)[0], "ours-1 cannot see that stop"


# ── the review of f5699673: the quotation mark that made a real heading a mention ──────────────────────────────
@pytest.mark.parametrize("name,text,form", [
    ("a quoted term in the next item's first sentence",
     "Item 1. Business\n" + BODY + " Item 1A. Risk Factors You should consider the risks (the “Risks”) "
     "below. " + "Risks abound. " * 1800
     + " Item 1B. Unresolved Staff Comments None. Item 2. Properties " + BODY, "10-K"),
    ("the same with no later stop at all",
     "Item 1. Business\n" + BODY + " Item 1A. Risk Factors You should consider the risks (the “Risks”) "
     "below. " + "Risks abound. " * 1800, "10-K"),
    ("a 20-F whose Item 5 heading is followed by a defined term",
     "Item 4. Information on the Company\n" + BODY + " Item 5. Operating and Financial Review and Prospects "
     "prepared under (“IFRS”). " + "Risks abound. " * 1800, "20-F"),
])
def test_a_quoted_term_after_an_inline_heading_does_not_make_it_a_mention(name, text, form):
    """:func:`_is_ref` calls a heading a reference when a closing quotation mark follows within 200 characters
    of the same line. For a heading that begins a line, that line is the heading. For an **inline** one those
    200 characters are the next item's prose, so one defined term in its first sentence -- and filers define a
    term in the first sentence of Item 1A as a habit -- turned the real heading into a mention: the slice ran
    on and still said ``ok``, with every risk factor inside it.

    That is why :func:`_inline_ref` counts only a quotation mark it did not see opened, instead of calling the
    shared test. The filing is cut right here, and the same filing with straight quotes was always cut right,
    which is how the cause was found.
    """
    old_item, old_quality = sec_sections.cut_item(text, form, version=OURS_1)
    new_item, new_quality = sec_sections.cut_item(text, form, version=OURS_2)
    assert (new_quality, new_item) == (old_quality, old_item), name
    assert "Risks abound" not in new_item, name


@pytest.mark.parametrize("mention", [
    " as discussed under “Item 1A. Risk Factors” of this report. ",
    " the “Item 1A. Risk Factors” section describes them. ",
    " see “Part I, Item 1A. Risk Factors”. ",
])
def test_a_mention_in_quotation_marks_is_still_skipped(mention):
    """The other half of the row above: a quotation mark the mention opened itself still marks it a mention,
    whether or not a reference word comes first. Without this the fix would have been "ignore quotes".
    """
    text = ("Item 1. Business\n" + BODY[:3000] + mention + BODY[:3000] + " Item 1A. Risk Factors "
            + "Risks abound. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert mention.strip()[:20] in item, "the mention belongs to the section, not to its end"
    assert "Risks abound" not in item


def test_an_opening_paragraph_mention_with_no_reference_word_ends_the_slice_like_ours_1_does():
    """The cost of reading the words instead of the position, written down. "Our risks are listed in the
    section Item 1A. Risk Factors below" carries no "see", "under" or quotation mark, so it is a heading as far
    as this rule can tell, and Item 1 is ``short``. ``ours-1`` answers the same, so this is parity and not a
    regression; telling this mention from a heading needs the hand-checked set (#763).
    """
    text = ("Item 1. Business\nOur risks are listed in the section Item 1A. Risk Factors below. "
            + BODY + " Item 1A. Risk Factors " + "Risks abound. " * 400)
    assert sec_sections.cut_item(text, "10-K", version=OURS_2) == \
        sec_sections.cut_item(text, "10-K", version=OURS_1)
    assert sec_sections.cut_item(text, "10-K", version=OURS_2)[1] == "short"


def test_an_indented_heading_still_counts_as_starting_its_own_line():
    """The line test strips what it finds before the match, so a heading a filer indents is still a heading and
    not a mention -- which matters because the words before it, across the newline, read as a reference.
    """
    text = ("Item 1. Business\n" + BODY + "\nSee\n   Item 1A. Risk Factors\n" + "Risks abound. " * 400)
    decoy = sec_sections.STOP_PATTERNS_2["10-K"]
    import re as _re
    m = _re.compile(decoy, _re.I).search(text, 100)
    assert not sec_sections._inline_ref(text, m), "indentation is not text before the heading"
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert "Risks abound" not in item


@pytest.mark.parametrize("heading,is_an_item", [
    ("\nItem 1A.\x0c12\nRisk Factors\n", True),    # a form feed is a page break, so the 12 is a page number
    ("\nItem 1A.\r12\nRisk Factors\n", True),      # and so is a bare carriage return
    ("\nItem 2.\t01 Completion of Acquisition\n", False),   # a tab is what a tag became
    ("\nItem 2.\xa001 Completion of Acquisition\n", False),  # and so is a no-break space
    ("\nItem 2.  01 Completion of Acquisition\n", False),
    ("\nItem 16. 10-K Summary\n", False),          # the cost: a title opening with a two-digit token
    ("\nItem 2 .01 Completion of Acquisition\n", True),      # and the sub-item this guard cannot reach
])
def test_the_guard_reaches_over_the_spaces_a_tag_becomes_and_not_over_a_page_break(heading, is_an_item):
    """Which whitespace the guard may cross, and the two costs of the answer. ``ours-1``'s end pattern reads
    every row here as a heading; the middle three are the sub-item it exists to reject, the first two are page
    numbers it must not eat, and the last two are what the spelling cannot do: a title whose first token is a
    two-digit number is lost, and a sub-item whose tag split *before* the full stop is still read as an item.
    Both only matter to :func:`_holds_another_item`, because Item 15 and Item 16 never follow Item 1.
    """
    assert bool(sec_sections._LINE_END_2.search(heading)) is is_an_item


def test_an_inline_start_with_no_stop_at_all_is_overrun_on_the_pattern_path_too():
    """The pattern path's ``closed`` is read by the same label function, and nothing asserted it. A filing
    served as one paragraph, with a start and no stop anywhere, is the rest of the document.
    """
    text = "Cover page. Item 1. Business " + BODY
    assert sec_sections._line_item_ours_2(text, "10-K") is None, "this must reach the pattern path"
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "overrun"
    assert item.endswith(BODY.strip()[-40:])


def test_a_quoted_term_on_the_following_line_belongs_to_that_line_and_not_to_the_heading():
    """The quote window stops at the end of the heading's own line, which is the scope the shared test uses.

    This row does **not** pin the window's line restriction, though an earlier round said it did: "Item" sits
    mid-line here, so the line-end pattern never matches at the mention and the question never reaches the
    quote test. The shape that tells those two spellings apart is in
    ``test_the_details_inside_the_reference_test_each_decide_a_cut``.
    """
    text = ("Item 1. Business\n" + BODY + " Item 1A. Risk Factors\nWe use the term (“Risks”) below. "
            + "Risks abound. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "Risks abound" not in item and "We use the term" not in item


def test_a_heading_broken_across_lines_is_judged_by_the_line_its_first_word_is_on():
    """A stop pattern may match across a newline ("under Item\\n1A. Risk Factors"), because the heading's
    words are allowed to be split. The line this match belongs to is the one its **first** character sits on --
    the one carrying "given under" -- and not the one its tail happens to end on. Measuring from the match's
    end instead puts the line start inside the match, so the words before it read as empty and every such
    mention becomes a heading.
    """
    import re as _re
    text = ("Item 1. Business\n" + BODY + " more detail is given under Item\n1A. Risk Factors of this report. "
            + BODY[:2000] + "\nItem 1A. Risk Factors\n" + "Risks abound. " * 400)
    m = _re.compile(sec_sections.STOP_PATTERNS_2["10-K"], _re.I).search(text, 100)
    assert "\n" in m.group(), "the fixture must be a heading broken across lines"
    assert sec_sections._inline_ref(text, m), "its first word sits after 'given under'"

    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "more detail is given" in item, "the mention is part of the section"
    assert "Risks abound" not in item, "and the real heading, further down, is its end"


# ── the review of f1091f84: the same quotation mark, on a run-in heading ───────────────────────────────────────
@pytest.mark.parametrize("name,text,form", [
    ("10-K", "Item 1. Business\n" + BODY + "\nItem 1A. Risk Factors You should consider the risks (the "
     "“Risks”) below. " + "Risks abound. " * 1800 + "\nItem 1B. Unresolved Staff Comments\nNone.\n",
     "10-K"),
    ("20-F", "Item 4. Information on the Company\n" + BODY + "\nItem 5. Operating and Financial Review and "
     "Prospects prepared under (“IFRS”). " + "Risks abound. " * 1800 + "\nItem 6. Directors\nNames.\n",
     "20-F"),
])
def test_a_run_in_heading_with_a_defined_term_in_its_first_sentence_is_still_a_heading(name, text, form):
    """A **run-in** heading shares its line with the section's first sentence, and filers define a term in that
    sentence as a habit. The shared :func:`_is_ref` reads 200 characters of the heading's line, which for a
    run-in heading is that sentence, so the heading became a reference to itself: the line-end search skipped
    it, the slice closed on the item after next, :func:`_holds_another_item` skipped the same heading for the
    same reason, and the answer was ``ok`` on 52,273 characters with every risk factor inside Item 1.

    ``ours-1`` answers that too -- it is where the defect comes from -- so this is the one row in this file
    where ``ours-2`` is asked to be better than the board rather than equal to it. :func:`_ref_ours_2` is the
    corrected test and ``_is_ref`` keeps its own behaviour, because the board reads it.
    """
    old_item, old_quality = sec_sections.cut_item(text, form, version=OURS_1)
    new_item, new_quality = sec_sections.cut_item(text, form, version=OURS_2)
    assert "Risks abound" in old_item, f"{name}: ours-1's defect is the premise of this test"
    assert new_quality == "ok" and "Risks abound" not in new_item, name
    assert len(new_item) < len(old_item) / 1.5, name

    straight = text.replace("“", '"').replace("”", '"')
    assert sec_sections.cut_item(straight, form, version=OURS_2)[0] == new_item, \
        f"{name}: the same filing with straight quotes was always cut right"


def test_a_mention_whose_own_title_wraps_is_still_a_mention():
    """The quote window ends at the first line ending at or after the match, not at the first one anywhere. A
    mention the filer wrapped mid-title would otherwise have its window cut inside itself, and the quotation
    mark that marks it a mention falls on the other side of the cut.
    """
    text = ("Item 1. Business\n" + BODY[:6000] + " as set out in the section “Part I, Item\n1A. Risk "
            "Factors” of this report. " + BODY[:6000] + "\nItem 1A. Risk Factors\n" + "Risks abound. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "as set out in the section" in item, "the wrapped mention belongs to the section"
    assert "Risks abound" not in item


@pytest.mark.parametrize("after", [
    " and “Part II, Item 7. Management’s Discussion” of it. ",   # two quoted mentions in a row
    " (“we”, “us”) use the term throughout. ",              # two opened pairs after a heading
])
def test_two_quoted_runs_after_a_heading_are_read_from_the_first_quote_and_not_the_last(after):
    """A heading followed by two quoted runs is still a heading: the first quotation mark after it opens a pair,
    so nothing was left unopened.

    It pins the opener's direction too, and that took the item **after** the risk factors. An earlier version
    of this fixture ended with them, and there the titled fallback reached the same stop whichever end the
    opener was searched from, so the mutation looked equivalent and was written down as equivalent. With an
    Item 1B after them it is not: searching backwards finds the second pair's opener, reads the first pair's
    closer as unopened, calls the real heading a mention and gives ``ok`` on 41,064 characters with every risk
    factor inside Item 1.
    """
    text = ("Item 1. Business\n" + BODY + "\nItem 1A. Risk Factors" + after + "Risks abound. " * 400
            + "\nItem 1B. Unresolved Staff Comments\nNone.\n")
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "Risks abound" not in item and after.strip()[:12] not in item


def test_the_quote_window_is_two_hundred_characters_and_the_reference_window_is_forty():
    """Two rows about the windows' sizes, and what they are worth: a stop with a quotation mark 300 characters
    away is a heading, and a reference word 12 spaces before a mention still marks it one.

    Neither row pins its number from both sides. Shrinking the quote window to 40 or growing it to 300, or
    moving the reference window between 10 and 400, leaves both answers as they are -- only a quote window of
    2,000 is caught, by the first row. Where exactly those edges belong is a question for real filings (#763);
    these rows say only that each window exists and reaches as far as the shape it is measured on.
    """
    far = ("Item 1. Business\n" + BODY + " Item 1A. Risk Factors " + "x" * 300 + "” still open. "
           + "Risks abound. " * 400)
    assert "Risks abound" not in sec_sections.cut_item(far, "10-K", version=OURS_2)[0], \
        "a quotation mark 300 characters past the heading says nothing about it"

    near = ("Item 1. Business\n" + BODY[:6000] + " refer to" + " " * 12 + "Item 1A. Risk Factors for them. "
            + BODY[:6000] + "\nItem 1A. Risk Factors\n" + "Risks abound. " * 400)
    item = sec_sections.cut_item(near, "10-K", version=OURS_2)[0]
    assert "refer to" in item, "a reference word 12 spaces before the mention still marks it one"
    assert "Risks abound" not in item


@pytest.mark.parametrize("separator", ["\x0c", "\r", "\v", " ", " ", "\x85", "\x1c"])
def test_only_the_named_spaces_are_crossed_by_the_sub_item_guard(separator):
    """Every other separator keeps the number after it a page number. The class used to be spelled by
    exclusion, which read each of these as a space and ate the page number as a sub-item number -- U+2028 and
    U+0085 among them, which no list of line endings had thought of.

    The class names five characters, and they are a choice and not a complete set: ``&ensp;``, ``&emsp;`` and
    ``&thinsp;`` are entities too and are not on it, because :func:`strip_html` turns a tag into a space, a tab
    or U+00A0 and nothing wider.
    """
    assert sec_sections._LINE_END_2.search("\nItem 1A." + separator + "12\nRisk Factors\n")


def test_the_indented_heading_test_asserts_its_label():
    """The earlier indented-heading test bound ``quality`` and never read it."""
    text = ("Item 1. Business\n" + BODY + "\nSee\n   Item 1A. Risk Factors\n" + "Risks abound. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "Risks abound" not in item


def test_ours_1_s_reference_test_keeps_its_own_behaviour():
    """The board's :func:`_is_ref` is untouched, and no golden case reaches its quote half -- the move's golden
    reads one path per case, and none of them has a quotation mark near a heading. So the half is pinned here,
    by the difference between the two versions on the input that found the defect: ``ours-1`` calls that run-in
    heading a reference and ``ours-2`` does not. Both answers are deliberate.
    """
    text = ("Item 1A. Risk Factors You should consider the risks (the “Risks”) below. "
            + "Risks abound. " * 10)
    m = sec_sections._LINE_END.search(text)
    assert m is not None
    assert sec_sections._is_ref(text, m), "ours-1 reads any closing quote on the line as a reference"
    assert not sec_sections._ref_ours_2(text, m), "ours-2 reads only one it did not see opened"


# ── the review of 00385d7e: a cited item carries its title in quotation marks ──────────────────────────────────
_H = BODY[:13000]


@pytest.mark.parametrize("name,text,form,want_quality", [
    ("a mention the filer wrapped before the number",
     "Item 1. Business\n" + _H + " as discussed in Part I,\nItem 1A, “Risk Factors,” of this report. "
     + _H + "\nItem 1A. Risk Factors\n" + "Risky. " * 400, "10-K", "ok"),
    ("a paragraph that opens with the citation",
     "Item 1. Business\n" + _H + "\nItem 7, “Management's Discussion and Analysis,” contains more. "
     + _H + "\nItem 1A. Risk Factors\n" + "Risky. " * 400, "10-K", "ok"),
    ("inside the first 2,000 characters, where only the label can go wrong",
     "Item 1. Business\n" + BODY[:1000] + "\nItem 1A, “Risk Factors,” of this report. " + _H
     + "\nItem 1B. Unresolved\nNone.\n", "10-K", "ok"),
    ("before the real start, where it would move where the slice begins",
     "Contents. as set out in Part I,\nItem 1. Business “Overview” of this report. " + _H[:5000]
     + "\nItem 1. Business\n" + _H + "\nItem 1A. Risk Factors\n" + "Risky. " * 400, "10-K", "ok"),
    ("a 20-F citing its own Item 5",
     "Item 4. Information on the Company\n" + _H + " described in Part I,\nItem 5. “Operating and "
     "Financial Review and Prospects” below. " + _H + "\nItem 5. Operating Results\n" + "Risky. " * 400,
     "20-F", "ok"),
])
def test_an_item_cited_with_its_title_in_quotation_marks_is_a_mention(name, text, form, want_quality):
    """The citation style a 10-K uses for an item it is not starting: ``Item 1A, "Risk Factors," of this
    report``. The quotation marks are around the **title**, so they open after the number and close after it --
    and the unopened-quote test read that as a heading, because the quote it saw was opened. Measured cost
    before this row existed: Item 1 cut in half (13,024 characters of 26,066) and still labelled ``ok``, on
    four of these five shapes, and a false ``overrun`` on the fifth.

    The five rows are the four places ``ours-2`` asks the question -- the line-end search, the titled fallback,
    :func:`_holds_another_item` and the start filter -- plus the 20-F path. The last two were never exercised
    before this review.

    What marks a mention now is :func:`_cites_its_title`, asked of the text that follows the item's **number**:
    a reference word in the title's place, a quoted title and then such a word, or a quoted title next to the
    number on a line that goes on after it. The review of a7e95d92 found the first spelling of this rule read
    only one of the six citation styles below and the number's own position wrongly; the rows after this one
    hold every style it measured.
    """
    old = sec_sections.cut_item(text, form, version=OURS_1)
    new = sec_sections.cut_item(text, form, version=OURS_2)
    assert new[1] == want_quality, name
    assert new == old, name


def test_a_heading_whose_title_is_in_quotation_marks_is_still_a_heading():
    """A filer who puts his own heading's title in quotation marks -- ``Item 1A. "Risk Factors"`` alone on its
    line -- has written a heading, and the line ending right after the closing mark is what says so: a citation
    goes on ("of this report", "for more").

    The first spelling of the citation rule read that line as a citation, and the cost was hidden by a fixture
    that ended with Item 1A. Every real 10-K has an item after it, and with Item 1B there the slice ran through
    the risk factors and was labelled ``ok`` -- the exact answer ``ours-2`` exists to stop giving. ``ours-1``
    gives it too (``ok`` on 15,824 characters, 2,800 of them risk factors), so this is one of the places
    ``ours-2`` is now right where the board is not; the board stays on ``ours-1`` all the same (#755 pins it).
    """
    tail = "\nItem 1B. Unresolved Staff Comments\nNone.\nItem 2. Properties\nNone.\n"
    # A quotation mark the filer never closed on that line says nothing either way, so the heading stands.
    unclosed = "Item 1. Business\n" + _H + "\nItem 1A. “Risk Factors\n" + "Risky. " * 400 + tail
    head = "Item 1. Business\n" + _H + "\nItem 1A. “Risk Factors”\n" + "Risky. " * 400
    # The last two are what the line having to go on **in words** is for: a full stop or a page number after
    # the closing mark is not a sentence carrying on, and before that test both were read as citations.
    for name, text in [("the risk factors end the document", head),
                       ("an Item 1B follows them", head + tail),
                       ("a full stop after the closing mark",
                        "Item 1. Business\n" + _H + "\nItem 1A. “Risk Factors”.\n" + "Risky. " * 400 + tail),
                       ("the page number on the heading's line",
                        "Item 1. Business\n" + _H + "\nItem 1A. “Risk Factors” 12\n" + "Risky. " * 400 + tail),
                       ("an opening mark the filer never closed", unclosed)]:
        item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
        assert quality == "ok" and "Risky." not in item, name
        assert len(item) == len(_H), name
        if text is not unclosed:
            old_item, _ = sec_sections.cut_item(text, "10-K", version=OURS_1)
            assert "Risky." in old_item, f"{name}: ours-1 reads the quoted heading as a citation and runs past it"


@pytest.mark.parametrize("citation,ours_1_is_right", [
    ("Item 1A, “Risk Factors,” of this report.", True),
    ("Item 1A entitled “Risk Factors” of this report.", True),
    ("Item 1A under the caption “Risk Factors” of this report.", True),
    ("Item 1A of this Annual Report, “Risk Factors,” for more.", True),
    ("Item 1A (“Risk Factors”) of this report.", True),
    ("Item 7 of this report under “Liquidity and Capital Resources” for more.", True),
    # the citation with no quotation marks at all, and the two quote shapes a filer's HTML did not curl:
    # ours-1 reads none of these three and stops Item 1 at the citation
    ("Item 1A of this report.", False),
    ('Item 1A, "Risk Factors," of this report.', False),
    ("Item 1A, ‘Risk Factors,’ of this report.", False),
])
def test_every_citation_style_measured_is_a_mention_and_not_a_heading(citation, ours_1_is_right):
    """The six styles the review of a7e95d92 measured, plus the unquoted one and the two quote shapes
    ``ours-1`` cannot read. The first rule looked for a quotation mark within twelve punctuation characters of
    the number, so anything between them -- "entitled", "under the caption", "of this Annual Report,", "(" --
    fell through to the unopened-quote test, which read the citation as a heading: Item 1 cut to 13,024
    characters of 26,0xx and still labelled ``ok``, on six of these nine rows.

    ``ours_1_is_right`` says whether the board already answers this one. Where it does, the two versions must
    agree; where it does not, ``ours-2`` must be the longer answer, because the board stops at the citation.
    """
    text = ("Item 1. Business\n" + _H + " as discussed in Part I,\n" + citation + " " + _H
            + "\nItem 1A. Risk Factors\n" + "Risky. " * 400)
    old = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert new[1] == "ok" and "Risky." not in new[0]
    assert len(new[0]) > 20_000, "the citation must not end the section"
    if ours_1_is_right:
        assert new == old
    else:
        assert len(new[0]) > len(old[0]) and len(old[0]) < 20_000


@pytest.mark.parametrize("citation", [
    "Item 1A, “Risk Factors.”",
    "Item 1A. “Risk Factors.”",
    "Item 1A “Risk Factors.”",
    "Item 7, “Management's Discussion and Analysis.”",
    "Item 1A, “Risk Factors”\nof this report.",
])
def test_a_citation_that_ends_its_paragraph_is_still_a_citation(citation):
    """US punctuation puts the full stop **inside** the quotation marks, so a citation that closes a paragraph
    -- ``For more, see Part I, Item 1A, "Risk Factors."`` -- has nothing after the closing mark either. The
    first spelling of "a quoted title with the line going on after it" therefore read every one of these as a
    heading: Item 1 cut to 13,024 characters of 26,050 and labelled ``ok``, where ``ours-1`` is right.

    What tells them from a filer's own quoted heading is punctuation a title does not carry: a comma between
    the number and the opening mark (a heading's number takes a full stop or nothing), or a full stop, comma,
    semicolon or colon inside the closing mark. Rows 1 and 4 carry both, so the comma rule is pinned by row 5
    alone -- the same citation with the sentence wrapped onto the next line, where nothing else is left.
    """
    text = ("Item 1. Business\n" + _H + " as discussed in Part I,\n" + citation + "\n" + _H
            + "\nItem 1A. Risk Factors\n" + "Risky. " * 400
            + "\nItem 1B. Unresolved Staff Comments\nNone.\nItem 2. Properties\nNone.\n")
    old = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert new == old and new[1] == "ok"
    assert len(new[0]) > 20_000 and "Risky." not in new[0]


@pytest.mark.parametrize("citation", [
    "Item 1A “Risk Factors”).",         # the parenthetical closes after the citation
    "Item 1A. “Risk Factors”)",
    "Item 1A “Risk Factors”);",
    "Item 1A “Risk Factors”.)",
    "Item 1A (“Risk Factors”).",        # and one the citation opened itself
    "Item 1A “Risk Factors”,",          # a clause of a sentence that goes on below
    "Item 1A “Risk Factors”;",
    "Item 1A “Risk Factors”]",         # the square bracket, which the closer class has to name
    "Item 1A “Risk Factors” ,",        # and a clause whose comma the filer spaced away from the mark
    "Item 1A. “Risk Factors,”\nand elsewhere in this report.",   # the mark holds the comma
    "Item 1A. “Risk Factors;”",
    "Item 1A – “Risk Factors” of this report.",              # a dash in place of the full stop
    "Item 1A - “Risk Factors.”",
])
def test_a_citation_whose_line_ends_in_punctuation_is_still_a_citation(citation):
    """"(see Part I,\nItem 1A "Risk Factors")." -- a parenthetical cross-reference, the commonest citation form
    there is. Asking for a **letter** after the closing mark made eleven of these thirteen rows headings: Item
    1 cut to 13,024 characters of 26,050 and labelled ``ok``, and :func:`_holds_another_item` reads the same
    test, so a wrapped citation early in Item 1 turned the label to ``overrun`` as well. The two rows whose
    closing mark holds the punctuation were already right, and are here because they are what pins the set of
    marks that counts inside it.

    Two more things after the mark now say the line goes on, and neither ends a heading's line: a comma or a
    semicolon, spaces allowed before it, and a closing bracket with no opening one between it and the mark. The last two rows
    are the dash a filer puts where the full stop goes, which neither signal could reach before.

    ``ours-1`` answers every row here the same way, which is why they are asserted against it -- but only
    because the rows are written with curly quotation marks, the ones a filer's HTML produces. The same
    citation with straight ones (``Item 1A. "Risk Factors")``) is a heading to ``ours-1``: 13,024 characters
    where ``ours-2`` gives 26,050.
    """
    text = ("Item 1. Business\n" + _H + " as discussed in Part I,\n" + citation + "\n" + _H
            + "\nItem 1A. Risk Factors\n" + "Risky. " * 400
            + "\nItem 1B. Unresolved Staff Comments\nNone.\nItem 2. Properties\nNone.\n")
    old = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert new == old and new[1] == "ok"
    assert len(new[0]) > 20_000 and "Risky." not in new[0]


@pytest.mark.parametrize("after", ["", ".", " 12", " (12)", " [12]", " .......... 12", ":", " —"])
def test_what_may_follow_a_quoted_heading_and_still_leave_it_a_heading(after):
    """The other side of the row above, which is what keeps the two rules apart. A heading's line ends after
    the title, or with a full stop, a colon, a dash, or the page number a contents page prints -- in brackets
    or after leaders. None of those is a sentence, a clause or an unopened bracket, so the line is over and
    ``Item 1A. "Risk Factors"`` is the heading it looks like.

    ``ours-1`` reads every row here as a citation and runs Item 1 through the risk factors.
    """
    text = ("Item 1. Business\n" + _H + "\nItem 1A. “Risk Factors”" + after + "\n" + "Risky. " * 400
            + "\nItem 1B. Unresolved Staff Comments\nNone.\nItem 2. Properties\nNone.\n")
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and "Risky." not in item and len(item) == len(_H)
    old_item, _ = sec_sections.cut_item(text, "10-K", version=OURS_1)
    assert "Risky." in old_item, "ours-1 reads it as a citation"


@pytest.mark.parametrize("heading", [
    "Item 1A.\nIn our business we face risks.\n",
    "Item 1A.\nBelow is a summary of risk factors.\n",
    "Item 1A “Risk Factors”\nIn this section we describe them.\n",
])
def test_a_reference_word_on_the_next_line_does_not_make_a_heading_a_citation(heading):
    """The reference-word half of the citation test is asked of the text after the number, and its leading
    class was spelled with ``\\s``, which crosses a newline. A heading that carries only its number on its
    line -- the title printed underneath, or on the same line in quotation marks -- was then a citation
    whenever the next line opened with "In our", "Below" or another of those words, and the slice ran through
    the risk factors: ``ok`` on 15,840 characters where ``ours-1`` gives 13,000. :func:`_holds_another_item`
    reads the same test, so the label was blind to it too.

    The class now allows only the spaces that keep to the match's line. The third row is the same reach
    through the quoted-run spelling, and there ``ours-2`` is now shorter than ``ours-1`` -- right, because the
    risk factors do not belong to Item 1.
    """
    text = ("Item 1. Business\n" + _H + "\n" + heading + "Risky. " * 400
            + "\nItem 1B. Unresolved Staff Comments\nNone.\nItem 2. Properties\nNone.\n")
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and "Risky." not in item
    assert len(item) == len(_H), "the heading on the next line is the end of Item 1"


@pytest.mark.parametrize("heading,form,mention", [
    ("Item 1. Business “Acme,” “we” and “our” refer to Acme Corp.", "10-K",
     " see “Item 1A. Risk Factors” for more. "),
    ("Item 1. Business. “Acme,” “we” refer to Acme Corp.", "10-K",
     " see “Item 1A. Risk Factors” for more. "),
    ("Item 4. Information on the Company “Acme” means us.", "20-F",
     " see “Item 5. Operating Results” below. "),
    ("Item 1. Business Overview “Acme” refers to us.", "10-K",
     " see “Item 1A. Risk Factors” for more. "),
])
def test_the_citation_rule_is_asked_of_the_number_and_not_of_the_end_of_the_match(heading, form, mention):
    """A **start** pattern ends after the title, a line-end pattern after the number. The first spelling of the
    citation rule read the text after the *match*, so at the start filter it tested what followed "Business" --
    and a filer who defines his own names in the section's first sentence ("Item 1. Business "Acme," "we" and
    "our" refer to Acme Corp.") had his real start thrown away. The pattern path then took over and stopped at
    the first quoted mention in the body: 13,049 characters where the round before gave 26,081, which is the
    run-in start fix of round 4 undone.

    Every row is a run-in start whose first sentence defines a term, and the fourth has a two-word title so
    that the number is further still from the quotation mark.
    """
    stop = "\nItem 1A. Risk Factors\n" if form == "10-K" else "\nItem 5. Operating Results\n"
    text = "Cover page.\n" + heading + _H + mention + _H + stop + "Risky. " * 400
    item, quality = sec_sections.cut_item(text, form, version=OURS_2)
    assert quality == "ok" and "Risky." not in item
    assert len(item) > 20_000, "the real start was thrown away and the pattern path stopped at the mention"


def test_a_quoted_section_name_after_the_title_is_not_read_as_a_citation():
    """``Item 1A. Risk Factors, and the section "Forward-Looking Statements" below.`` The title is exactly where a
    heading's title goes and the quotation marks are around something else, so neither the number's
    neighbourhood nor the quoted run says citation. This row used to pin that as the cost: ``ours-2`` cut 13,024
    where ``ours-1`` gives 26,099. #820's part test now reads "in Part I," with its comma as a reference to Item
    1A's own part, so this spelling is a citation; the same line without the part is still the cost.
    """
    text = ("Item 1. Business\n" + _H + " as discussed in Part I,\nItem 1A. Risk Factors, and the section "
            "“Forward-Looking Statements” below. " + _H + "\nItem 1A. Risk Factors\n" + "Risky. " * 400)
    old = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert new[1] == "ok" and len(new[0]) > 20_000 and "Risky." not in new[0]
    assert len(old[0]) > 20_000, "and the board does read it as a citation"
    without_part = text.replace(" as discussed in Part I,\n", " as discussed below.\n")
    cost = sec_sections.cut_item(without_part, "10-K", version=OURS_2)
    assert cost[1] == "ok" and len(cost[0]) < 20_000, "without the part, the citation still ends the slice"


@pytest.mark.parametrize("name,text", [
    ("the closer is taken from the far end of the window",
     "Item 1. Business\n" + _H + " “Part I,\nItem 1A. Risk Factors” and “Item 7. MD&A” of "
     "this report. " + _H + "\nItem 1A. Risk Factors\n" + "Risky. " * 400),
    ("the window stops at the first newline anywhere",
     "Item 1. Business\n" + _H + " “Part I,\nItem\n1A. Risk Factors” of this report. " + _H
     + "\nItem 1A. Risk Factors\n" + "Risky. " * 400),
    ("the text before the match is not stripped",
     # "for more" and not "of this report": the citation rule reads the second on its own, so with it this row
     # stayed green whether or not the before-text was stripped, and pinned nothing (round 7).
     "Item 1. Business\n" + _H + "\nsee\n\nItem 1A for more. " + _H + "\nItem 1A. Risk Factors\n"
     + "Risky. " * 400),
])
def test_the_details_inside_the_reference_test_each_decide_a_cut(name, text):
    """An earlier note said six mutations inside this test changed no measured answer. Three of them do, and
    these are the shapes that show it -- each one a mention that stops being a mention, which costs the section
    half its text.

    A fourth, moving the window's start from the match to its end, only moves where a 200-character window
    falls: it takes a quotation mark exactly 192 characters of filler past the number to tell the two apart,
    and neither side of that boundary is more right than the other. It is not pinned here, and the one
    genuinely equivalent mutation is named in the function itself.
    """
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert new[1] == "ok", name
    assert len(new[0]) > 20000, f"{name}: the mention must not end the section"
    assert "Risky." not in new[0], name


# ── the 13 Nov 2026 real-filing check ──────────────────────────────────────────────────────────────────────────
# ours-2 over 383 real 10-Ks on homesrv (claude-coordinator notes/revenuechaser/reviews/2026-11-13_0114_10k-section-
# cutter-real-filings.md): five wrong cuts, three of them ``ok``, and a sixth (AES) found by the fix. Each shape below
# is the filing's own boundary text, transcribed; the filler around it is built. Red on the merge-base: each of these
# ends Item 1 at the mention, or (Tempus) runs past the real stop, or (McDonald's) answers ``short``.
def test_cal_maine_a_mention_through_part_ii_that_starts_a_line_does_not_end_item_1():
    """"see Note 15 – Segment Reporting in Part II. / Item 8. Notes to ..." -- the filer's HTML breaks every few
    words, so "Item 8." starts a line and only the words before it ("in Part II.") say it is a mention."""
    text = ("ITEM 1. BUSINESS\nOverview\n" + BODY[:3000] + "\nsee \n Note 15 – Segment Reporting \nin \nPart II. "
            "\nItem \n8. Notes \nto \nConsolidated \nFinancial \n Statements. \n" + BODY[:3000]
            + "\nITEM 1A. RISK FACTORS\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "Segment Reporting" in item and item.endswith(BODY[:3000].strip())


def test_cheniere_and_goldmining_a_mention_on_the_pattern_path_is_not_a_stop():
    """The pattern path took the first titled stop, mention or not: Cheniere's "described in Item 1A. Risk
    Factors ." and U.S. GoldMining's 'See\\n“ Item 2. Properties” for further information'."""
    for mention in ("uncertainties described in Item 1A. Risk Factors . ",
                    "in Alaska. See\n“ Item 2. Properties” for further information. "):
        # "PART I Item 1." on one line: no line-start heading, so this is the pattern path
        text = ("PART I Item 1. Business Overview " + BODY[:3000] + mention + BODY[:3000]
                + " Item 1A. Risk Factors " + "Risky. " * 400)
        item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
        assert quality == "ok", mention
        assert item.endswith(BODY[:3000].strip()), f"{mention!r} ended the section"


def test_tempus_an_item_letter_set_apart_from_its_number_still_ends_item_1():
    """"Item\\u20091 A. Risk Fact ors" on its own line. Unseen, the slice ran through all of Item 1A to Item 1B,
    535,001 characters, and still said ``ok``."""
    text = ("PART I\nItem 1. Business\n" + BODY + "\nItem 1 A. Risk Fact ors \nA description of the risks\n"
            + "Risky. " * 400 + "\nItem 1B. Unresolved Staff Comments\nNone.\n")
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok"
    assert "Risky." not in item


def test_cheniere_items_1_and_2_with_a_full_stop_after_the_first_number_is_a_line_start():
    """"ITEMS 1. AND 2. BUSINESS AND PROPERTIES" was no line-start heading, so Cheniere went to the pattern path,
    and a mention with no reference word before it ("within Item 1A. Risk Factors, these ...") ended Item 1 there.
    The line rule keeps only a stop that starts its own line."""
    text = ("ITEMS 1. AND 2. BUSINESS AND PROPERTIES\n" + BODY[:3000] + " As further described in Risks Relating to"
            " Regulations within Item 1A. Risk Factors, these rigorous regulatory requirements apply. " + BODY[:3000]
            + "\nITEM 1A. RISK FACTORS\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and item.startswith("AND PROPERTIES")
    assert "these rigorous" in item and item.endswith(BODY[:3000].strip())


def test_a_mention_of_another_part_right_before_a_real_heading_does_not_hide_it():
    """The other side of Cal-Maine (Fable review): "... is set forth in Part III." ends a sentence about Part III,
    and the Item 1A heading on the next line is real. A part is a reference only when the item belongs to it."""
    text = ("Item 1. Business\n" + BODY + "\nInformation about our executive officers is set forth in Part III.\n"
            "Item 1A. Risk Factors\n" + "Risky. " * 3000 + "\nItem 1B. Unresolved Staff Comments\nNone.\n")
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and "Risky." not in item


def test_a_tag_split_title_of_item_1_is_not_item_1b():
    """Fable review: "Item 1 B usiness" is Item 1's own title split by a tag; only "1 B." / "1 B:" / "1 B—" is 1B."""
    text = ("Item 1. Business\n" + BODY + "\nItem 1 B usiness (continued)\n" + BODY[:3000]
            + "\nItem 1A. Risk Factors\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and "(continued)" in item and "Risky." not in item


@pytest.mark.parametrize("before,ends_there", [
    ("in Part II.\n Item 8. Notes to Consolidated Financial Statements.", False),   # indented: still a mention
    ("is set forth in Part I.\nItem 1A. Risk Factors", True),     # Item 1's own part, a sentence's end
    ("refer to Part I.\nItem 1A. Risk Factors", False),           # ... unless the words say it is a reference
])
def test_a_part_before_a_line_start_item_is_read_with_the_item_s_own_part(before, ends_there):
    """Fable review, round 2: the part test read no indented line, and Part I hid Item 1's real end."""
    text = ("Item 1. Business\n" + BODY[:3000] + " This " + before + "\n" + BODY[:3000]
            + "\nItem 1A. Risk Factors\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert "Risky." not in item
    assert item.endswith(BODY[:3000].strip()) is not ends_there


def test_a_20_f_is_not_read_with_a_10_k_s_parts():
    """In a 20-F Item 5 is Part I; "in Part II." before it ends a sentence, and Item 5 is Item 4's real end."""
    text = ("Item 4. Information on the Company\n" + BODY + "\nas described in Part II.\n"
            "Item 5. Operating and Financial Review and Prospects\n" + "Results. " * 400)
    item, quality = sec_sections.cut_item(text, "20-F", version=OURS_2)
    assert "Results." not in item


def test_xtl_a_quoted_mention_of_the_start_is_not_the_start_on_the_pattern_path():
    """With mention stops skipped, a start at '“Item 4. Information on the Company,” “Item 5. ...”' no longer ended
    at once and was passed over: it ran to the real stop and won (XTL 20-F, full-disk check). Mention starts are
    skipped by the same words-before test."""
    text = ("as described in “Item 3. Key Information—D. Risk Factors,” “Item 4. Information on the Company,” “Item 5."
            " Operating and Financial Review and Prospects” and elsewhere. " + BODY[:3000]
            + " A. ITEM 4. INFORMATION ON THE COMPANY A. History and Development. " + BODY[:3000]
            + " ITEM 4A. UNRESOLVED STAFF COMMENTS None. ITEM 5. OPERATING " + "Results. " * 400)
    item, quality = sec_sections.cut_item(text, "20-F", version=OURS_2)
    assert quality == "ok" and item.startswith("A. History and Development")


def test_a_bank_s_qualified_by_mention_does_not_end_item_1():
    """First Hawaiian, Cullen/Frost, Valley and Park National open Item 1 with "The disclosures set forth in this
    item are qualified by Item 1A. Risk Factors and the section captioned ..." on the pattern path."""
    text = ("PART I ITEM 1. BUSINESS The disclosures set forth in this item are qualified by Item 1A. Risk Factors and"
            " the section captioned “Forward-Looking Statements” of this report. " + BODY[:3000]
            + " ITEM 1A. RISK FACTORS " + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and item.endswith(BODY[:3000].strip())


# ── #763: the eleven filings that stayed uncut on the 13 Nov 2026 full-disk check ──────────────────────────────
# Six 10-Ks whose heading ``ours-2`` could not read, and five annual reports that carry a Form 10-K/20-F
# cross-reference index instead of item sections. Each input below is the shape of the named filing's own
# heading, read off the stored document on 13 Nov 2026; the filings themselves are not in this tree.

def test_aig_a_pipe_between_the_number_and_the_title_is_a_heading():
    """AIG prints "ITEM 1 | Business" as the heading and again as every page's running header. Neither rule read
    it, and ``ours-1`` cut 58,722 characters out of a pair of cross-references instead -- labelled ``ok``."""
    text = (TOC + "\nPart I\nITEM 1 | Business\n" + BODY[:5000]
            + "\n10\nAIG | 2025 Form 10-K\nTABLE OF CONTENTS\nITEM 1 | Business\n" + BODY[:5000]
            + "\nITEM 1A | Risk Factors\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and item.startswith("We make widgets") and "Risky." not in item
    assert sec_sections.cut_item(text, "10-K", version=OURS_1)[1] != "ok"


def test_eos_a_full_stop_before_the_number_is_a_heading():
    """Eos Energy writes "ITEM. 1 BUSINESS" -- the stop belongs to the word, not to the number."""
    text = (TOC + "\nPART I\nITEM. 1 BUSINESS\nGeneral\n" + BODY[:5000]
            + "\nITEM 1A. RISK FACTORS\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and item.startswith("General") and "Risky." not in item


def test_fb_financial_a_dash_between_the_word_and_the_number_is_a_heading():
    """FB Financial writes "ITEM - 1. Business"; its stop is "ITEM 1A - Risk Factors"."""
    text = (TOC + "\nPART I\nITEM - 1. Business\n" + BODY[:5000]
            + "\nITEM 1A - Risk Factors\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and item.startswith("We make widgets") and "Risky." not in item


def test_brt_an_ell_written_for_the_one_is_still_item_1():
    """BRT Apartments writes "Item l. Business." with the letter ell. ``ours-1`` found no heading and ran
    172,334 characters, ``overrun``."""
    text = (TOC + "\nPART I\nItem l. Business.\nGeneral\n" + BODY[:5000]
            + "\nItem 1A. Risk Factors.\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    # The filer's own full stop after "Business." is the first character of the body.
    assert quality == "ok" and item.lstrip(". \n").startswith("General") and "Risky." not in item


def test_southern_missouri_zero_width_spaces_inside_a_heading_do_not_hide_it():
    """Two defects in one filing. ``strip_html`` leaves ``&#8203;`` where it found it, so the heading reads
    "Item 1. ​ ​ Description of Business"; and its first sentence names Item 1A ("...are qualified by
    Item 1A. Risk Factors"), which the contents test read as a contents entry and threw the heading away. With
    it gone the cut fell through to the pattern path and ran 199,999 characters."""
    text = (TOC + "\nPART I\nItem 1. ​ ​ Description of Business\n"
            "The disclosures set forth in this Item 1. are qualified by Item 1A. Risk Factors and the section"
            " captioned “Forward-Looking Statements”. " + BODY[:5000]
            + "\nItem 1A. Risk Factors\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and item.startswith("The disclosures") and "Risky." not in item
    assert sec_sections.cut_item(text, "10-K", version=OURS_1)[1] != "ok"


def test_exelon_item_1_under_part_i_is_its_heading_whatever_title_follows():
    """Exelon's contents page says "ITEM 1. BUSINESS" and the heading over the body says "ITEM 1. \\n General".
    Item 1 is the first item of Part I, so the line after a "PART I" line is its heading; the contents page's own
    "PART I / ITEM 1." is still thrown out, because the next item is listed right under it."""
    text = ("Table of Contents\nPART I\nITEM 1.\nBUSINESS\n7\nGeneral\n7\nITEM 1A.\nRISK FACTORS\n20\n"
            "\nTable of Contents\nPART I\nITEM 1.\nGeneral\nCorporate Structure\n" + BODY[:5000]
            + "\nITEM 1A. RISK FACTORS\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and item.startswith("General\nCorporate Structure") and "Risky." not in item


def test_mcdonalds_an_annual_report_with_a_form_index_is_crossref_and_not_short():
    """McDonald's, HSBC, Barclays, Pearson and Santander file the annual report itself and map the form's items
    to it in a table. McDonald's index gives a page number and no caption at all; the other four map Item 4.B to
    scattered ranges and notes. There is nothing to cut, and ``crossref`` says so where ``short`` and ``missing``
    read as a cut that went wrong."""
    index = ("Form 10-K Cross-Reference Index\nPage reference\nPart I\nItem 1 Business Page 3\n"
             "Item 1A Risk Factors Page 27\nItem 2 Properties Page 36\n")
    report = "About McDonald's\n" + BODY[:5000] + "\n" + index
    assert sec_sections.cut_item(report, "10-K", version=OURS_2)[1] == "crossref"
    assert sec_sections.cut_item(report, "10-K", version=OURS_1)[1] in ("short", "missing")
    # Without the index the same unreadable filing keeps the label its cut earns: here there is no start at
    # all, so ``missing``. The table is what makes it ``crossref``, not the length.
    assert sec_sections.cut_item("About McDonald's\n" + BODY[:5000], "10-K", version=OURS_2)[1] == "missing"


def test_hsbc_an_index_headed_item_number_and_caption_is_one_too():
    """HSBC's table is headed "Form 20-F Item Number and Caption / Location / Page" and never says
    "cross-reference"; without it the 20-F answers ``missing``, which reads as a cut to try again."""
    text = ("HSBC Holdings plc Annual Report on Form 20-F\n" + BODY[:5000]
            + "\nForm 20-F Item Number and Caption\nLocation\nPage\n"
              "4. Information on the Company\nStrategic report\n14-24\n")
    assert sec_sections.cut_item(text, "20-F", version=OURS_2)[1] == "crossref"


def test_a_contents_page_spelled_the_way_the_heading_is_is_still_a_contents_page():
    """The start pattern learned "ITEM - 1." and the contents test has to learn it too, or the contents entry
    becomes the body: `overrun`, cut from the index (Fable review of this change)."""
    text = ("ITEM - 1. Business 3\nITEM - 1A. Risk Factors 12\nITEM - 1B. Unresolved Staff Comments 35\n"
            "\nPART I\nITEM - 1. Business\n" + BODY[:5000] + "\nITEM - 1A. Risk Factors\n" + "Risky. " * 400)
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert quality == "ok" and item.startswith("We make widgets") and "Risky." not in item


def test_a_sentence_that_names_the_form_is_not_a_cross_reference_index():
    """A bank's Guide 3 index -- "This cross-reference index to Guide 3 disclosures in this Annual Report on
    Form 10-K" -- is a sentence, not the table. Read unanchored it turned a wrongly-short cut into ``crossref``,
    which tells the next run there is nothing to find (Fable review of this change).

    The two halves are one test on purpose. The second alone proves nothing: the merge-base has no
    ``crossref`` label at all, so "this is not crossref" is true there for the wrong reason. The first half is
    what makes the pair red on the base, and the second is what pins the anchoring.
    """
    index = "Form 10-K Cross-Reference Index\nPart I\nItem 1 Business Page 3\n"
    sentence = ("Our business. This cross-reference index to Guide 3 disclosures in this Annual Report on"
                " Form 10-K is provided for convenience. ")
    assert sec_sections.cut_item("About us.\n" + BODY[:5000] + "\n" + index, "10-K",
                                 version=OURS_2)[1] == "crossref"
    assert sec_sections.cut_item("About us. " + sentence + BODY[:5000], "10-K",
                                 version=OURS_2)[1] == "missing"


# ── the per-version suspect flagger (#835) ──────────────────────────────────────────────────────────────────────
#
# ``sec_forms`` now flags its 10-K/20-F business rows (it only flagged the 40-F before), and the flagger is
# ``ours-1``'s. Two of its seven signals are undefined for ``ours-2`` and were measured raising false alarms on
# the 4,988 stored 10-K/20-F filings (13 Nov 2026, real documents on disk, read-only): S8 marks every long slice
# because ``ours-2`` lifts the 200,000 cap, and S6 marks a correct slice whose Item 1 holds a sub-item heading
# (``ours-2`` answers that in the cut, as ``overrun``, so no ``ok`` slice reaches the flagger holding one). The
# version carries which signals it defines (:attr:`sec_sections.Rule.signals`), and :func:`sec_sections.cut_flag`
# reads it, so a stored row's ``rules_version`` names its flagger as well as its cut.

_LONG = "We make widgets and sell them worldwide. " * 6_000          # > MAX_ITEM_CHARS, one clean Item 1
_LONG_K = "Item 1. Business\n" + _LONG + "\nItem 1A. Risk Factors\nRisky.\n"
# A correct Item 1 that carries an 8-K-style sub-item heading inside it (PCVX, #755): ``ours-1``'s S6 reads the
# sub-item as a section end and flags it; ``ours-2`` cuts past it and the slice is a clean ``ok``.
_SUBITEM_K = ("Item 1. Business\n" + BODY + "\nItem 2.01 Completion of Acquisition of Assets\n" + BODY
              + "\nItem 1A. Risk Factors\n")


def test_ours_2_does_not_raise_s8_on_a_long_slice_but_bare_flag_does():
    """S8 compares the slice to ``MAX_ITEM_CHARS``. ``ours-2`` caps nothing, so a long slice is normal, not
    suspect -- 160 of the 173 slices the bare flagger marked on the stored filings were this false alarm."""
    item, quality = sec_sections.cut_item(_LONG_K, "10-K", version=OURS_2)
    assert quality == "ok" and len(item) >= sec_sections.MAX_ITEM_CHARS
    assert sec_sections.cut_flag(quality, _LONG_K, item, version=OURS_2) == "ok"
    assert "S8" in sec_sections.flag(quality, _LONG_K, item)          # the bare flagger still raises it


def test_ours_2_does_not_raise_s6_on_a_slice_holding_a_sub_item_but_bare_flag_does():
    """S6 reads a sub-item as a heading of another item. ``ours-2`` cuts correctly past it, so its ``ok`` slices
    never hold one; the signal only ever fired as a false alarm on this version, so it is not defined for it."""
    item, quality = sec_sections.cut_item(_SUBITEM_K, "10-K", version=OURS_2)
    assert quality == "ok" and "Item 2.01" in item
    assert sec_sections.cut_flag(quality, _SUBITEM_K, item, version=OURS_2) == "ok"
    assert "S6" in sec_sections.flag(quality, _SUBITEM_K, item)       # the bare flagger still raises it


def test_ours_2_still_raises_the_signals_it_defines():
    """The five defined signals (S1-S5) are the point of the flag: a cut that ends on a cross-reference is
    ``ok;suspect:S4`` under both versions. BAT's 20-F Item 4 ends "...refer to notes 13, 14 and 27 in Part III"
    (real filing 1303523, 13 Nov 2026), the shape S4 is for."""
    text = ("Item 1. Business\n" + BODY + "\nAvailable Information\nWe file with the SEC. "
            "refer to notes 13, 14 and 27 in Part III\nItem 1A. Risk Factors\n")
    item, quality = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert sec_sections.cut_flag(quality, text, item, version=OURS_2) == "ok;suspect:S4"


def test_the_signal_sets_are_the_versions_own_and_s6_s8_are_ours_1_only():
    """The sets are declared, not inferred: ``ours-1`` keeps every signal (so every existing caller of ``flag``
    is unchanged), and ``ours-2`` drops exactly S6 and S8."""
    assert sec_sections.rule(OURS_1).item_signals == sec_sections.SIGNALS_OURS_1
    assert sec_sections.rule(OURS_2).item_signals == sec_sections.SIGNALS_OURS_2
    assert set(sec_sections.SIGNALS_OURS_1) - set(sec_sections.SIGNALS_OURS_2) == {"S6", "S8"}


@pytest.mark.parametrize("name", ["suspect_tail_cross_reference", "suspect_inner_item_header"])
def test_cut_flag_defaults_to_ours_1_and_equals_bare_flag_there(name):
    """``cut_flag`` with no version is ``DEFAULT_VERSION`` (``ours-1``), whose signal set is every signal, so it
    is byte-identical to the bare :func:`flag` every current caller uses.

    The two inputs are the shared cases where ``flag`` answers something other than the label it was handed, so
    the comparison reaches the signal dict and the filter. An input whose label is not ``ok`` would compare two
    early returns and pass however ``cut_flag`` were written -- which is what the first version of this test did.
    """
    text, form = _BY_NAME[name]
    item, quality = sec_sections.cut_item(text, form, version=OURS_1)
    assert quality == "ok", "the case must reach the flagger, not the quality != ok early return"
    flagged = sec_sections.flag(quality, text, item, form)
    assert flagged.startswith("ok;suspect:"), flagged
    assert sec_sections.cut_flag(quality, text, item, form) == flagged


# ── the AIF cutter's own signal set (#838) ──────────────────────────────────────────────────────────────────────
#
# A version has two cutters and #835 gave it one signal set, so the ``sec_forms`` layer asked a 40-F's Annual
# Information Form the questions it asks a 10-K's Item 1. S3 (does the slice carry ``competition``, ``human
# capital``, ``employees`` or ``available information``) and S6 (does it hold a line-start item number other than
# Item 1) are item-shaped; NI 51-102 Form 51-102F2 asks an AIF for none of them. ``flag`` trips on ``S2 and S3``
# and S2 -- a small share of a big document -- is what the AIF of a long 40-F is by construction, so a correct
# AIF read ``ok;suspect:S2,S3``. ``module_relatedness.locator.asks`` fires on any ``;suspect``, so that sent a
# correct business row to the writer model: a paid call per 40-F on a board running a paid provider.
#
# The case below is the shape measured during the #837 review: a 40-F past the 150,000-character S2 threshold
# whose AIF business block is about 2.5% of it and carries no US heading. It is built, not a stored filing,
# because the suite is offline; the rate on the real documents is in the PR report.

#: An AIF business block just over :data:`sec_sections._BODY_MIN` (4,000) and over ``MIN_ITEM_CHARS``, so the cut
#: is a real ``ok`` one and S1 stays quiet. No 10-K heading in it, which is the whole point: an AIF has none.
_AIF_BODY = "We make widgets and sell them to customers worldwide. " * 80
#: Filler that is neither an AIF title nor a 10-K heading, one sentence per line and no page numbers, so the
#: contents-entry check does not read it as a table of contents.
_40F_FILLER = "The registrant files this annual report with the Commission.\n" * 1_300
#: The long 40-F: the AIF block sits between the two halves of the filler, so S5 reads ordinary prose in front
#: of it and S2's "small share of a big document" is true -- which is what an AIF of a long 40-F always is.
_LONG_40F = (_40F_FILLER + "annual information form\nGENERAL DEVELOPMENT OF THE BUSINESS\n"
             + _AIF_BODY + "\nRISK FACTORS\nWe face risks.\n" + _40F_FILLER)


@pytest.mark.parametrize("version", [OURS_1, OURS_2])
def test_a_correct_aif_of_a_long_40_f_is_not_suspect(version):
    """The bug and the fix in one case, for both versions, because ``ours-1`` is what ``sec_forms`` stores with
    today: asked with the ITEM set the block is ``ok;suspect:S2,S3``, asked with its own cutter's set it is a
    bare ``ok``. The cut is the same text either way -- only the label moves."""
    aif = sec_sections.cut_aif_business(_LONG_40F, version=version)
    assert aif is not None and len(aif) >= sec_sections.MIN_ITEM_CHARS
    assert len(_LONG_40F) > 150_000 and len(aif) < 0.03 * len(_LONG_40F), "the case must reach S2"

    assert sec_sections.cut_flag("ok", _LONG_40F, aif, "40-F", version=version, aif=True) == "ok"
    # What the layer asked before #838, and still asks for an item cut: the two item-shaped signals fire.
    item_set = sec_sections.cut_flag("ok", _LONG_40F, aif, "40-F", version=version)
    assert item_set == "ok;suspect:S2,S3", item_set


@pytest.mark.parametrize("version", [OURS_1, OURS_2])
def test_the_aif_set_still_raises_the_signals_that_read_the_text(version):
    """Dropping S3 and S6 is not switching the flag off: a block that ends mid cross-reference is still
    ``ok;suspect:S4`` on the AIF path, and S2 rides along as context rather than as the reason."""
    text = _LONG_40F.replace("\nRISK FACTORS\n", " For a description of our mines, see\nRISK FACTORS\n", 1)
    aif = sec_sections.cut_aif_business(text, version=version)
    assert aif is not None
    flagged = sec_sections.cut_flag("ok", text, aif, "40-F", version=version, aif=True)
    assert flagged.startswith("ok;suspect:") and "S4" in flagged, flagged
    assert "S3" not in flagged and "S6" not in flagged, flagged


def test_the_aif_sets_are_the_versions_own_and_drop_exactly_the_item_shaped_signals():
    """Declared, like the item sets: each version's AIF set is its item set without S3 and S6, and ``ours-1``
    keeps S8 because its AIF block caps while ``ours-2``'s does not."""
    assert sec_sections.rule(OURS_1).aif_signals == sec_sections.AIF_SIGNALS_OURS_1
    assert sec_sections.rule(OURS_2).aif_signals == sec_sections.AIF_SIGNALS_OURS_2
    for item_set, aif_set in ((sec_sections.SIGNALS_OURS_1, sec_sections.AIF_SIGNALS_OURS_1),
                              (sec_sections.SIGNALS_OURS_2, sec_sections.AIF_SIGNALS_OURS_2)):
        assert set(item_set) - set(aif_set) == {"S3", "S6"} & set(item_set)
        assert not set(aif_set) - set(item_set), "the AIF set adds no signal of its own"
    assert "S8" in sec_sections.AIF_SIGNALS_OURS_1 and "S8" not in sec_sections.AIF_SIGNALS_OURS_2
