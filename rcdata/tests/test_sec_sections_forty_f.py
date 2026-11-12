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

import time

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
    assert section.startswith("We make widgets")
    # The run-in line's own page number and item label stay in the body ("... 9 ITEM 4:"): the stop is the
    # title, and what the filer printed in front of it on that line is not ours to trim. #763's set is where
    # a residue like this gets judged against the filing.
    assert section.endswith("9 ITEM 4:")


def test_btg_a_stop_printed_under_the_heading_is_a_subheading_and_not_the_end():
    """**edgartools' short tail.** BTG's exhibit is 568,780 characters; edgartools cuts 338 of them, because
    the next heading it detected was the following line. A 338-character answer is worse than none: it reads as
    a section that was found, and what it really found is the block's own first subheading.

    So a stop has to stand at least ``_AIF_MIN`` past the start of the body, which is ``ours-1``'s rule as
    well. The heading it skips stays inside the section, where the filer put it.
    """
    stub = "We are a responsible international gold producer headquartered in Vancouver, Canada. " * 4
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + stub
            + "\nMATERIAL PROPERTIES\n" + "Four operating mines. " * 50
            + "\nBUSINESS OVERVIEW\n" + BODY + "\nRISK FACTORS\n")
    assert len(stub) < sec_sections._AIF_MIN, "the first stop has to be the close one"
    section, quality = _cut2(text)
    assert quality == "ok" and section.startswith(stub.strip()[:40])
    assert "MATERIAL PROPERTIES" in section and section.endswith(BODY.strip()[-40:])
    assert len(section) > sec_sections._AIF_MIN


def test_a_start_whose_whole_reachable_body_is_short_is_not_the_section():
    """The other half of the minimum, and the 7 the measurement counted: a contents line or a cross-reference
    that got through, with nothing but a few words behind it. Such a start is rejected and the next tried;
    here there is no next one, so the answer is ``missing`` and not sixty characters labelled ``overrun``.
    """
    text = AIF + BODY + "\nDESCRIPTION OF THE BUSINESS\n" + "See the exhibit. " * 3
    assert _cut2(text) == (None, "missing")


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
    whose page ends on the word "see" and whose next page opens with the heading loses that heading. That is a
    cost of this version and **not** parity with ``ours-1``, whose AIF rule applies no reference test at all --
    round 1's review corrected that claim here. It is why the last-resort start is asked only what ``ours-1``
    asks, so the cost falls on the titles ``ours-1`` never read. Which answer is right belongs to the
    hand-checked set (#763) and not to a string built here.
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
    # added in round 1: NI 51-102's item 5, and the two real item titles the line anchor was losing
    "DIVIDENDS",
    "DIVIDENDS AND DISTRIBUTIONS",
    "CAPITAL STRUCTURE",
    "LEGAL PROCEEDINGS AND REGULATORY ACTIONS",
    "DIRECTORS AND EXECUTIVE OFFICERS OF THE COMPANY",
    "THREE YEAR HISTORY",
])
def test_which_titles_close_it(title):
    """The nine stops, which are the parts of an Annual Information Form that follow the business block in NI
    51-102's own order. A title that is not one of them leaves the section running, which is the ``overrun``
    label and not a shorter cut.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + "\n" + title + "\n" + "After. " * 50
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()
    # and through the line-start pattern itself: with the inline fallback behind it, eleven of these twelve
    # rows passed when _AIF_STOP_2 was made never to match (round 1's review), so the path is asserted too.
    assert sec_sections._AIF_STOP_2.search("\n" + title + "\n") is not None


# ── what the mutant battery found the tests above could not see ────────────────────────────────────────────────
# 51 mutants of the 40-F rule, each judged on whether the ANSWERS move -- the section, its label and ours-1's own
# text over 92 inputs. Every start title and every stop title is dropped in turn, generated from the source and
# not anchored by hand, because round 1's rewrite moved all of their spellings and an anchor that no longer
# matches is a mutant that never ran. Three survive, and each is explained rather than pinned:
#
# * **the third start title.** ``DESCRIPTION OF BUSINESS`` is already matched by the first pattern, whose "the"
#   and whose possessive word are both optional, so removing it changes no answer. It stays because the titles
#   are adapted one-to-one from the list named in the provenance block, and a list that silently drops an entry
#   is a list nobody can check against its source.
# * **``_is_ref`` in place of ``_ref_ours_2`` at the start filter.** The two differ only on what follows the
#   match on its line, and a start here is line-anchored: its line ends at the title, so both read the same empty
#   window and the before-test they share decides. ``_ref_ours_2`` is used because every other ``ours-2`` call
#   site uses it, not because an input tells them apart -- and none can.
# * **the line tail's two adjacent stars.** It moves no answer, only the time: that is what the timing test
#   below is for, and a battery that compares answers cannot see it by construction.
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


def test_a_body_ten_characters_under_the_minimum_is_still_rejected():
    """The minimum is a measurement of the **body**, and the heading does not count towards it. Ten characters
    under is as rejected as three hundred: the number is what the measurement names (TIGO and EC's index
    blocks), and a rule that let a near miss through would have no number at all.

    The last-resort pass measures its distance from the heading instead, because that is how ``ours-1``
    measures it and the two have to answer the same text; that is pinned by the ``ours-1`` parity test below.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY[:3_985] + "\nRISK FACTORS\n"
    assert _cut2(text) == (None, "missing")
    assert _cut2(AIF + "BUSINESS OVERVIEW\n" + BODY[:4_100] + "\nRISK FACTORS\n") == (BODY[:4_100].strip(), "ok"), \
        "a hundred characters more and the same stop is the section's end"


