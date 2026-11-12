"""#756: ``ours-2``'s business section of a 40-F's Annual Information Form, and ``ours-1`` left where it is.

Each test is named after the filing whose shape it is, from the per-filing table of the 12 Nov 2026 measurement
(``2026-11-12_1200_sec-40f-business-section.md``, the note issue #756 quotes). That measurement is the whole
reason this rule exists: of 137 stored 40-F filings, ``ours-1``'s ``aif_business`` found a section in **none**,
and of the 88 stored AIF exhibits it cut 44 and missed 40 -- 28 with no heading it knows, 5 with a heading and no
line-start stop after it, 7 where every candidate pair was a contents line or a body under 4,000 characters.

**What these tests cannot do**, the same limit as the ours-2 item tests: the filings are not in this tree and no
test fetches one, because an SEC request needs the owner's word. The inputs are the shapes the measurement
describes, built here. A passing run says the rule does what #756 asked of it, not that PBA's real exhibit cuts
correctly -- that is #763's hand-checked set, measured by the first full cut.

**Why the section's end is ours and not edgartools'.** On the 44 exhibits where both cutters fire, edgartools'
section is shorter every single time, median four times shorter, because it ends at the next detected heading of
any kind; six of its eighty-six cuts are under 1,000 characters, BTG's being 338. So the stops here are a list of
real section titles and a short body is rejected rather than returned -- which is what ``test_btg_*`` pins.

Red on the merge-base: ``sec_sections.cut_aif_business_q`` and ``_AIF_START_2`` do not exist there, so every test
below fails at the first call.
"""
from __future__ import annotations

import pytest

from RevenueChaser.rcdata import sec_sections

from .sec_sections_cases import BODY

OURS_1, OURS_2 = sec_sections.OURS_1, sec_sections.OURS_2

#: What makes a document an Annual Information Form at all, for both versions: the phrase itself. Every one of
#: the 137 stored primaries carries it, which is why it is a precondition and never evidence of a section.
AIF = "This Annual Information Form is dated 24 March 2026.\n"


def _cut2(text: str) -> tuple[str | None, str]:
    """``(section, quality)`` under ``ours-2``."""
    return sec_sections.cut_aif_business_q(text, version=OURS_2)


# ── the four misses the measurement counted ────────────────────────────────────────────────────────────────────
def test_pba_a_business_heading_titled_the_other_way_is_a_heading_too():
    """**The 28.** ``ours-1`` knows one start title, ``GENERAL DEVELOPMENT OF THE BUSINESS``, and the NI 51-102
    business block is also written under ``DESCRIPTION OF THE BUSINESS``, ``BUSINESS OF THE COMPANY`` and
    ``BUSINESS OVERVIEW``. PBA's is ``DESCRIPTION OF PEMBINA'S BUSINESS AND OPERATIONS``: 458,578 characters of
    exhibit on disk, 190,279 of which edgartools cuts as the section and ``ours-1`` answers nothing at all.

    The trailing ``AND OPERATIONS`` is the one tail the start pattern allows, and PBA is the filing it is for.
    """
    text = AIF + "DESCRIPTION OF PEMBINA'S BUSINESS AND OPERATIONS\n" + BODY + "\nRISK FACTORS\nRisky.\n"
    assert sec_sections.cut_aif_business(text, version=OURS_1) is None, \
        "ours-1 is one start title, and this is not it -- the 137-way zero"
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()


def test_exk_a_heading_with_no_line_start_stop_after_it_still_ends_somewhere():
    """**The 5.** EXK's exhibit carries the heading and then runs the next section's title into the line above
    it -- ``...completed by the Company during its most recently completed financial year. 9 ITEM 4: RISK
    FACTORS``. ``ours-1`` wants ``RISK FACTORS`` alone on its line, finds none, and answers nothing on 455,636
    characters that edgartools cuts 15,760 out of.

    So when no line-start stop follows the start, the same titles are looked for wherever they appear. The label
    stays ``ok``: a stop was found, and it is the filer's line breaks that are unusual, not the cut.
    """
    tail = "No significant acquisitions were completed. 9 ITEM 4: RISK FACTORS The Company's business is risky.\n"
    text = AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY + "\n" + tail
    assert sec_sections.cut_aif_business(text, version=OURS_1) is None
    section, quality = _cut2(text)
    assert quality == "ok"
    assert "RISK FACTORS" not in section, "the run-in title ends the section even though it starts no line"
    assert section.startswith("We make widgets") and section.endswith("ITEM 4:")


