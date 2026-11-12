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


def test_btg_a_three_hundred_character_body_is_not_the_business_section():
    """**The 7, and edgartools' short tail.** BTG's exhibit is 568,780 characters; edgartools cuts 338 of them,
    because the next heading it detected was the following line. A 338-character answer is worse than none: it
    reads as a section that was found, and it is a contents line, a cross-reference, or a heading whose section
    is printed somewhere else.

    So a pair whose body is under ``_AIF_MIN`` is rejected and the **next start tried**, which is where the real
    body is -- and when the only start left is ``ours-1``'s, the last-resort pass answers it as ``ours-1`` does.

    Round 1 replaced this rejection with a distance rule (a stop closer than the minimum being read as a
    subheading, so the search went on). Round 2 measured what that cost: a contents block with no page numbers
    came back as the front matter, and a genuinely short section came back with the whole risk-factor section
    inside it, both labelled ``ok``. The rejection is back, and the contents test below is what the distance
    rule had been standing in for.
    """
    stub = "We are a responsible international gold producer headquartered in Vancouver, Canada. " * 4
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + stub
            + "\nMATERIAL PROPERTIES\n" + "Four operating mines. " * 50
            + "\nBUSINESS OVERVIEW\n" + BODY + "\nRISK FACTORS\n")
    assert len(stub) < sec_sections._AIF_MIN, "the first pair has to be the short one"
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()
    assert stub.strip() not in section and "MATERIAL PROPERTIES" not in section


def test_a_short_business_section_does_not_swallow_the_risk_factors():
    """**Round 2's second finding.** The section here really is 3,900 characters and the risk factors behind it
    are 34,000. Reading past the near stop returned all of it, labelled ``ok`` -- the one answer that must never
    be given, because a stored "business section" with every risk factor in it is what #755 spent nine rounds
    on for the item rule.

    ``missing`` is the honest answer: the rule's minimum says it cannot tell a 3,900-character section from an
    index block, and #763's hand-checked set is where that number gets judged. Returning the risks is not a
    second-best; it is the wrong section.
    """
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY[:3_900] + "\nRISK FACTORS\n"
            + "Risky prose here. " * 2_000 + "\nDIVIDENDS\nNone.\n")
    section, quality = _cut2(text)
    assert (section, quality) == (None, "missing")


def test_a_contents_block_with_no_page_numbers_is_still_a_contents_block():
    """**Round 2's first finding, and the worst of the round.** A contents block whose entries carry no page
    number passes :data:`_PAGE_NO`, and for one round nothing else rejected it: the cut came back as the rest of
    the contents plus the front matter, 6,490 characters labelled ``ok``, where the body is 26,999.

    What a contents block is, without page numbers, is headings on consecutive lines. So the test is the
    neighbouring non-blank line, above or below: another AIF heading there means this is a list of sections and
    not one of them.
    """
    contents = ("TABLE OF CONTENTS\nCORPORATE STRUCTURE\nGENERAL DEVELOPMENT OF THE BUSINESS\n"
                "DESCRIPTION OF THE BUSINESS\nRISK FACTORS\nDIVIDENDS\nMARKET FOR SECURITIES\n")
    text = (AIF + contents + "Forward-looking statements follow. " * 180
            + "\nCORPORATE STRUCTURE\n" + "Incorporated in Alberta. " * 20
            + "\nGENERAL DEVELOPMENT OF THE BUSINESS\n" + "History. " * 600
            + "\nDESCRIPTION OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n" + "Risky. " * 2_000)
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()


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