# ── round 1 of the review of feefad16: eight shapes it measured, worst first ────────────────────────────────────
@pytest.mark.parametrize("shape", [
    "GENERAL DEVELOPMENT OF THE BUSINESS\nThree Year History\n" + BODY + "\nRISK FACTORS\n",
    "GENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY[:2_000] + "\nMaterial Properties\n" + BODY + "\nRISK FACTORS\n",
    "For more see\nGENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n",
    "GENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n",
])
def test_the_last_resort_answers_exactly_what_ours_1_answers(shape):
    """**Round 1's first finding.** The last-resort start is there so that no section ``ours-1`` finds is lost,
    and the first version of it lost three shapes: a subheading printed directly under the heading (NI 51-102F2
    item 4.1 is the three-year history, which is a stop here), a stop closer than the minimum, and a page whose
    previous line ends on a reference word -- ``ours-1`` applies no reference test to its start at all.

    So the last resort is asked exactly what ``ours-1`` asks: ``ours-1``'s stop, ``ours-1``'s distance,
    ``ours-1``'s page-number test, and nothing else. The assertion is equality with ``ours-1``, not a label.
    """
    text = AIF + shape
    old = sec_sections.cut_aif_business(text, version=OURS_1)
    section, quality = _cut2(text)
    assert old is not None, "the input has to be one ours-1 answers, or it tests nothing"
    assert quality == "ok" and section == old


@pytest.mark.parametrize("number", ["6.", "ITEM 4:", "ITEM 4 -", "4 \u2013", "4.1.1", "3.1)", "ITEM 4"])
def test_which_numbering_styles_may_stand_in_front_of_a_title(number):
    """**Round 1's second finding.** The first version read "6. DESCRIPTION" and not "ITEM 4: DESCRIPTION",
    which is EXK's own heading as the measurement quotes it -- and a filing whose stops are all numbered that
    way came back with every risk factor inside the business section, labelled ``ok``.
    """
    start = AIF + number + " DESCRIPTION OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n"
    assert _cut2(start) == (BODY.strip(), "ok")
    stop = AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY + "\n" + number + " RISK FACTORS\n" + "Risky. " * 400
    assert _cut2(stop) == (BODY.strip(), "ok")


def test_a_roman_numeral_in_front_of_a_title_is_not_read():
    """And is written down rather than quietly fixed: "IV. DESCRIPTION OF THE BUSINESS" is a plausible shape
    that the 12 Nov measurement did not count, and a rule for a shape nobody measured is a guess. It belongs to
    #763's hand-checked set, with the four title variants the review listed (``OUR BUSINESS``, ``GENERAL
    DESCRIPTION OF THE BUSINESS``, ``DESCRIPTION OF THE BUSINESS OF THE COMPANY``, and a "(continued)" tail).
    """
    assert _cut2(AIF + "IV. DESCRIPTION OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n") == (None, "missing")


