"""#755: ``ours-2``, the five defects of the plan's section 5 fixed, and the boards left on ``ours-1``.

Each test is named after the filing the defect was measured on, and every one of them is a **difference between
the two versions on the same input**: the input is the thing that was really seen, ``ours-1``'s answer is what the
census recorded, and ``ours-2``'s is what this issue promises. Four of the inputs are already in
``sec_sections_cases.py``, because the move's golden file records those defects rather than hiding them
(``split_word_item_1a``, ``item_1_01_in_exhibit_list``, ``over_the_cap``, ``short_body``); taking them from there
keeps one copy of each and lets the golden go on proving that ``ours-1`` did not move.

**What these tests cannot do.** The filings themselves are not in this tree and no test fetches one -- an SEC
request needs the owner's word. So the inputs are the shapes the census describes, built here, and a passing run
says the rule now does what #755 asked of it, not that the real VFF filing cuts correctly. That is #763's
hand-checked set, and the first full cut is what measures it.

Red on the merge-base: ``sec_sections.OURS_2`` does not exist there, so every test below fails at import or at
the first ``version=OURS_2``.
"""
from __future__ import annotations

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