def test_a_cross_reference_to_the_risk_factors_inside_a_line_does_not_end_the_section_early():
    """The same test on the other end: a stop that is a mention **inside a line** is skipped and the next one
    taken, so the body is not cut off at the first filing that happens to say "see RISK FACTORS" mid-sentence.
    """
    text = (AIF + "BUSINESS OF THE COMPANY\n" + BODY + " For the risks of our business see RISK FACTORS below. "
            + BODY + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(text)
    assert quality == "ok" and "see RISK FACTORS below" in section
    assert section.endswith(BODY.strip()[-40:]), "the section runs on to the stop that is really one"


def test_a_cross_reference_whose_title_begins_the_next_line_does_end_the_section():
    """The second of this version's two known costs, and the one it shares with ``ours-2``'s item rule.

    A page that ends on "see" and a next line that opens with the title is the same text as a cross-reference
    broken across two lines. :func:`_inline_ref` keeps a title that begins its own line even so, because that
    is what the item rule measured over nine rounds and what ``ours-1`` does in its pattern rule; the price is
    this shape, where the section ends at a cross-reference 27,034 characters in rather than running on to
    ``MATERIAL PROPERTIES``.

    Position cannot tell a mention from a heading here, because the words that would settle it are on the
    previous line. The count over real filings is #763's, and no further round of built strings is planned for
    it -- the item rule reached the same place and stopped there.
    """
    text = (AIF + "BUSINESS OF THE COMPANY\n" + BODY + "\nFor the risks of our business see\nRISK FACTORS\n"
            + BODY + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(text)
    assert quality == "ok" and section.endswith("For the risks of our business see")


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
    """The ten stop titles, in the eighteen spellings this table reads them in. They are the other parts of an
    Annual Information Form under NI 51-102 -- most of them printed after the business block, two of them (the
    three-year history, and the general development of the business where a filing prints both headings) before
    it or inside it, which is why the start is chosen first and a stop is only ever looked for after it. A title
    that is not one of them leaves the section running, which is the ``overrun`` label and not a shorter cut.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + "\n" + title + "\n" + "After. " * 50
    section, quality = _cut2(text)
    assert quality == "ok" and section == BODY.strip()
    # and through the line-start pattern itself: with the inline search behind it, fifteen of these eighteen
    # rows pass when _AIF_STOP_2 is made never to match (round 1's review, measured again in round 4; the three
    # that fail are ITEM 8. RISK FACTORS, DIVIDENDS and DIVIDENDS AND DISTRIBUTIONS), so the path is asserted.
    assert sec_sections._AIF_STOP_2.search("\n" + title + "\n") is not None


# ── what the mutant battery found the tests above could not see ────────────────────────────────────────────────
# 95 mutants of the 40-F rule, each judged on whether the ANSWERS move -- the section, its label and ours-1's own
# text over 157 inputs. Every start title and every stop title is dropped in turn, generated from the source and
# not anchored by hand, because six rounds of rewriting moved all of their spellings and an anchor that no longer
# matches is a mutant that never ran. Round 3 re-pointed twelve such anchors and added fifteen mutants, round 4
# three and five, round 5 ten and seven, round 6 ten and twelve.
#
# The battery audits these tests as much as the code, and has found what they did not say in every round it has
# run.
#   * Round 3: nine inputs missing, and one redundancy in the code -- the earliest-unclosed-start comparison,
#     which round 2 needed when one loop walked two title sets and which round 3's split into two passes made
#     dead, since starts arrive in document order.
#   * Round 4: the two stop-title tails were in the "redundant alternative" class on a measurement that held
#     only for the rows the battery carried -- every one of them in capitals and unnumbered. Since round 3's
#     first finding the inline search does not read title case at all, so with both tails dropped
#     ``Legal Proceedings and Regulatory Actions`` goes from 26,999 characters to 54,041 and
#     ``Directors and Executive Officers of the Company`` to 54,048, while a numbered
#     ``12. LEGAL PROCEEDINGS AND REGULATORY ACTIONS`` goes to 27,004. Three rows and three inputs were added.
#   * Round 5: the leading ``\d`` of the pattern that read the text before a heading was in the "provable
#     equivalence" class and the review measured that it was not -- a live mutant with no test, 54,081
#     characters against 27,046 through the cut.
#   * Round 6: five more missing inputs, each for a distinction the code makes and no document exercised -- a
#     year in front of a page number, a gap made of a closing quote and a space together, a tab where a line
#     ending would be, a figure glued to the word "item", and a comma after a page number.
#
# Nine survive, in four classes, and each is explained rather than pinned:
#
# **A redundant alternative, subsumed by another pattern.** The third start title: ``DESCRIPTION OF BUSINESS``
# is matched by the first, whose "the" and whose possessive word are both optional. It stays because the titles
# are adapted one-to-one from the list named in the provenance block, and a list that silently drops an entry is
# a list nobody can check against its source.
#
# **A provable equivalence**, where no input can tell the two apart:
#   * ``_is_ref`` in place of ``_ref_ours_2`` at the start filter -- the two differ only on what follows the
#     match on its line, and a start here is line-anchored, so both read the same empty window; the one shape
#     that reaches the filter at all, ``see\nDESCRIPTION OF THE BUSINESS``, is skipped by both;
#   * ``match`` in place of ``fullmatch`` in the contents test -- :data:`_AIF_HEADING_LINE` ends in
#     :data:`_AIF_TAIL`, which ends in ``$``, so a line with anything behind the title fails either way;
#   * dropping ``any(c.isalpha())`` from the heading test -- every inline match is letters, so it can only
#     change the answer to a direct call, which the table above makes.
#
# **Killed by a test and not by an answer over a document:** :data:`_DASH_CHARS` replaced by :data:`_DASH`, the
# regex source leaking into the characters, which is round 5's first finding in its second spelling. Every
# document shape tried gives the same answer, because the characters the leak adds (``\``, ``u`` and four
# digits) only ever join the figure that is being stripped off the end of a line and the figure keeps a digit
# either way. ``test_the_two_spellings_of_the_dash_list_agree`` compares the two lists directly, which is the
# only thing that can see it -- and it is exactly the kind of mistake that has now happened twice.
#
# **Answer-invariant but not time-invariant**, which a battery comparing answers cannot see by construction, and
# which is what the timing tests are for:
#   * the line tail's two adjacent stars (round 1);
#   * the gap before an inline heading's prose, unbounded (round 3: 12.4 s on 32,000 blanks);
#   * the ends read from their first entry for every start rather than from a forward-only cursor (round 3:
#     2.3 s on 8,000 starts and 8,000 ends);
#   * the text before an inline heading, searched over the whole line instead of stripped off its end -- round 4
#     measured 21.9 s on 32,000 blanks and anchored it, round 5 measured 15.1 s on 32,000 digits and added a
#     lookbehind, and round 6 replaced the search with an :meth:`str.rstrip` walk because patching the pattern
#     had fixed one run shape at a time while costing answers (``-12-`` and ``completed.12``);
#   * the line start found with ``rfind("\n")`` while any line ending counted as one (round 6: 10.2 s on 1,500
#     headings in a document whose lines end in ``\r``, and a wrong label with it).
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
    """The inline search's own filter. The titles are read wherever they appear -- in every document, not only
    in one with no line-start stop in it (round 2's second finding) -- and a filing that says
    ``see "RISK FACTORS" below`` in its first paragraph would otherwise lose the whole body.
    With nothing left to stop it the section runs to the end of the document, which is ``overrun`` -- the label
    says so instead of a short cut pretending to be a clean one.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + " For more see “RISK FACTORS” below in this document. " + BODY
    section, quality = _cut2(text)
    assert quality == "overrun" and "RISK FACTORS" in section


def test_an_inline_stop_that_begins_its_line_is_a_heading_even_after_the_word_see():
    """Why the inline search uses :func:`_inline_ref` and not the stricter test: a filer whose page ends on the
    word "see" and whose next page opens with the heading has written a heading. The reference test applies to a
    match **inside** a line only, which is the same correction ``ours-2``'s item rule carries -- and the cost of
    that choice has its own test above
    (``test_a_cross_reference_whose_title_begins_the_next_line_does_end_the_section``).
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

    Both shapes answer ``missing`` now, for two different reasons -- the first is a contents entry by its page
    number, the second by the heading on the line below it.
    """
    paged = AIF + "BUSINESS OF THE COMPANY\n12\n" + BODY + "\nRISK FACTORS\n"
    assert _cut2(paged) == (None, "missing"), "the page number is a page number, not the title's fourth word"
    assert sec_sections._AIF_START_2.search("BUSINESS OF THE COMPANY\n12\n") is not None, \
        "and the heading itself still matches -- it is the filter that rejects it, not the pattern"
    assert sec_sections._AIF_START_2.search("BUSINESS OF THE COMPANY\n12\n").group(0).strip() \
        == "BUSINESS OF THE COMPANY"
    swallowed = AIF + "BUSINESS OF THE COMPANY\nRISK FACTORS\n" + BODY + "\nMARKET FOR SECURITIES\n"
    assert _cut2(swallowed) == (None, "missing"), \
        "two headings on consecutive lines are a contents block, whatever stands under the second"


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
    # added in round 4: the spellings where the tail is what decides, because the inline search cannot stand in
    # for the line pattern here -- title case is not read, and a number in front leaves it in the body.
    "Legal Proceedings and Regulatory Actions",
    "Directors and Executive Officers of the Company",
    "12. LEGAL PROCEEDINGS AND REGULATORY ACTIONS",
])
def test_a_real_item_title_with_its_tail_ends_the_section_where_it_stands(stop):
    """NI 51-102's own item titles, which the line anchor was losing: item 12 is "Legal Proceedings and
    Regulatory Actions" and item 10 "Directors and Executive Officers of the Company". Losing the tail does not
    lose the section -- the inline search finds the title again -- but it ends the body at the **next** plain
    stop instead, which is a whole section too much. The later stop here is what makes that visible.
    """
    text = AIF + "BUSINESS OVERVIEW\n" + BODY + "\n" + stop + "\n" + BODY + "\nMARKET FOR SECURITIES\n"
    assert _cut2(text) == (BODY.strip(), "ok")


def test_the_earlier_of_the_two_kinds_of_end_is_the_end():
    """**Round 2's third finding.** For one round the line-start search decided whenever it found anything, and
    the inline search ran only when it found nothing at all. EXK's own shape then returned 61,300 characters
    with every risk factor inside: its run-in ``9 ITEM 4: RISK FACTORS`` was ignored because a numbered
    ``ITEM 5: DIVIDENDS`` further down begins a line.

    Both kinds are collected and the earlier one ends the section. What keeps an inline title honest is the
    test it has to pass, not the order it is consulted in.
    """
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY
            + "\nNo significant acquisitions. 9 ITEM 4: RISK FACTORS The Company is risky.\n"
            + "Risky. " * 2_000 + "\nITEM 5: DIVIDENDS\nNone.\nITEM 6: MARKET FOR SECURITIES\nTSX.\n")
    section, quality = _cut2(text)
    assert quality == "ok" and section.endswith("9 ITEM 4:")
    assert "Risky." not in section