@pytest.mark.parametrize("sentence,still_cut", [
    (" The Company is exposed to a number of risk factors, including these. ", True),
    (" The Company is not party to any material legal proceedings. ", True),
    (" Our directors and officers hold shares. ", True),
])
def test_a_stop_title_in_lower_case_prose_is_not_a_heading(sentence, still_cut):
    """**Round 1's third finding.** The inline search reads the stop titles wherever they appear and is
    case-insensitive, because filers print headings in capitals and in title case alike. The cost it measured:
    "...including risk factors" ended a section after 6,038 characters, labelled ``ok``; another such sentence
    nearer the heading made the section ``missing`` outright.

    A heading is not written inside a sentence in lower case, so that is the test. The position cannot tell the
    two apart -- the same lesson as the item rule's citations.
    """
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY[:6_000] + sentence + BODY
            + " 9 ITEM 4: RISK FACTORS The Company's business is risky.\n")
    section, quality = _cut2(text)
    assert quality == "ok"
    assert sentence.strip() in section and len(section) > 30_000


def test_a_title_cannot_reach_over_the_line_ending_and_swallow_what_follows():
    """**Round 1's fourth finding.** The gaps inside a title were ``\\s+``, which matches a newline, so
    ``BUSINESS OF THE COMPANY`` could take up to three words from the next line into itself: the page number
    under the heading became a title word and the contents test never saw it, and in another shape the stop
    heading on the next line was eaten and the risk factors came back as the business section.
    """
    paged = AIF + "BUSINESS OF THE COMPANY\n12\n" + BODY + "\nRISK FACTORS\n"
    assert _cut2(paged) == (None, "missing"), "the page number is a page number, not the title's fourth word"
    swallowed = AIF + "BUSINESS OF THE COMPANY\nRISK FACTORS\n" + BODY + "\nMARKET FOR SECURITIES\n"
    section, quality = _cut2(swallowed)
    assert quality == "ok" and section.startswith("RISK FACTORS"), \
        "the stop is too close to be the end, so it stays in the body -- but it is not part of the title"


def test_a_long_line_of_blanks_does_not_make_the_heading_patterns_crawl():
    """**Round 1's fifth finding.** The line's tail was ``[^\\S\\n]*[:.-]?[^\\S\\n]*$``: two adjacent stars over
    the same class, which is quadratic on a line that fails to match. Measured then: 0.05 s at 2,000 blanks,
    0.63 s at 8,000, 9.2 s at 32,000. ``strip_html`` collapses blank runs, so no stored document reaches this
    -- but text that did not pass through it would, and one star costs nothing.

    The bound is wide on purpose: this is a test of the shape of the pattern, not of the runner's speed.
    """
    text = AIF + "BUSINESS OVERVIEW" + " " * 32_000 + "x\n" + BODY + "\nRISK FACTORS\n"
    started = time.monotonic()
    section, quality = _cut2(text)
    assert time.monotonic() - started < 2.0
    assert (section, quality) == (None, "missing"), "and the line is still not a heading"


def test_a_closed_section_beats_an_unclosed_one_further_down_the_document():
    """**Round 1's seventh finding.** An Annual Information Form often ends in a schedule, and a schedule can
    carry a heading of its own with nothing behind it. Returning the first pair that survived meant 5,999
    characters of appendix in place of 26,999 characters of body, labelled ``overrun``.

    So every start is tried before an unclosed pair is returned, and only when none of them closes does the
    label become ``overrun``.
    """
    text = (AIF + "GENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n" + "Risky. " * 2_400
            + "\nSCHEDULE A\nBUSINESS OPERATIONS\n" + "Appendix. " * 700)
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()
    assert section == sec_sections.cut_aif_business(text, version=OURS_1)


