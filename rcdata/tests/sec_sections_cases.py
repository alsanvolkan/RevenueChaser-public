"""The inputs of the ``ours-1`` golden check (#752).

This file is the one definition of them: the test reads it, and so did the capture that produced the golden. That
capture is not a file in the tree (research scripts are not part of this repository, AGENTS.md): it exec'd
``origin/main``'s own ``module_value_chain/splitter.py`` against these inputs. The golden's ``_how_to_recapture``
says how to do it again, and it is only ever done to ADD a case.

Each input is built from the two blocks below rather than written out, so the file stays small; the golden file
pins each input's SHA-256, so an accidental edit here is caught instead of silently changing what is compared.

The shapes are the ones the measurements of 9-12 Nov 2026 name: a table of contents in front of the body
(#315), a filer that splits the word *Item* itself ("ITE M 1A", VFF), an exhibit list opening with
"Item 1.01" (PCVX), a 20-F whose Item 4.B runs into a regulation chapter (PDD, BABA), a quoted cross-reference
before the real heading (#116 C17), an Annual Information Form in a 40-F (CNQ) and one titled the other way
(the 28 filings our one heading misses), a body past the 200,000 cap, and a body too short to be one.

Six of the cases are here because the first sixteen left a branch unread, which two review rounds on PR 758 found
by mutating the rule and watching the golden still pass: the three dashed cases together are the only readers of
the whole ``DASH`` class (see the comment on them -- one case reads one dash),
``suspect_tail_cross_reference`` and ``suspect_inner_item_header`` are the only cases where ``flag`` answers
anything but the quality it was handed, and ``aif_over_the_cap`` is the only one that reaches the AIF path's own
cap.

Which path a case takes is worth knowing before reading its expectation. ``_line_item`` -- headings on their own
lines, a stop found by ``_LINE_END`` -- decides ``toc_then_body``, ``body_only``, ``dash_headings``,
``item_1_01_in_exhibit_list``, ``quoted_cross_reference_first`` and ``suspect_inner_item_header``. The legacy
START/STOP patterns decide the rest, either because the headings are inline or because the line rule declined.
"""
from __future__ import annotations

#: A paragraph repeated to make a body long enough to pass ``MIN_ITEM_CHARS`` and ``_BODY_MIN``.
BODY = "We make widgets and sell them to customers worldwide. " * 500
#: The contents page a 10-K opens with: Item 1 and Item 1A back to back, each with a page number.
TOC = "TABLE OF CONTENTS\nItem 1. Business 3\nItem 1A. Risk Factors 12\n"
_TAIL = "\nCompetition\nWe compete.\n" + "More body. " * 300 + "\nItem 1A. Risk Factors\nRisky.\n"

#: ``(name, text, form)``. The names are what the golden file keys on.
CASES: tuple[tuple[str, str, str], ...] = (
    ("empty", "", "10-K"),
    ("blank", "   ", "10-K"),
    ("no_item_headers", "no items here at all", "10-K"),
    ("toc_then_body", TOC + "Item 1. Business\n" + BODY + _TAIL, "10-K"),
    ("body_only", "Item 1. Business\n" + BODY + _TAIL, "10-K"),
    ("toc_only", TOC, "10-K"),
    ("no_stop_header", "Item 1. Business\n" + BODY, "10-K"),
    # The stop comes after the body on purpose: a stop the filer split is a stop our pattern does not see, so
    # the slice runs on into Item 1A. With the split line in front of the start instead, the case would read
    # ``overrun`` for the ordinary reason -- no stop at all -- and prove nothing about the split word.
    ("split_word_item_1a", "Item 1. Business\n" + BODY + "\nITE M 1A. Risk Factors\nRisky.\n", "10-K"),
    ("item_1_01_in_exhibit_list",
     "Item 1.01 Entry into a Material Definitive Agreement\nItem 1. Business\n" + BODY + "\nItem 2. Properties\n",
     "10-K"),
    ("foreign_20f", "Item 4. Information on the Company\n" + BODY + "\nItem 5. Operating Review\n", "20-F"),
    ("foreign_20f_regulation_chapter",
     "Item 4. Information on the Company\nB. Business Overview\n" + BODY + "\nRegulations\nrules\n" + BODY
     + "\nItem 5. x\n", "20-F"),
    ("quoted_cross_reference_first",
     "see “Item 1. Business” of this report\nItem 1. Business\n" + BODY + "\nItem 1A. Risk Factors\n",
     "10-K"),
    ("aif_general_development",
     "annual information form\nGENERAL DEVELOPMENT OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n", "40-F"),
    ("aif_description_of_business",
     "annual information form\nDESCRIPTION OF THE BUSINESS\n" + BODY + "\nRISK FACTORS\n", "40-F"),
    ("over_the_cap", "Item 1. Business\n" + "w " * 120_000 + "\nItem 1A. Risk Factors\n", "10-K"),
    ("short_body", "Item 1. Business\nTiny.\nItem 1A. Risk Factors\n", "10-K"),
    # Three dashed headings, because one is not enough to read the ``DASH`` class. ``dash_headings`` has its
    # headings on their own lines, so the line-start rule decides it and only the start's dash is read -- that
    # rule's end pattern carries no dash class at all. The two inline cases have no line starts, so the legacy
    # START/STOP patterns decide, and those are where ``DASH`` is actually used on both ends. Measured against
    # ``origin/main``'s rule with the class mutated: dropping U+2212 breaks ``minus_dash_inline`` alone, dropping
    # U+2010-U+2015 breaks ``dash_headings`` and ``minus_dash_inline``, and dropping the plain hyphen breaks
    # ``hyphen_dash_inline`` alone. Together they fail on any member removed; ``dash_headings`` by itself does not.
    ("dash_headings", "Item 1 \u2014 Business\n" + BODY + "\nItem 1A \u2013 Risk Factors\nRisky.\n", "10-K"),
    ("minus_dash_inline",
     "part one Item 1 \u2212 Business " + BODY + " Item 1A \u2013 Risk Factors risky. ", "10-K"),
    ("hyphen_dash_inline", "part one Item 1 - Business " + BODY + " Item 1A - Risk Factors risky. ", "10-K"),
    # The two cases that make ``flag`` answer anything but the quality it was given. Without them, replacing the
    # whole suspect block with ``return quality`` passes the golden.
    ("suspect_tail_cross_reference", "Item 1. Business\n" + BODY + " See\nItem 1A. Risk Factors\n", "10-K"),
    ("suspect_inner_item_header",
     "Item 1. Business\nItem 3. Legal Proceedings\nWe litigate.\n" + BODY + _TAIL, "10-K"),
    # The AIF path has a cap of its own, on a different line from the item cap, and nothing else reaches it.
    ("aif_over_the_cap",
     "annual information form\nGENERAL DEVELOPMENT OF THE BUSINESS\n" + "w " * 120_000 + "\nRISK FACTORS\n", "40-F"),
)

#: One document for ``strip_html``: a script, a style, entities, a non-breaking space, and the tags it turns into
#: newlines.
STRIP_HTML_INPUT = ("<html><body><script>drop me</script><style>x{}</style><p>Hello&nbsp;&amp; welcome</p>"
                    "<div>Item 1. Business</div><tr>row</tr><br/>tail</body></html>")