@pytest.mark.parametrize("title,is_one", [
    ("RISK FACTORS", True),
    ("MARKET FOR SECURITIES", True),
    ("ITEM 4: RISK FACTORS", True),
    ("Risk Factors", False),
    ("Market for Securities", False),
    ("Legal Proceedings and Regulatory Actions", False),
    ("Risk factors", False),
    ("risk factors", False),
    ("1234", False),
])
def test_which_titles_are_printed_the_way_a_heading_is_printed(title, is_one):
    """**Round 3's first finding, and the third test this module has had for the same question.** Capitals and
    nothing else.

    Round 1 asked for "no lower-case word at all" and round 2 relaxed it to "no lower-case word but a
    connective", so that ``Market for Securities`` and ``Legal Proceedings and Regulatory Actions`` would be
    read. Round 2's second finding then put the inline search in play for **every** document rather than only
    for one with no line-start stop in it, and round 3 measured what the relaxed test then accepted: ordinary
    AIF prose. "The Board has adopted a Code of Business Conduct", "See Description of the Business - Risk
    Factors - Commodity Prices", "The Company maintains Directors and Officers Liability Insurance" and "The
    Company has two Material Properties" each ended a section early, or lost it outright when the sentence fell
    inside the first 4,000 characters.

    **The cost is written down and not argued away:** a filer who prints a run-in heading in title case is not
    read, and the section runs on to its next line-start stop or to the end of the document -- a label of
    ``overrun`` rather than a quiet short cut. Which way the count goes is a question about real filings, so it
    belongs to #763's hand-checked set; the one run-in heading anybody has measured, EXK's
    ``9 ITEM 4: RISK FACTORS``, is in capitals.
    """
    assert sec_sections._printed_as_a_heading(title) is is_one


@pytest.mark.parametrize("after,ends_it", [
    (" The Company's business is risky.", True),
    (" \u2014 The Company's business is risky.", True),
    (" 12 The Company's business is risky.", True),
    ("\nThe Company's business is risky.", True),
    (" are described in note 14 of the financial statements.", False),
    (" of the Company are set out below.", False),
    ("\nand the Company's business is risky.", False),
])
def test_an_inline_title_has_to_run_into_its_section_s_first_sentence(after, ends_it):
    """The other half of the inline test, and the half the capitals cannot do: ``RISK FACTORS`` is printed in
    capitals whichever way it is used. A run-in heading is followed by prose that begins with a capital; a
    sentence that merely names the section goes on in lower case ("Legal Proceedings are described in note 14"
    ended a section after 7,999 characters, measured in round 2).

    **The prose may begin on the next line** (round 3's second finding): ``strip_html`` turns
    ``<b>ITEM 4: RISK FACTORS</b><br>The Company...`` into exactly that, and the same-line-only test returned
    63,100 characters with every risk factor inside it. The filer's punctuation and page number may stand in
    between, which is the third row's ``12``.
    """
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY + " 31 RISK FACTORS" + after + " " + "Risky. " * 2_000)
    section, quality = _cut2(text)
    assert (quality == "ok") is ends_it
    assert ("Risky." not in section) is ends_it


def test_a_title_case_run_in_heading_is_not_read_which_is_this_version_s_one_known_cost():
    """The cost of round 3's first finding, stated as a test so that it cannot be forgotten and so that a later
    round that changes it has to change this file too.

    The same shape as the first row above, printed ``Risk Factors`` instead of ``RISK FACTORS``: the inline
    search does not read it, the section runs to the end of the document, and the label says ``overrun``
    instead of claiming a clean cut. #763's hand-checked set is what decides whether that is the right trade.
    """
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY + " 31 Risk Factors The Company's business is risky. "
            + "Risky. " * 2_000)
    section, quality = _cut2(text)
    assert quality == "overrun" and "Risky." in section