def test_btg_a_three_hundred_character_body_is_not_the_business_section():
    """**The 7, and edgartools' short tail.** BTG's exhibit is 568,780 characters; edgartools cuts 338 of them,
    because the next heading it detected was the following line. A 338-character answer is worse than none: it
    reads as a section that was found, and it is a contents line or a cross-reference that got through.

    So a candidate pair shorter than ``_AIF_MIN`` is rejected and the **next start is tried**, which is where the
    real body is. Rejecting the pair is not the same as giving up on the document.
    """
    stub = "We are a responsible international gold producer headquartered in Vancouver, Canada. " * 4
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + stub
            + "\nMATERIAL PROPERTIES\n" + "Four operating mines. " * 50
            + "\nBUSINESS OVERVIEW\n" + BODY + "\nRISK FACTORS\n")
    assert len(stub) < sec_sections._AIF_MIN, "the first pair has to be the short one"
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()
    assert stub.strip() not in section and "MATERIAL PROPERTIES" not in section


def test_cnq_the_aif_written_into_the_40_f_itself_is_cut_at_the_description_and_not_before_it():
    """**The 3 primaries.** CNQ and CNI write the Annual Information Form into the 40-F itself, which is the
    shape ``ours-1`` was written for, and it is three filings of the 137. Such a filing prints ``GENERAL
    DEVELOPMENT OF THE BUSINESS`` first and the description after it, which is why that title is ``ours-2``'s
    first **stop** and not a start: ``ours-1`` returns both blocks and everything between, ``ours-2`` returns the
    description alone.

    Both answers are a section. Which one a board wants is not settled here -- the boards stay on ``ours-1``
    (``splitter.VERSION``), and this test exists to pin that the two versions differ on the same input.
    """
    text = (AIF + "GENERAL DEVELOPMENT OF THE BUSINESS\n" + "Three year history. " * 300
            + "\nDESCRIPTION OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n")
    old = sec_sections.cut_aif_business(text, version=OURS_1)
    section, quality = _cut2(text)
    assert old is not None and old.startswith("Three year history.")
    assert "DESCRIPTION OF THE BUSINESS" in old, "ours-1 spans several NI 51-102 sections"
    assert quality == "ok" and section == BODY.strip()
    assert len(section) < len(old)


def test_acb_a_business_title_inside_a_sentence_is_not_a_heading():
    """**Class B.** ACB's primary document carries the words ``DESCRIPTION OF THE BUSINESS`` and no heading:
    it is the exhibit index saying where the AIF is. Our start titles are line-anchored for exactly this, and
    edgartools, which searches anywhere, cuts nothing here either.

    A document whose only business title is in a sentence holds no section, and the answer is ``missing`` --
    not a cut of whatever follows the sentence.
    """
    text = (AIF + "For a DESCRIPTION OF THE BUSINESS of the Company, see Exhibit 99.1 to this report.\n"
            + BODY + "\nRISK FACTORS\n")
    assert _cut2(text) == (None, "missing")


# ── the label ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_start_with_no_stop_at_all_is_overrun_and_nothing_is_cut_off_it():
    """``ours-1`` caps its AIF text at ``MAX_ITEM_CHARS`` and five of the stored cuts sat exactly there, which
    says "200,000 characters" and not "this ran to the end of the document". ``ours-2`` caps nothing -- the
    ``sec_forms`` layer stores a section whole -- and says ``overrun`` instead.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + "w " * 120_000
    section, quality = _cut2(text)
    assert quality == "overrun"
    assert len(section) > sec_sections.MAX_ITEM_CHARS, "the length is told, not trimmed"
    assert sec_sections.rule(OURS_2).caps is False


def test_a_document_with_no_annual_information_form_phrase_holds_no_section():
    """The precondition both versions share, and the only one ``ours-1``'s 137 primaries all passed."""
    text = "DESCRIPTION OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n"
    assert _cut2(text) == (None, "missing")
    assert sec_sections.cut_aif_business(text, version=OURS_2) is None
    assert _cut2("") == (None, "missing") and _cut2(None) == (None, "missing")


