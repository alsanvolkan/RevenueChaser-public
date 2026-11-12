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

from .sec_sections_cases import BODY, CASES

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
    # #756 is where the AIF block improves; a version that moved two rules at once would leave nobody able to say
    # which of them moved a stored section.
    assert sec_sections.rule(OURS_2).aif_business is sec_sections.rule(OURS_1).aif_business


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
    assert "1a" in sec_sections.STOP_PATTERNS_2["10-K"] and "1b" in sec_sections.STOP_PATTERNS_2["10-K"]
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

    What tells them from a filer's own quoted heading is punctuation a title does not carry: the comma between
    the number and the opening mark (rows 1, 4 and 5 -- a heading's number takes a full stop or nothing), or
    the full stop inside the closing mark (rows 2 and 3). The last row is the same citation with the sentence
    wrapped onto the next line, where the comma is the only signal left.
    """
    text = ("Item 1. Business\n" + _H + " as discussed in Part I,\n" + citation + "\n" + _H
            + "\nItem 1A. Risk Factors\n" + "Risky. " * 400
            + "\nItem 1B. Unresolved Staff Comments\nNone.\nItem 2. Properties\nNone.\n")
    old = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert new == old and new[1] == "ok"
    assert len(new[0]) > 20_000 and "Risky." not in new[0]


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
    """The one citation style measured in the review of a7e95d92 that ``ours-2`` still reads as a heading, kept
    here as a row rather than as prose: ``Item 1A. Risk Factors, and the section "Forward-Looking Statements"
    below.`` The title is exactly where a heading's title goes and the quotation marks are around something
    else, so neither the number's neighbourhood nor the quoted run says citation.

    ``ours-1`` answers 26,099 here and ``ours-2`` 13,024, which is the only shape in this file where the board
    is right and this version is not. A combined heading -- ``Item 1A. Risk Factors, and Item 1B. Unresolved
    Staff Comments`` -- is the same shape with the opposite answer, and the words after the comma are all there
    is to tell them apart, so the measurement belongs to the hand-checked set (#763) and not to a built string.
    """
    text = ("Item 1. Business\n" + _H + " as discussed in Part I,\nItem 1A. Risk Factors, and the section "
            "“Forward-Looking Statements” below. " + _H + "\nItem 1A. Risk Factors\n" + "Risky. " * 400)
    old = sec_sections.cut_item(text, "10-K", version=OURS_1)
    new = sec_sections.cut_item(text, "10-K", version=OURS_2)
    assert new[1] == "ok" and len(new[0]) < 20_000, "the citation ends the slice, which is the cost"
    assert len(old[0]) > 20_000, "and the board does read it as a citation"


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