def test_a_single_word_title_ends_a_section_only_on_a_line_of_its_own():
    """``DIVIDENDS`` is the one stop title of a single word, and inside a line nothing can tell it from a
    sentence: "Dividends received from subsidiaries fund the parent" passes both inline tests. So it is a line
    stop only. Measured in round 2: as an inline stop it ended a section after 7,999 characters where the body
    is 35,065.
    """
    inline = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY[:8_000]
              + " Dividends received from subsidiaries fund the parent. " + BODY
              + " 9 ITEM 4: RISK FACTORS The Company is risky.\n")
    section, quality = _cut2(inline)
    assert quality == "ok" and "Dividends received" in section and len(section) > 30_000
    on_its_own = AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY + "\nDIVIDENDS\nNone declared. " * 20
    assert _cut2(on_its_own) == (BODY.strip(), "ok")


def test_a_sentence_cased_mention_is_not_a_heading_either():
    """A filer who writes "We discuss Risk factors in the next section" has capitalised a sentence, not printed
    a heading. Under round 1's and round 2's tests this was the shape that argued for them; under round 3's it
    is refused for the plainer reason that the title is not in capitals.
    """
    text = AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY[:6_000] + " We discuss Risk factors in the next section. " + BODY
    section, quality = _cut2(text)
    assert quality == "overrun" and "We discuss Risk factors" in section
    assert sec_sections._printed_as_a_heading("RISK FACTORS") is True
    assert sec_sections._printed_as_a_heading("Risk factors") is False


def test_the_last_resort_gives_up_on_a_near_stop_exactly_where_ours_1_gives_up():
    """**Round 3's third finding, and the direct reversal of what round 1 put here.**

    ``ours-1`` takes the **first** ``RISK FACTORS`` line that is not a cross-reference and, if it stands closer
    than 4,000 characters, drops the whole start rather than looking for a later one. Round 1 read that as a
    section ``ours-1`` loses and had the last resort look past the near stop -- strictly more than ``ours-1``
    answers, which looked like the safe direction.

    Round 3 measured what it actually gains: a contents block. Dropping the start **is** ``ours-1``'s own
    contents test ("an index block the contents check missed", which is what ``_BODY_MIN`` is called in
    ``ours-1``), and looking past the near stop turned a table of contents into a 33,902-character section
    where ``ours-1`` answers 26,999 of real body from the heading further down. A rule that asks less than
    ``ours-1`` here returns front matter; a rule that asks more loses the three shapes round 1 found. So it
    asks exactly what ``ours-1`` asks, and this shape -- a business block opening under a risk-factors
    cross heading -- comes back empty from both.
    """
    text = AIF + "GENERAL DEVELOPMENT OF THE BUSINESS\nRISK FACTORS\n" + BODY + "\nRISK FACTORS\n"
    assert sec_sections.cut_aif_business(text, version=OURS_1) is None, \
        "ours-1 stops at the near heading and gives up"
    assert _cut2(text) == (None, "missing"), "and so does ours-2, which is the whole point of the pass"


def test_the_contents_block_the_last_resort_would_have_returned_had_it_asked_less():
    """The measurement behind the test above, kept as a test of its own because it is the shape that decided
    it: a contents block with no page numbers, the front matter behind it, and the real heading further down.
    ``ours-1`` answers the body; the version that looked past a near stop answered the contents.
    """
    text = (AIF + "TABLE OF CONTENTS\nCORPORATE STRUCTURE\nGENERAL DEVELOPMENT OF THE BUSINESS\nRISK FACTORS\n"
            + "DIVIDENDS\nMARKET FOR SECURITIES\n" + "Forward-looking statements follow. " * 180
            + "\nCORPORATE STRUCTURE\n" + "Incorporated in Alberta. " * 20
            + "\nGENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n")
    old = sec_sections.cut_aif_business(text, version=OURS_1)
    section, quality = _cut2(text)
    assert quality == "ok" and section == old
    assert section.startswith("We make widgets") and "TABLE OF CONTENTS" not in section


def test_the_last_resort_applies_no_body_minimum_because_ours_1_applies_none():
    """**Round 2's sixth finding.** ``ours-1`` tests only the distance from its heading and returns whatever the
    body is, so a 3,970-character body is a section to it. The minimum was still being applied to the
    last-resort pass, and a fuzz over 60,000 built documents found 858 where ``ours-2`` answered nothing and
    ``ours-1`` answered a section -- every one of them a body between 3,959 and 3,999 characters.

    The minimum belongs to this version's own titles, where it is what rejects a contents line. On ``ours-1``'s
    title it would be this version losing a section, which is the one thing the last resort exists to prevent.
    """
    text = AIF + "GENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY[:3_970] + "\nRISK FACTORS\n"
    old = sec_sections.cut_aif_business(text, version=OURS_1)
    assert old is not None and len(old) == 3_970
    assert _cut2(text) == (old, "ok")


def test_the_earliest_unclosed_start_is_the_one_that_is_returned():
    """**Round 2's eighth finding.** When nothing closes, an appendix's heading at the end of the document was
    still beating the real heading at the top of it, because the titles were tried before ``ours-1``'s and the
    first survivor won. 6,999 characters of appendix, where the body runs to 70,000.
    """
    text = (AIF + "GENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY + " risk factors are discussed. "
            + "Risky. " * 2_000 + "\nSCHEDULE A\nBUSINESS OPERATIONS\n" + "Appendix. " * 700)
    section, quality = _cut2(text)
    assert quality == "overrun" and section.startswith("We make widgets")
    assert "Appendix." in section, "it runs to the end of the document, appendix and all"


def test_a_title_s_own_words_do_not_reach_over_a_line_ending_either():
    """The ``\\s+`` of round 1's fourth finding survived in one place, which round 2 found: the three-year
    history's spelling had to allow a hyphen as well as a blank, and ``[\\s-]`` let the title match
    ``"3\\nyear history"`` -- a figure at the end of one line and prose at the start of the next.
    """
    assert sec_sections._AIF_STOP_2.search("\n3\nyear history\n") is None
    assert sec_sections._AIF_STOP_2.search("\nTHREE-YEAR HISTORY\n") is not None
    assert sec_sections._AIF_STOP_2.search("\nTHREE YEAR HISTORY\n") is not None