def test_a_title_that_is_not_a_stop_leaves_the_section_running():
    """What the stop list's docstring claims, as an assertion. A heading our titles do not know does not end
    the section, and the answer says ``overrun`` rather than a shorter cut that looks clean.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + "\nMINERAL PROJECTS\n" + BODY
    section, quality = _cut2(text)
    assert quality == "overrun" and "MINERAL PROJECTS" in section


@pytest.mark.parametrize("stop", [
    "LEGAL PROCEEDINGS AND REGULATORY ACTIONS",
    "DIRECTORS AND EXECUTIVE OFFICERS OF THE COMPANY",
    "DIVIDENDS",
])
def test_a_real_item_title_with_its_tail_ends_the_section_where_it_stands(stop):
    """NI 51-102's own item titles, which the line anchor was losing: item 12 is "Legal Proceedings and
    Regulatory Actions" and item 10 "Directors and Executive Officers of the Company". Losing the tail does not
    lose the section -- the inline fallback finds the title again -- but it ends the body at the **next** plain
    stop instead, which is a whole section too much. The later stop here is what makes that visible.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + "\n" + stop + "\n" + BODY + "\nMARKET FOR SECURITIES\n"
    assert _cut2(text) == (BODY.strip(), "ok")


def test_a_document_whose_stops_begin_their_lines_is_not_read_for_inline_ones():
    """Why the fallback is a fallback and not a second chance. A filing that prints its headings on lines of
    their own has told us what a heading looks like in it; a title inside one of its sentences is prose. So the
    inline search runs only when the document holds no line-start stop at all -- here one stands too close to
    be the end, and the section runs on rather than stopping at a sentence further down.
    """
    text = (AIF + "BUSINESS OVERVIEW\n" + BODY[:1_000] + "\nMATERIAL PROPERTIES\n" + BODY
            + " Risk Factors are set out below. " + BODY)
    section, quality = _cut2(text)
    assert quality == "overrun" and "Risk Factors are set out below." in section


def test_the_inline_search_keeps_its_distance_like_the_line_one():
    """Both searches obey the same two rules, and this is the second of them on the fallback's side: an inline
    title closer than the minimum is the block's own subheading, so the search goes on to the next one.
    """
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY[:1_000] + " Risk Factors follow. " + BODY
            + " Material Properties follow. " + BODY)
    section, quality = _cut2(text)
    assert quality == "ok"
    assert "Risk Factors follow." in section and "Material Properties follow." not in section


def test_a_sentence_cased_mention_is_not_a_heading_either():
    """The heading test is "no lower-case word in it", not "some upper-case word in it". A filer who writes
    "We discuss Risk factors in the next section" has capitalised a sentence, not printed a heading, and the
    looser test would end the section there.
    """
    text = AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY[:6_000] + " We discuss Risk factors in the next section. " + BODY
    section, quality = _cut2(text)
    assert quality == "overrun" and "We discuss Risk factors" in section
    assert sec_sections._printed_as_a_heading("RISK FACTORS") is True
    assert sec_sections._printed_as_a_heading("Risk Factors") is True
    assert sec_sections._printed_as_a_heading("Risk factors") is False


def test_the_last_resort_looks_past_a_stop_that_is_too_close_where_ours_1_gives_up():
    """``ours-1`` takes the **first** ``RISK FACTORS`` line that is not a cross-reference and then rejects the
    whole start if that line is closer than 4,000 characters -- it does not go on to the next one. So a filing
    whose business block opens under a risk-factors cross heading comes back empty from ``ours-1``.

    The last resort keeps ``ours-1``'s title, its page-number test and its distance, but the distance is a test
    of each candidate stop and not of the first one alone. That is strictly more than ``ours-1`` answers, which
    is the direction this version is allowed to move: it never loses a section, and here it gains one.
    """
    text = AIF + "GENERAL DEVELOPMENT OF THE BUSINESS\nRISK FACTORS\n" + BODY + "\nRISK FACTORS\n"
    assert sec_sections.cut_aif_business(text, version=OURS_1) is None, \
        "ours-1 stops at the near heading and gives up"
    section, quality = _cut2(text)
    assert quality == "ok" and section.startswith("RISK FACTORS\nWe make widgets")