@pytest.mark.parametrize("heading,why", [
    ("DESCRIPTION OF THE BUSINESS\n12", "a page number on the next line is a contents entry"),
    ("DESCRIPTION OF THE BUSINESS\n 7 ", "the page number may be spaced"),
    ("see\nDESCRIPTION OF THE BUSINESS", "a reference word before it, even across the line break"),
    ("refer to\nBUSINESS OVERVIEW", "the same, in the other wording"),
    ("under\nBUSINESS OF THE COMPANY", "and in the third"),
])
def test_a_contents_entry_and_a_cross_reference_are_not_the_start_of_the_section(heading, why):
    """The two tests ``ours-2`` asks of every start, and the 7 are where ``ours-1`` asked only the first.

    The reference test reads the forty characters before the match with the newline stripped off, so a filer
    whose page ends on the word "see" and whose next page opens with the heading loses that heading. ``ours-1``
    does the same in its own line rule, so this is parity and not a regression; which answer is right belongs to
    the hand-checked set (#763) and not to a string built here.
    """
    assert _cut2(AIF + heading + "\n" + BODY + "\nRISK FACTORS\n") == (None, "missing"), why


def test_a_cross_reference_to_the_risk_factors_does_not_end_the_section_early():
    """The same test on the other end: a stop that is a mention is skipped and the next one taken, so the body
    is not cut off at the first filing that happens to say "see Risk Factors" on a line of its own.
    """
    text = (AIF + "BUSINESS OF THE COMPANY\n" + BODY + "\nFor the risks of our business see\nRISK FACTORS\n"
            + BODY + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(text)
    assert quality == "ok" and "RISK FACTORS" in section
    assert section.endswith(BODY.strip()[-40:]), "the section runs on to the stop that is really one"


# ── the version table ──────────────────────────────────────────────────────────────────────────────────────────
def test_only_a_version_that_labels_its_aif_section_answers_cut_aif_business_q():
    """``ours-1``'s block answers text or nothing, and the board never asked it for more, so a label invented
    here would be behaviour invented here. It raises instead.
    """
    assert sec_sections.rule(OURS_1).aif_business_q is None
    assert sec_sections.rule(OURS_2).aif_business_q is not None
    with pytest.raises(ValueError, match="no labelled AIF cutter"):
        sec_sections.cut_aif_business_q(AIF, version=OURS_1)
    with pytest.raises(ValueError):
        sec_sections.cut_aif_business_q(AIF, version=None)


def test_the_text_only_caller_gets_the_same_text_the_labelled_one_does():
    """``cut_aif_business`` is what the boards call. ``ours-2`` must not answer it differently from its own
    labelled cutter: one rule, two signatures.
    """
    text = AIF + "DESCRIPTION OF OPERATIONS\n" + BODY + "\nLEGAL PROCEEDINGS\n"
    assert sec_sections.cut_aif_business(text, version=OURS_2) == _cut2(text)[0] == BODY.strip()


@pytest.mark.parametrize("title", [
    "DESCRIPTION OF THE BUSINESS",
    "NARRATIVE DESCRIPTION OF THE BUSINESS",
    "DESCRIPTION OF BUSINESSES",
    "BUSINESS OF THE COMPANY",
    "BUSINESS OPERATIONS",
    "BUSINESS OVERVIEW",
    "DESCRIPTION OF OPERATIONS",
    "6. DESCRIPTION OF THE BUSINESS",
    "ITEM 4 BUSINESS OVERVIEW",
    "3.1 BUSINESS OF THE COMPANY:",
    "Description of the Business",
])
def test_which_titles_open_the_business_block(title):
    """The six titles, with the number and the punctuation the measurement saw in front of and behind them, and
    in lower case because ``strip_html`` keeps the filer's own casing.
    """
    section, quality = _cut2(AIF + title + "\n" + BODY + "\nRISK FACTORS\n")
    assert quality == "ok" and section == BODY.strip()


@pytest.mark.parametrize("title", [
    "GENERAL DEVELOPMENT OF THE BUSINESS",
    "THREE-YEAR HISTORY",
    "3 YEAR HISTORY",
    "DESCRIPTION OF CAPITAL STRUCTURE",
    "MATERIAL PROPERTIES",
    "LEGAL PROCEEDINGS",
    "LEGAL MATTERS",
    "RISK FACTORS",
    "CODE OF BUSINESS CONDUCT",
    "MARKET FOR SECURITIES",
    "DIRECTORS AND EXECUTIVE OFFICERS",
    "ITEM 8. RISK FACTORS",
])
def test_which_titles_close_it(title):
    """The nine stops, which are the parts of an Annual Information Form that follow the business block in NI
    51-102's own order. A title that is not one of them leaves the section running, which is the ``overrun``
    label and not a shorter cut.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + "\n" + title + "\n" + "After. " * 50
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()


# ── what the mutant battery found the tests above could not see ────────────────────────────────────────────────
# 52 mutants of the 40-F rule, each judged on whether the ANSWERS move -- the section, its label and ours-1's own
# text over 67 inputs. Six of them survived the tests above and the shapes below are what kill them. Two survive
# still, and both are explained rather than pinned:
#
# * **the third start title.** ``DESCRIPTION OF BUSINESS`` is already matched by the first pattern, whose "the"
#   and whose possessive word are both optional, so removing it changes no answer. It stays because the six
#   titles are adapted one-to-one from the list named in the provenance block, and a list that silently drops an
#   entry is a list nobody can check against its source.
# * **``_is_ref`` in place of ``_ref_ours_2`` at the start filter.** The two differ only on what follows the
#   match on its line, and a start here is line-anchored: its line ends at the title, so both read the same empty
#   window and the before-test they share decides. ``_ref_ours_2`` is used because every other ``ours-2`` call
#   site uses it, not because an input tells them apart -- and none can.
def test_a_title_that_ends_a_line_but_does_not_begin_one_is_not_a_heading():
    """The line anchor's other half. The ``$`` is what rejects ACB's sentence; this is what the ``^`` rejects --
    a sentence that happens to end on the title, which is the shape a running page footer leaves behind.
    """
    text = AIF + "Each segment is described in our DESCRIPTION OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n"
    assert _cut2(text) == (None, "missing")


def test_a_stop_title_that_ends_a_line_but_does_not_begin_one_does_not_end_the_section():
    """The same anchor on the stop, where getting it wrong costs text rather than a section: the body would end
    at the first sentence that mentions the risk factors.
    """
    text = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\nWe now turn to our RISK FACTORS\n" + BODY
            + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(text)
    assert quality == "ok" and "We now turn to our RISK FACTORS" in section


def test_a_quoted_mention_inside_a_line_is_not_the_inline_stop_either():
    """The fallback's own filter. Once no line-start stop exists, the titles are read wherever they appear, and
    a filing that says ``see "RISK FACTORS" below`` in its first paragraph would otherwise lose the whole body.
    With nothing left to stop it the section runs to the end of the document, which is ``overrun`` -- the label
    says so instead of a short cut pretending to be a clean one.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + " For more see “RISK FACTORS” below in this document. " + BODY
    section, quality = _cut2(text)
    assert quality == "overrun" and "RISK FACTORS" in section


def test_an_inline_stop_that_begins_its_line_is_a_heading_even_after_the_word_see():
    """Why the fallback uses :func:`_inline_ref` and not the stricter test: a filer whose page ends on the word
    "see" and whose next page opens with the heading has written a heading. The reference test applies to a
    match **inside** a line only, which is the same correction ``ours-2``'s item rule carries.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + "\nsee\nRISK FACTORS The Company's business is risky. " + BODY
    section, quality = _cut2(text)
    assert quality == "ok" and "RISK FACTORS" not in section


def test_the_last_resort_start_is_read_in_both_the_spellings_ours_1_reads():
    """``ours-1``'s start title accepts "of the business" and "of our business", and the last-resort start is
    there to keep every section ``ours-1`` finds. Reading one spelling of the two would lose some of them.
    """
    text = AIF + "GENERAL DEVELOPMENT OF OUR BUSINESS\n" + BODY + "\nRISK FACTORS\n"
    assert sec_sections.cut_aif_business(text, version=OURS_1) is not None
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()


def test_the_body_minimum_is_measured_on_the_body_and_not_from_the_heading():
    """Ten characters under the limit, which is less than the heading line is long: measuring from the start of
    the heading instead of the start of the body would accept this pair. The limit is about how much prose the
    section holds, so the heading cannot count towards it.
    """
    short = BODY[:3_990]
    text = AIF + "BUSINESS OVERVIEW\n" + short + "\nRISK FACTORS\n" + BODY
    assert len(short) < sec_sections._AIF_MIN < len("BUSINESS OVERVIEW\n") + len(short)
    assert _cut2(text) == (None, "missing"), "and the next start is tried -- there is none here"