def test_a_document_full_of_mentions_is_scanned_once_and_not_once_per_start():
    """**Round 2's seventh finding.** Each start used to re-scan the whole text for its end, so a megabyte with
    80,000 lower-case mentions in it cost 5.8 s where one scan costs 0.02 s -- the reference test runs an
    ``rfind`` per mention. The ends of a document are collected once and then read by position.

    The bound is wide on purpose: this is a test of the shape of the algorithm, not of the runner's speed.
    """
    text = AIF + "DESCRIPTION OF THE BUSINESS\n" + ("risk factors " * 8) * 10_000
    started = time.monotonic()
    section, quality = _cut2(text)
    assert time.monotonic() - started < 2.0
    assert quality == "overrun", "none of those mentions is a heading"


# ── round 3 of the review of caf3f175: five findings, and the narrowing the fix for the second one needed ──────
def test_a_run_in_heading_whose_prose_is_on_the_next_line_has_to_be_introduced_like_one():
    """The narrowing that fixing round 3's second finding needed, and the shape that forced it.

    Once the prose may begin on the next line, a title at the **end** of a prose line whose next line opens
    with a capital reads exactly like a run-in heading: "We now turn to our RISK FACTORS" ended a
    27,019-character body. The two cannot be told apart by the capital alone, so the title also has to be
    introduced the way a heading is -- by its line's start, or by a page or item number and nothing else, which
    is EXK's ``9 ITEM 4:``. A sentence is not a page number.
    """
    sentence = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\nWe now turn to our RISK FACTORS\n" + BODY
                + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(sentence)
    assert quality == "ok" and "We now turn to our RISK FACTORS" in section, "the sentence is not a heading"
    assert section.endswith(BODY.strip()[-40:]), "and the section runs on to the real stop"

    numbered = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY
                + "\nNo significant acquisitions were completed. 9 ITEM 4: RISK FACTORS\n"
                + "The Company's business is risky.\n" + "Risky. " * 2_000)
    section, quality = _cut2(numbered)
    assert quality == "ok" and "Risky." not in section, "and the page number plus item number is one"
    assert len(section) == 27_054


@pytest.mark.parametrize("head,is_one", [
    ("", True),
    ("   ", True),
    ("No significant acquisitions were completed. 9 ITEM 4: ", True),
    ("31 ", True),
    ("We now turn to our ", False),
    ("The Company has two ", False),
    # added in round 4: a sentence ending in a figure is not a page number
    ("At year end the Company had 2 ", False),
    ("Our mines, all 3 ", False),
    ("x 12 ", False),
    ("Production fell in 2025. 12 ", True),
    # added in round 5: the sentence-end test was a string of characters taken from a regex class, so it
    # accepted the letter "u" and the digits 0, 1, 2 and 5 and refused a real dash
    ("had mines in Peru 12 ", False),
    ("Production fell in 2020 12 ", False),
    ("as follows: 12 ", True),
    ("were completed \u2014 12 ", True),
    ("were completed \u2013 12 ", True),
    ("were completed - 12 ", True),
    ("were completed. - ", False),
    # and the item number introduces itself, whatever stands in front of it
    ("No significant acquisitions were completed.\u201d 9 ITEM 4: ", True),
    ("(the \u201cAcquisition\u201d) 9 ITEM 4: ", True),
    ("Total 1,234 9 ITEM 4: ", True),
    ("\u2022 silver 9 ITEM 4: ", True),
    ("were completed.\u201d 12 ", True),
])
def test_what_may_introduce_a_run_in_heading(head, is_one):
    """The table for the test above, read directly. Whitespace alone counts as a line start; prose does not,
    and prose ending in a word is what distinguishes the two shapes above.
    """
    text = "previous line\n" + head + "RISK FACTORS"
    assert sec_sections._introduced_as_a_heading(text, len(text) - len("RISK FACTORS")) is is_one


def test_a_contents_line_at_the_end_of_a_document_is_missing_and_not_an_empty_overrun():
    """**Round 3's fourth finding.** A start with no end keeps its body and is labelled ``overrun``, and the
    body minimum was being applied only to a closed pair. A contents line as the document's last line then
    came back as ``("", "overrun")`` -- a section of no characters, reported as one that was found and ran
    long. The minimum applies to an unclosed body too, and the answer is ``missing``.
    """
    # Round 4: the second of these three passes on the previous commit as well, because pass 1 always applied
    # the minimum. The defect was on ``ours-1``'s own title, where the pass had none -- that is the first line.
    assert _cut2(AIF + "Corporate structure and history follow.\nGENERAL DEVELOPMENT OF THE BUSINESS\n") \
        == (None, "missing"), 'this answered ("", "overrun") before the fix'
    assert _cut2(AIF + "Corporate structure and history follow.\nDESCRIPTION OF THE BUSINESS\n") == (None, "missing")
    assert _cut2(AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY)[1] == "overrun", "a real unclosed body still is one"


def test_a_title_followed_by_blanks_does_not_take_quadratic_time():
    """**Round 3's fifth finding, first half.** The test for the prose after an inline title was
    ``[^\\S\\n]*(?:...)?[^\\S\\n]*``: two stars over one class, which is the same quadratic shape round 1
    removed from the title tail and which nobody had looked for here. 32,000 blanks after a title cost 12.4 s.
    The pattern is bounded now and has one star.
    """
    text = AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY + " RISK FACTORS" + " " * 32_000
    started = time.monotonic()
    _cut2(text)
    assert time.monotonic() - started < 2.0


def test_the_ends_are_read_forward_only_and_not_from_the_start_for_every_start():
    """**Round 3's fifth finding, second half.** Round 2 collected the ends once, which fixed the re-scan, but
    every start then walked that list from its first entry; a document holding 8,000 starts and 8,000 ends cost
    2.3 s. Starts arrive in document order, so the cursor into the ends only ever moves forward.

    The walk could have been a bisect, and is not: ``bisect`` is an import, and this module's own test refuses
    any import but ``re``, ``dataclasses``, ``html`` and ``__future__``.
    """
    # Round 4: 8,000 pairs and a 3.0 s limit could not fail on the code this guards -- the pre-fix walk took
    # 1.19 s there. 16,000 pairs quadruples that and the limit is tighter than the pre-fix cost.
    text = AIF + ("BUSINESS OVERVIEW\nx\nRISK FACTORS\ny\n" * 16_000)
    started = time.monotonic()
    _cut2(text)
    assert time.monotonic() - started < 2.0


