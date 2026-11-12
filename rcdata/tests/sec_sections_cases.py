"""The inputs of the ``ours-1`` golden check (#752). One definition, shared by the test and the capture script.

Each input is built from the two blocks below rather than written out, so the file stays small; the golden file
pins each input's SHA-256, so an accidental edit here is caught instead of silently changing what is compared.

The shapes are the ones the measurements of 9-12 Nov 2026 name: a table of contents in front of the body
(#315), a filer that splits the word *Item* itself ("ITE M 1A", VFF), an exhibit list opening with
"Item 1.01" (PCVX), a 20-F whose Item 4.B runs into a regulation chapter (PDD, BABA), a quoted cross-reference
before the real heading (#116 C17), an Annual Information Form in a 40-F (CNQ) and one titled the other way
(the 28 filings our one heading misses), a body past the 200,000 cap, and a body too short to be one.
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
    ("split_word_item_1a", "ITE M 1A. Risk Factors\nItem 1. Business\n" + BODY, "10-K"),
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
)

#: One document for ``strip_html``: a script, a style, entities, a non-breaking space, and the tags it turns into
#: newlines.
STRIP_HTML_INPUT = ("<html><body><script>drop me</script><style>x{}</style><p>Hello&nbsp;&amp; welcome</p>"
                    "<div>Item 1. Business</div><tr>row</tr><br/>tail</body></html>")