def test_the_two_passes_are_two_passes_and_the_first_one_to_close_answers():
    """The shape of the rule after round 3, asserted rather than described: this version's own titles first,
    then ``ours-1``'s, and a closed pair from either beats an unclosed one from both.
    """
    assert sec_sections._aif_pass_ours_1(AIF + "GENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY
                                        + "\nRISK FACTORS\n") == (BODY.strip(), None)
    assert sec_sections._aif_pass_ours_1(AIF + "BUSINESS OVERVIEW\n" + BODY + "\nRISK FACTORS\n") == (None, None), \
        "ours-1's pass reads ours-1's title and no other"
    both = AIF + "BUSINESS OVERVIEW\n" + BODY + "\nGENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n"
    section, quality = _cut2(both)
    assert quality == "ok" and section.startswith("We make widgets"), "this version's own title closes first"


# ── round 4 of the review of e810512c: the fix for round 3's second finding carried two defects of its own ─────
def test_the_text_before_a_run_in_heading_does_not_take_quadratic_time():
    """**Round 4's first finding, and the same defect round 3 had just removed from the other side of the
    title.** :data:`_H_BEFORE` opened with ``[^\\S\\n]*`` and was matched with ``search``, so a run of n blanks
    before the title was retried from each of n positions: 21.9 s on 32,000 blanks, 5.5 s on 16,000.

    It is reachable. ``strip_html`` collapses runs of ``[ \\t]`` but not of ``&emsp;``, which arrives as its own
    character -- 16,000 of them leave a 16,001-character run and cost 7.1 s. The pattern is anchored with
    ``\\Z`` now and the head is stripped before it is read, so the run is walked once.

    The timing test for :data:`_H_AFTER` above does not cover this: its blanks fall **after** the title, where
    this code is never asked anything.
    """
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY + "\nx" + " " * 32_000
            + "RISK FACTORS\nThe Company is risky.\n" + "Risky. " * 100)
    started = time.monotonic()
    section, quality = _cut2(text)
    assert time.monotonic() - started < 2.0
    assert quality == "overrun", "and the answer is the one it had before"


def test_a_sentence_that_ends_in_a_figure_does_not_introduce_a_heading():
    """**Round 4's third finding.** :data:`_H_BEFORE` was anchored at its end only, so any prose ending in a
    number passed as "a page number and nothing else" -- "the Company had 2 MATERIAL PROPERTIES" ended a
    section at 27,030 characters where the same sentence written "two" ran on to 54,073. Two spellings of one
    sentence, two answers.

    A page number stands at a line's start or after the sentence whose page it ends, so what comes before it
    has to be nothing or a sentence's punctuation. EXK's ``...were completed. 9 ITEM 4:`` passes on the full
    stop; a sentence that merely counts something does not.
    """
    answers = set()
    for figure in ("2", "two"):
        text = (AIF + "BUSINESS OVERVIEW\n" + BODY + f"\nAt year end the Company had {figure} MATERIAL PROPERTIES\n"
                + "Both are in Ontario. " + BODY + "\nRISK FACTORS\n")
        section, quality = _cut2(text)
        assert quality == "ok" and f"had {figure} MATERIAL PROPERTIES" in section
        answers.add(len(section) - len(figure))
    assert len(answers) == 1, "the figure's spelling may not change the answer"


def test_where_the_line_branch_s_reference_test_still_decides():
    """**Round 4's second finding**, which is a correction to this file and not to the rule.

    The two branches of :func:`_aif_ends` can reach the same offset, and where they do the inline one decides:
    :func:`_ref_ours_2` refuses a line stop after "see" and :func:`_inline_ref` keeps a title that opens its own
    line, which is ``ours-2``'s item rule's own measured correction. So the reference test on the line branch
    decides only where the inline search does not match, and this test pins both of those places rather than
    leaving the earlier docstring's claim that "a line-start stop has to pass the reference test" standing.

    - ``DIVIDENDS`` is the one stop the inline search does not read at all, so the line test decides it.
    - A stop whose next line opens in lower case fails :func:`_runs_into_prose`, so the line test decides that
      too.
    """
    dividends = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\nFor our distributions see\nDIVIDENDS\n"
                 + BODY + "\nMARKET FOR SECURITIES\n")
    section, quality = _cut2(dividends)
    assert quality == "ok" and "see\nDIVIDENDS" in section, "the reference is skipped and the real stop taken"

    lower = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\nFor the risks of our business see\nRISK FACTORS\n"
             + "and the related uncertainties are set out below. " + BODY + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(lower)
    assert quality == "ok" and "see\nRISK FACTORS" in section


# ── round 5 of the review of b27ecc76: two defects inside round 4's own fix, and one it had not reached ────────
def test_the_item_number_introduces_itself_whatever_stands_in_front_of_it():
    """**Round 5's second finding, and a regression round 4's fix introduced.**

    Round 4's defect was a **bare** figure ("had 2 MATERIAL PROPERTIES"), and its fix asked what stood in front
    of the number. It asked that of the ``ITEM n`` form too, where the word "item" is the introduction, so
    EXK's heading survived only because its sentence happens to end in a plain full stop. A closing quote or
    bracket after that stop, a table row, a running footer or a list item in front of the page number each
    returned the business section with every risk factor inside it, labelled ``ok`` -- all of them 54,0xx
    characters where the answer is 27,0xx.
    """
    for tail in ('were completed.', 'were completed."', 'were completed.”', 'were completed.)',
                 '(the “Acquisition”)', 'Total 1,234', 'Annual Information Form', '• silver'):
        text = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\n" + tail + " 9 ITEM 4: RISK FACTORS\n"
                + "The Company is risky. " + BODY + "\nMATERIAL PROPERTIES\n")
        section, quality = _cut2(text)
        assert quality == "ok", tail
        assert len(section) < 30_000, f"{tail!r} returned {len(section)}, the risk factors with it"
        assert "The Company is risky." not in section, tail


def test_the_sentence_end_test_is_a_compiled_class_and_not_a_string_of_characters():
    """**Round 5's first finding.** The test was ``before[-1] in ".:;!?" + _DASH``, and :data:`_DASH` is a
    **regex class fragment** -- ``\\-\\u2010-\\u2015\\u2212`` as source text. Concatenated into a plain string
    for ``in``, the punctuation it accepted was ``. : ; ! ? \\ - u 0 1 2 5``: the letter ``u``, four digits, and
    not one real en or em dash.

    So the same two answers round 4 set out to remove came back keyed on spelling: "had mines in Peru 2
    MATERIAL PROPERTIES" ended a section at 27,044 characters where "in Chile" ran on to 54,086, and
    "...were completed <em dash> 9 ITEM 4:" lost its heading where an ASCII hyphen kept it.
    """
    assert "—" not in ".:;!?" + sec_sections._DASH, "the string test accepted no em dash"
    assert "u" in ".:;!?" + sec_sections._DASH, "and did accept a letter"
    for mark in (".", ":", ";", "!", "?", "-", "–", "—", "−"):
        assert sec_sections._H_SENTENCE_END.search("completed" + mark) is not None, mark
    for ch in ("u", "0", "1", "2", "5", "d", "\\"):
        assert sec_sections._H_SENTENCE_END.search("complete" + ch) is None, ch

    answers = set()
    for place in ("Peru", "Chile"):
        text = (AIF + "BUSINESS OVERVIEW\n" + BODY + f"\nThe Company had mines in {place} 2 MATERIAL PROPERTIES\n"
                + "Both are in Ontario. " + BODY + "\nRISK FACTORS\n")
        section, _ = _cut2(text)
        answers.add(len(section) - len(place))
    assert len(answers) == 1, "the place name may not change the answer"


def test_a_run_of_digits_before_a_title_does_not_take_quadratic_time():
    """**Round 5's third finding**, which round 4's ``\\Z`` anchor had not reached: the search still restarted at
    every digit of a run and ran to the run's end before failing, 15.1 s on 32,000 digits and 7.0 s on 16,000
    ``1.`` pairs. A lookbehind in front of each figure leaves one starting position instead of n.

    Lower reachability than the blank run -- it needs thousands of digits with no space, on a line that ends in
    a capitalised stop title -- but it is the same shape, and the shape is what the loop keeps finding.
    """
    text = (AIF + "DESCRIPTION OF THE BUSINESS\n" + BODY + "\n" + "1" * 32_000
            + " x RISK FACTORS\nThe Company is risky.\n" + "Risky. " * 100)
    started = time.monotonic()
    _cut2(text)
    assert time.monotonic() - started < 2.0


def test_the_prose_may_begin_on_a_line_ended_by_any_line_ending():
    """**Round 5's seventh item.** :data:`_H_AFTER` crosses a line ending with ``\\s``, and the test for "the
    prose is on the next line" asked for ``"\\n"`` alone. A form feed or a carriage return therefore put the
    prose on another line while the introduction test was skipped, so "We now turn to our RISK FACTORS" ended a
    section at 27,019 characters behind a ``\\x0c`` where ``\\n`` leaves it running to 54,032. ``strip_html``
    removes neither character.
    """
    answers = set()
    for ending in ("\n", "\r", "\x0c", "\v", "\u2028"):   # written as an escape: a raw U+2028 here
        # reads as a space, and an editor that normalises it turns this into the same-line shape (round 6)
        text = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\nWe now turn to our RISK FACTORS" + ending
                + BODY + "\nMATERIAL PROPERTIES\n")
        section, quality = _cut2(text)
        assert quality == "ok" and "We now turn to our RISK FACTORS" in section, repr(ending)
        answers.add(len(section))
    assert len(answers) == 1, "the line ending's spelling may not change the answer"


def test_where_a_title_case_stop_shows_the_line_branch_s_reference_test_deciding():
    """**Round 5's fifth item**, a correction to this file: the docstring of :func:`_aif_ends` named two places
    where the line branch's reference test still decides and there are three. A **title-case** stop is the
    third, because the inline search has not read title case since round 3's first finding.
    """
    text = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\nFor the risks of our business see\nRisk Factors\n"
            + "The Company is risky. " + BODY + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(text)
    assert quality == "ok" and "see\nRisk Factors" in section, "the reference is skipped and the real stop taken"


# ── round 6 of the review of 80986b48: four defects, all inside round 5's own fix ───────────────────────────────
LINE_ENDINGS = ("\n", "\r", "\x0c", "\v", " ", "\u0085")


def test_a_heading_alone_on_its_line_is_read_whatever_ends_the_lines():
    """**Round 6's first finding, and the worst of the six rounds' crop.** Round 5 made :data:`_VERTICAL` the
    answer to "is the prose on the next line?" and left the answer to "where does this line begin?" as
    ``text.rfind("\\n", ...)``. The two then disagreed: in a document whose lines end in ``\\r`` or ``\\x0c``, a
    heading alone on its own line was measured against the **previous** line, refused, and the section ran to
    the end of the document -- 54,088 characters labelled ``overrun`` where ``\\n`` gives 27,032 of body.

    One answer for every line ending, which is what :func:`_line_start` is for.
    """
    answers = set()
    for ending in LINE_ENDINGS:
        text = (AIF + "BUSINESS OVERVIEW\n" + BODY + ending + "No acquisitions were completed." + ending
                + "RISK FACTORS" + ending + "The Company is risky. " + BODY + ending + "MATERIAL PROPERTIES"
                + ending)
        section, quality = _cut2(text)
        assert quality == "ok" and "The Company is risky." not in section, repr(ending)
        answers.add(len(section))
    assert len(answers) == 1, "the line ending's spelling may not change the answer"


def test_many_candidates_in_a_document_with_no_newline_do_not_take_quadratic_time():
    """The other half of round 6's first finding. Each candidate sliced the text back to the last ``\\n`` and
    searched it, so a document whose lines end in ``\\r`` was walked from its start for every heading in it:
    1,500 of them cost 10.2 s, and 4,000 cost 49.2 s, where the same document written with ``\\n`` costs 0.02 s.
    """
    text = (AIF + "BUSINESS OVERVIEW\n" + BODY
            + "\r" + ("We now turn to our RISK FACTORS\rThe Company is risky and it stays so.\r" * 2_000))
    started = time.monotonic()
    _cut2(text)
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize("gap", [" ", "\t", "  ", " ", " ", " ", " "])
def test_the_gap_before_a_page_number_may_be_any_whitespace(gap):
    """**Round 6's second finding.** Round 5 wrote ``rstrip(_H_CLOSERS)`` where ``rstrip()`` had stood, and
    :data:`_H_CLOSERS` carried a space and a tab and nothing else. Every other gap was then left in front of the
    sentence-end test and refused it: an em space, a thin space, an en space and a no-break space each turned
    27,019 characters into 54,054. ``strip_html`` collapses runs of ``[ \\t]`` and leaves those alone, which the
    comment on the pattern above says in so many words.

    Whitespace is stripped on its own now and the closers are a separate set.
    """
    text = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\nNo acquisitions were completed." + gap
            + "12 RISK FACTORS\nThe Company is risky. " + BODY + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(text)
    assert quality == "ok" and "The Company is risky." not in section
    assert len(section) < 30_000


@pytest.mark.parametrize("head,ends_it", [
    ("No significant acquisitions were completed. 9 ITEM 4: ", True),
    ("Total 1,234 9 ITEM 4: ", True),
    ("ITEM 4: ", True),
    ("For the risks of our business see Item 4: ", False),
    ("as discussed under Item 4 ", False),
    ("the line item 12 ", False),
    ("subitem 4 ", False),
])
def test_what_may_stand_in_front_of_an_item_number(head, ends_it):
    """**Round 6's third finding.** Round 5 made the item number introduce itself and asked nothing at all about
    what stood in front of it, which read a cross-reference as a heading: "see Item 4:" and "as discussed under
    Item 4" each ended a section where the sentence is a reference, and "the line item 12" and "subitem 4" did
    the same at a noun -- 27,0xx where the answer is 54,0xx. :func:`_inline_ref` cannot catch them, because the
    reference word is not directly in front of the title; the item number stands between them.

    So the word "item" needs a word boundary, and what stands before it may be nothing, a page number, or the
    end of a sentence. A page number counts in front of "item" and nowhere else, because that is EXK's own shape
    behind a table row and a running footer.
    """
    text = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\n" + head + "RISK FACTORS\nThe Company is risky. "
            + BODY + "\nMATERIAL PROPERTIES\n")
    section, quality = _cut2(text)
    assert quality == "ok"
    assert ("The Company is risky." not in section) is ends_it, f"{head!r} -> {len(section)}"


@pytest.mark.parametrize("head,ends_it", [
    ("No acquisitions were completed. -12- ", True),
    ("-12- ", True),
    ("No acquisitions were completed.12 ", True),
    ("No acquisitions were completed. 12 ", True),
    ("No acquisitions were completed.” 12 ", True),
    # the documented cost: a figure introduced by another figure, whichever it is
    ("No acquisitions were completed.1 12 ", False),
    ("Production fell in 2020 12 ", False),
    ("The Company had mines in Peru 2 ", False),
])
def test_which_printed_page_numbers_introduce_a_heading(head, ends_it):
    """**Round 6's fourth finding**, and the cost it chose deliberately.

    Round 5's lookbehind refused a page number that touched the character before it, so ``-12-`` -- an ordinary
    printed page-number form -- and ``completed.12`` were both lost: 54,0xx where the answer is 27,0xx. The
    figure is read off the end of the line with :meth:`str.rstrip` now, which has no restart to guard against,
    so the lookbehind is gone and both forms are back.

    The last three rows are **one answer chosen for four shapes that the text cannot separate**: a footnote
    mark, a year, a table row's last cell and a running footer's page count all leave a figure in front of the
    page number, and so does "had 2 MATERIAL PROPERTIES", which is the shape round 4 was fixing. A fifth rule
    read off built strings would only move which of them is wrong, so the count goes to #763's hand-checked set.
    """
    text = (AIF + "BUSINESS OVERVIEW\n" + BODY + "\n" + head + "MATERIAL PROPERTIES\nBoth are in Ontario. "
            + BODY + "\nRISK FACTORS\n")
    section, quality = _cut2(text)
    assert quality == "ok"
    assert ("Both are in Ontario." not in section) is ends_it, f"{head!r} -> {len(section)}"


def test_the_two_spellings_of_the_dash_list_agree():
    """:data:`_DASH` is the inside of a regex class and :data:`_DASH_CHARS` is the same characters as plain
    text, because round 5's first finding was the first used as if it were the second. Two spellings of one list
    drift unless something compares them, and this is that something.
    """
    import re as _re
    klass = _re.compile(f"[{sec_sections._DASH}]")
    for ch in sec_sections._DASH_CHARS:
        assert klass.fullmatch(ch), f"{ch!r} is in the character string and not in the class"
    for ch in "-‐‑‒–—―−":
        assert ch in sec_sections._DASH_CHARS, f"{ch!r} is in the class's range and not in the string"
    for ch in "u0125\\ x":
        assert ch not in sec_sections._DASH_CHARS, f"{ch!r} is the regex source leaking into the characters"


def test_the_two_spellings_of_the_line_ending_list_agree():
    """The same for :data:`_VERTICAL`, which is built from :data:`_VERTICAL_CHARS` so that the pattern and the
    character string cannot disagree -- :func:`_line_start` reads the string and
    :func:`_inline_prose_follows` reads the pattern, and round 6's first finding was those two disagreeing.
    """
    for ch in sec_sections._VERTICAL_CHARS:
        assert sec_sections._VERTICAL.fullmatch(ch), repr(ch)
    for ch in LINE_ENDINGS:
        assert ch in sec_sections._VERTICAL_CHARS, repr(ch)
    assert sec_sections._VERTICAL.search("a b\tc") is None
