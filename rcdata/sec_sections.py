"""The one item-cutting rule, versioned: Item 1 / Item 4 of a stored annual report, and a 40-F's AIF business block.

This module is the single owner of the rule that cuts an SEC form's prose into sections. It was spread over
``module_value_chain/config.py`` (the patterns and the length limits) and ``module_value_chain/splitter.py`` (the
rule itself); both now re-export from here, so there is one copy to fix and one place that says which version cut
a section.

**Why it lives in ``rcdata`` and not in ``rcdata.build``.** ``rcdata/__init__.py`` is a docstring only and this
module imports nothing but ``re``, ``dataclasses`` and (lazily, in :func:`strip_html`) ``html``, so a board
importing it costs nothing. ``rcdata.build.__init__`` builds the dataset registry and pulls in duckdb and httpx,
which a board must not pay for just to slice a filing.

**Why the rule is versioned.** Two callers cut live text with it and store what they cut, neither with a version
column to say which rule did it: ``rcdata/build/fundamentals/filings.py`` writes ``filing_item``, and
``module_value_chain/edgar_text.py`` writes ``chain_filings``, which the Relatedness profile writer reads. So
improving the rule in place would change what Value Chain and Relatedness read, make those tables an unlabelled
mixture of two rules, and restale the Relatedness profiles -- which is a paid model run. Instead every caller that
stores a section names a version:

- ``ours-1`` -- the rule exactly as it was on 12 Nov 2026, caps a section at :data:`MAX_ITEM_CHARS` and labels a
  capped one ``capped``. The boards stay on it, and this module must keep it byte-identical in behaviour.
- ``ours-2`` -- the ``sec_forms`` layer's rule (#755): the word *Item* may be split like any other heading word,
  a sub-item number is not the item, **nothing is capped**, a cross-reference body is labelled ``pointer``, and a
  slice holding another item's own title line is ``overrun`` however it ended. #828 adds the heading spellings
  six real filings use (a pipe, a stop before the number, a dash, an ell for the one, a zero-width space, no
  title at all under "PART I") and the ``crossref`` label for an annual report that carries a Form 10-K/20-F
  item index instead of item sections. No board reads it, and moving one onto it is its own change.
- later versions arrive as new entries in :data:`RULES`; an older one is never edited again.

  **What enforces that, honestly.** :class:`Rule` versions the two entry points and nothing below them: the
  patterns, ``_LINE_START``, ``_is_ref``, ``_before_regulation``, the two limits, :func:`flag` and
  :func:`strip_html` are module-level and shared, so an ``ours-2`` author editing one in place would move
  ``ours-1`` too. The only guard is the golden check in ``rcdata/tests/test_sec_sections.py``, which compares
  ``ours-1``'s output against what the pre-move code returned. So ``ours-2`` adds its own helpers next to these
  rather than changing them, and if it ever needs to change one, the shared one is copied first. ``flag`` deserves
  the same care as the cut: its signals ride in the stored ``quality`` text.

``VERSIONS`` is the tuple of what exists. A caller that names an unknown version gets a ``ValueError``, because a
section stored under a version string nobody can reproduce is worse than a failed run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: The rule as it was before this module existed. Not edited again: see the module docstring.
OURS_1 = "ours-1"
#: The ``sec_forms`` layer's rule (#755): the five defects of ``ours-1`` fixed, and **nothing capped**. No board
#: reads it; moving one onto it is its own change, with the owner's before/after look at what the board shows.
OURS_2 = "ours-2"
#: Every version this module can cut with. A new rule is a new entry, never an edit of an old one.
VERSIONS: tuple[str, ...] = (OURS_1, OURS_2)
#: What a caller gets when it names none. The boards pin this explicitly anyway, so that a later default change
#: cannot move their text without the change being visible in their own code.
DEFAULT_VERSION = OURS_1

# ── Lengths and heading spelling (moved from module_value_chain/config.py) ──────────────────────────────────────
MIN_ITEM_CHARS = 2000
#: Caps the text ``ours-1`` returns; a missed stop regex can otherwise run for a megabyte. The ``sec_forms`` layer
#: stores sections uncut and lets its reader cap, so this limit belongs to ``ours-1`` and to the boards on it.
MAX_ITEM_CHARS = 200_000

#: hyphen-minus, U+2010..U+2015 dashes, minus sign -- a filer writes "Item 1 — Business" with any of them.
#:
#: Two spellings of one list, and a test asserts they agree. :data:`_DASH` is the **inside of a regex class**
#: and is what every pattern here uses; :data:`_DASH_CHARS` is the same characters as plain text, for the code
#: that strips or compares them. Round 5's first finding was :data:`_DASH` used as if it were the second: read
#: as a string of characters it is ``\ - u 2 0 1 5`` and so accepts the letter ``u`` and four digits while
#: refusing every real dash.
_DASH = r"\-\u2010-\u2015\u2212"
_DASH_CHARS = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
DASH = _DASH


def _spaced(word: str) -> str:
    """A heading word with a stray space inside it: "Bus iness", "BUSINES S", "Risk Fac tors". The filer's HTML
    breaks words that way, as it writes "TAB LE OF CONTENTS" (#315). One optional space per letter boundary."""
    return r"\s?".join(word)


spaced = _spaced

# 10-K: Item 1 Business -> stop at Item 1A (else 1B / 2). 20-F: Item 4 -> stop at Item 4A / 5. A 10-K stop needs
# the section title, so a cross-reference such as "see Item 1A of this report" does not cut the body short
# (#116 C17).
START_PATTERNS = {
    "10-K": (rf"\bitems?\s*1\s*\.?\s*(?:(?:and|&|,)\s*2\s*\.?\s*)?[\:{_DASH}]*\s*"
             rf"(?:(?:description\s+of|our)\s+(?:the\s+)?)?{_spaced('business')}"),
    "20-F": (rf"\bitem\s*4\s*[\.\:{_DASH}\s]\s*[{_DASH}]?\s*"
             r"information\s+on\s+the\s+(?:company|group)"),
}
STOP_PATTERNS = {
    "10-K": (rf"\bitem\s*1a\s*[\.\:{_DASH}\s]*\s*{_spaced('risk')}\s+{_spaced('factors')}"
             rf"|\bitem\s*1b\s*[\.\:{_DASH}\s]*\s*{_spaced('unresolved')}"
             rf"|\bitem\s*2\s*[\.\:{_DASH}\s]*\s*(?:description\s+of\s+)?{_spaced('propert')}"),
    "20-F": rf"\bitem\s*4a\s*[\.\:{_DASH}\s]|\bitem\s*5\s*[\.\:{_DASH}\s]",
}

# ── line-start headings (#400 10-K package WP1) ─────────────────────────────────────────────────────────────
# The served text breaks a heading over lines ("ITEM\n1A.", "ITEM 4. \n INFORMATION ON THE COMPANY") and splits
# words ("B usiness", "INFO RMATION"), and an item named inside a sentence ("see “Item 5. Operating ...”") is not a
# heading. So a heading starts a line, the gaps inside it may hold a new line, and the body ends at the next
# line-start item heading that is not a quoted reference. Measured on 32 filings of the #400 input checks (9 Nov
# 2026): the previous rule was wrong on about 20, most of them labelled ``ok`` (ITUB 16k of 293k, IH, ALKS, MREO, NBP).
_LINE_START = {
    "10-K": re.compile(r"(?im)^[^\S\n]*items?\s{0,3}(1|i)\b(?:\s*(?:and|&|,)\s*2\b)?\s*[.:]?\s*"
                       rf"(?:[{DASH}]+\s*)?(?:(?:description\s+of|our)\s+(?:the\s+)?)?{spaced('business')}"),
    "20-F": re.compile(r"(?im)^[^\S\n]*item\s{0,3}(4)\b\s*[.:]?\s*"
                       rf"(?:[{DASH}]+\s*)?{spaced('information')}\s+(?:on|about|of)\s+(?:the\s+)?"
                       r"(?!(?:our\s+)?executive)[\w&.-]+"),
}
_LINE_END = re.compile(r"(?im)^[^\S\n]*item\s{0,3}(1[abc]|[2-9][abc]?|1[0-9])\b")
_TOC_NEXT = re.compile(r"(?i)item\s*(1a|2|4a|5)\b")
_PAGE_NO = re.compile(r"[^\n]{0,80}\n[^\S\n]*\d{1,3}[^\S\n]*\n")          # the heading's next line is a page number
_REF_BEFORE = re.compile(r"(?i)([“‘]|\b(see|in|under|refer to)[^\S\n]*[“\"‘]?)[^\S\n]*$")
_BODY_MIN = 4_000            # shorter, it is an index block the contents check missed (TIGO, EC)


def _is_ref(text: str, m: re.Match) -> bool:
    before = text[max(0, m.start() - 40):m.start()].rstrip()
    return bool(_REF_BEFORE.search(before)) or "”" in text[m.start():m.start() + 200].split("\n", 1)[0]


def _line_item(text: str, key: str) -> tuple[int, int] | None:
    """``(body start, body end)`` of the line-start rule, or None when it finds no closed body."""
    for m in _LINE_START[key].finditer(text):
        if _TOC_NEXT.search(text[m.end():m.end() + 400]) or _PAGE_NO.match(text, m.end()) or _is_ref(text, m):
            continue
        own = m.group(1).lower()
        end = next((e.start() for e in _LINE_END.finditer(text, m.end() + MIN_ITEM_CHARS)
                    if e.group(1).lower() != own and not _is_ref(text, e)), None)
        if end is None:
            return None          # no closing heading: the old rule decides (and labels it overrun)
        if end - m.start() < _BODY_MIN:
            continue
        if key == "20-F":
            end = _before_regulation(text, m.end(), end)
        return m.end(), end
    return None


# 20-F 4.B's regulation chapter is not the business (PDD 87%, BABA 68% of 4.B; 10-K reading report A6, 8 Nov 2026).
_OVERVIEW = re.compile(r"(?im)^[^\S\n]*b\.[^\S\n]*business\s+overview\b")
_REGULATION = re.compile(r"(?im)^[^\S\n]*(?:(?:prc|chinese|government(?:al)?)\s+)?regulations?"
                         r"(?:\s+(?:and\s+supervision|overview|matters))?[^\S\n]*$")


def _before_regulation(text: str, start: int, end: int) -> int:
    b = _OVERVIEW.search(text, start, end)
    r = _REGULATION.search(text, b.end(), end) if b else None
    return r.start() if r and r.start() - start >= _BODY_MIN else end


# A Canadian Annual Information Form in a 40-F's primary document (CNQ): "General Development" to "Risk Factors"
# (10-K reading report A7). One start heading and one stop heading only, so it misses every form that titles the
# section differently -- 28 of the 88 stored exhibits by the 12 Nov 2026 measurement, whose numbers and reasoning
# are in issue #756, not in a file in this tree (of those 88, that measurement cuts 44 and misses 40: 28 with no
# heading we know, 5 with no stop, 7 rejected as too short). ``ours-2`` is where that improvement landed
# (#756, the block at the end of this module); this one is the board's and does not move.
_AIF = re.compile(r"(?i)annual\s+information\s+form")
_AIF_START = re.compile(r"(?im)^[^\S\n]*general\s+development\s+of\s+(?:the\s+|our\s+)?business[^\S\n]*$")
_AIF_END = re.compile(r"(?im)^[^\S\n]*risk\s+factors[^\S\n]*$")


def _aif_business_ours_1(text: str) -> str | None:
    if not _AIF.search(text or ""):
        return None
    for m in _AIF_START.finditer(text):
        if _PAGE_NO.match(text, m.end()):
            continue
        e = next((e for e in _AIF_END.finditer(text, m.end()) if not _is_ref(text, e)), None)
        if e is not None and e.start() - m.start() >= _BODY_MIN:
            return text[m.end():e.start()].strip()[:MAX_ITEM_CHARS]
    return None


def _slice_item_ours_1(text: str, form: str = "10-K") -> tuple[str | None, str]:
    """``(item_text, quality)``: ok / capped / short / overrun / missing, exactly as the rule was on 12 Nov 2026.

    A 10-K opens with a table of contents that lists ``Item 1. Business`` and ``Item 1A. Risk Factors`` back to
    back, so the *first* start match is the TOC and the slice is a few dozen characters. We therefore try every
    start and keep the longest section -- the real body, not the contents page.

    ``capped``: the section is longer than :data:`MAX_ITEM_CHARS`, so the slice is cut at the cap and holds only
    the start of Item 1. ``overrun``: no stop header follows the start, so the slice runs on into Item 1A and
    beyond (or up to the cap). It is kept, but not labelled ``ok``.
    """
    text = text or ""
    if not text.strip():
        return None, "missing"

    key = "20-F" if str(form).upper().startswith("20") else "10-K"
    start_re = re.compile(START_PATTERNS[key], re.I)
    stop_re = re.compile(STOP_PATTERNS[key], re.I)

    line = _line_item(text, key)
    if line is not None:
        item = text[line[0]:line[1]].strip()
        # A section longer than the cap holds only part of Item 1, so it is not ``ok``.
        return item[:MAX_ITEM_CHARS], "capped" if len(item) > MAX_ITEM_CHARS else "ok"

    best: str | None = None
    stopped = False
    for start_m in start_re.finditer(text):
        stop_m = stop_re.search(text, start_m.end())
        end = stop_m.start() if stop_m else min(len(text), start_m.end() + MAX_ITEM_CHARS)
        item = text[start_m.end():end].strip()
        if len(item) >= MIN_ITEM_CHARS:
            # The first section long enough is the real body; the TOC's entry is tiny, and a later
            # cross-reference can otherwise win on length.
            best, stopped = item, stop_m is not None
            break
        if best is None or len(item) > len(best):
            best, stopped = item, stop_m is not None
    if best is None:
        return None, "missing"

    # A section longer than the cap holds only part of Item 1, so it is not ``ok``.
    capped = len(best) > MAX_ITEM_CHARS
    best = best[:MAX_ITEM_CHARS]
    if len(best) < MIN_ITEM_CHARS:
        return best, "short"
    return best, ("overrun" if not stopped else ("capped" if capped else "ok"))


# ── ours-2: the five defects of the plan's section 5 (#755) ─────────────────────────────────────────────────
# Its own patterns and its own line rule, next to ``ours-1``'s and not instead of them -- the module docstring's
# rule, and the only honest way to keep a stored section reproducible by the version it names.

#: **The word "item" is a heading word too.** ``ours-1`` spells every other word of a heading with
#: :func:`_spaced`, because the filer's HTML breaks words over tags, but spells ``item`` literally -- so VFF's
#: "ITE M 1A" is not a heading, Item 1A is missed, and the business slice runs 111,000 characters into the risk
#: factors before something else stops it.
_ITEM = _spaced("item")

#: **A sub-item is not its item.** PCVX's exhibit list quotes "Item 1.01 Entry into a Material Definitive
#: Agreement" (an 8-K's item), which must not be read as Item 1. The number may be followed by a full stop, by a
#: colon or by nothing -- but not *immediately* by a digit, which is what makes "1.01" a different item from "1".
#:
#: **A full stop, then two digits, and no newline crossed.** Three spellings were tried and the first two were
#: wrong, each in its own way:
#:
#: - ``(?![.,]\s*\d)`` let the lookahead reach over the newline of "Item 1A.\n12\nRisk Factors" -- a heading with
#:   its page number printed under it -- and threw the heading away. The slice ran past Item 1A and still said
#:   ``ok``, because :func:`_holds_another_item` reads this pattern too and was blind to the same heading.
#: - ``(?![.,]\d)`` fixed that and lost the defect it exists for: :func:`strip_html` turns every tag into a
#:   space, so a filer's ``<b>Item 2.</b><b>01</b> Completion of Acquisition`` arrives as "Item 2. 01
#:   Completion" -- which is PCVX's exhibit list again, with a space in it. That is the same tag-splitting
#:   :func:`spaced` exists for, so the guard has to allow for it as the heading words do.
#:
#: The spelling kept reads "a full stop, any spaces that are not a newline, then exactly two digits": the sub-item
#: numbers filers write are .01 to .99, and the spaces it reaches over are **named one by one** -- a space, a
#: tab, a no-break space, a narrow no-break space and an ideographic space. Those five are a choice and not a
#: complete set: ``&ensp;``, ``&emsp;`` and ``&thinsp;`` are entities too and are not on it, because
#: :func:`strip_html` turns a tag into a space, a tab or a no-break space and nothing wider. Everything else keeps its page number a page number:
#: "Item 1A.\f12\nRisk Factors" is the heading of Item 1A, and so are the versions written with a carriage
#: return, a form feed, U+2028 or U+0085. A class spelled by exclusion (``[^\S\r\n\f\v]``) read as a space
#: every separator it had not thought of, which is why this one lists what it allows instead. "Item 7. 2024 Results" and "Item 2. 3 properties" are items, as they are to ``ours-1``;
#: so now is "Item 2,3 and 4", because a comma never introduced a sub-item and the guard no longer mentions one.
#:
#: **What it costs:** "Item 2. 15 properties are leased" -- a heading whose title opens with a two-digit
#: number -- is not read as a heading, and "Item 16. 10-K Summary" is the most plausible real spelling of that.
#: The other side of it is "Item 2 .01", a sub-item whose tag split before the full stop, which no spelling here
#: rejects. Both reach every place this pattern is read, the end of a slice included, and not only
#: :func:`_holds_another_item`; the price is paid for the sub-item shape that was really measured.
_NOT_SUBITEM = r"(?!\.[ \t\xa0\u202f\u3000]*\d\d\b)"

#: ``ours-2``'s gap inside a heading: whitespace, or a zero-width character. :func:`strip_html` turns a tag into
#: a space but leaves ``&#8203;`` as it found it, and Southern Missouri Bancorp prints its headings as
#: "Item 1. \u200b \u200b Description of Business" -- where ``\s*`` stops and the heading is missed, leaving a
#: 155,000-character Item 1 uncut (13 Nov 2026 full-disk check). ``strip_html`` is the board's and may not move,
#: so the gap is spelled here instead.
_GAP = r"[\s\u200b\u200c\u200d\ufeff]"
#: What may stand between the word *item* and its number. ``ours-1`` allows a gap only, so "ITEM. 1 BUSINESS"
#: (Eos Energy) and "ITEM - 1. Business" (FB Financial) are not headings to it and both filings cut to nothing.
_NUM_SEP = rf"{_GAP}{{0,3}}(?:[.:|{_DASH}]{_GAP}{{0,3}})?"
#: ... and between the number and the title. AIG prints "ITEM 1 | Business" as the heading **and** as every
#: page's running header; ``ours-1`` found no heading at all there and cut 58,722 characters out of a
#: cross-reference instead, labelled ``ok`` (13 Nov 2026 full-disk check).
_TITLE_SEP = rf"{_GAP}*[.:|]?{_GAP}*(?:[{_DASH}]+{_GAP}*)?"
#: The number itself. ``l`` is the lower-case letter: BRT Apartments writes "Item l. Business." with an ell for
#: the one, which no later run will ever spell differently. :func:`_item_no` keeps it as read, so it matches no
#: stop and only ever starts a slice.
_ONE = r"(1|i|l)"

_LINE_START_2 = {
    # "Items 1. and 2." -- a full stop after the first number (Cheniere, 13 Nov 2026 real-filing check)
    "10-K": re.compile(rf"(?im)^[^\S\n]*{_ITEM}s?{_NUM_SEP}{_ONE}\b(?:\s*\.?\s*(?:and|&|,)\s*2\b)?"
                       rf"{_TITLE_SEP}(?:(?:description\s+of|our)\s+(?:the\s+)?)?"
                       rf"{spaced('business')}"),
    "20-F": re.compile(rf"(?im)^[^\S\n]*{_ITEM}{_NUM_SEP}(4)\b{_TITLE_SEP}"
                       rf"{spaced('information')}\s+(?:on|about|of)\s+(?:the\s+)?"
                       r"(?!(?:our\s+)?executive)[\w&.-]+"),
}
#: Item 1 is the first item of Part I, so a line-start "Item 1" on the line after a "PART I" line is its heading
#: whatever title follows underneath. Exelon prints "PART I \n ITEM 1. \n General" -- the title in its contents
#: is "BUSINESS" and the one over the body is not -- and ``ours-1`` cut 38,817 characters out of a
#: cross-reference instead, labelled ``ok``. Read only when the titled pattern above finds no heading, and the
#: contents page's own "PART I / ITEM 1." is thrown out by the guards that already stand there. 10-K only: a
#: 20-F's Item 4 is not the first item of its part.
_PART_I_ITEM_1 = re.compile(rf"(?im)^[^\S\n]*part{_GAP}{{1,3}}i{_GAP}*[.:]?[^\S\n]*\n"
                            rf"[^\S\n]*{_ITEM}s?{_NUM_SEP}{_ONE}\b[^\S\n]*[.:|]?[^\S\n]*$")
#: The letter may stand apart from its number: Tempus AI writes "Item\u20091 A. Risk Fact ors" (13 Nov 2026 real-filing
#: check), and without the gap allowed Item 1A was no heading, the slice ran through the risk factors to Item 1B and
#: still said ``ok``. The gap counts only with the full stop, colon or dash a heading puts after the letter, so a
#: tag-split "Item 1 B usiness" stays its own item's title (Fable review). :func:`_item_no` drops the gap again.
_LINE_END_2 = re.compile(rf"(?im)^[^\S\n]*{_ITEM}{_NUM_SEP}(1[abc]|1[^\S\n][abc](?=[.:{DASH}])|[2-9](?:[abc]|[^\S\n][abc](?=[.:{DASH}]))?|1[0-9])\b{_NOT_SUBITEM}")


def _item_no(m: re.Match) -> str:
    """The item number ``m`` read, lower case and without the gap :data:`_LINE_END_2` allows: "1 A" -> "1a"."""
    return re.sub(r"\s", "", m.group(1)).lower()

#: ``ours-1``'s :data:`START_PATTERNS` and :data:`STOP_PATTERNS`, spelled with :data:`_ITEM`. Copies and not
#: edits of those two, because the boards read them (module docstring). :data:`_NOT_SUBITEM` is **not** on them,
#: and that is right for every branch but one: each requires a title after the number ("business", "risk
#: factors", "propert"), which a sub-item like "Item 1.01 Entry into a Material Definitive Agreement" never
#: carries. **The exception is the 20-F stop**, ``item 4a`` or ``item 5`` followed by punctuation and nothing
#: else, which "Item 5.02 Departure of Directors" matches: a 20-F whose Item 4 quotes an 8-K item ends there.
#: Adding the guard to it is a change to what the cut returns, so it is measured and made on its own (#755's
#: follow-up), not slipped in with a comment. :data:`_LINE_END_2` matches a bare number everywhere and carries
#: the guard for that reason.
START_PATTERNS_2 = {
    "10-K": (rf"\b{_ITEM}s?\s*1\s*\.?\s*(?:(?:and|&|,)\s*2\s*\.?\s*)?[\:{_DASH}]*\s*"
             rf"(?:(?:description\s+of|our)\s+(?:the\s+)?)?{_spaced('business')}"),
    "20-F": (rf"\b{_ITEM}\s*4\s*[\.\:{_DASH}\s]\s*[{_DASH}]?\s*"
             r"information\s+on\s+the\s+(?:company|group)"),
}
STOP_PATTERNS_2 = {
    "10-K": (rf"\b{_ITEM}\s*1[^\S\n]?a\s*[\.\:{_DASH}\s]*\s*{_spaced('risk')}\s+{_spaced('factors')}"
             rf"|\b{_ITEM}\s*1[^\S\n]?b\s*[\.\:{_DASH}\s]*\s*{_spaced('unresolved')}"
             rf"|\b{_ITEM}\s*2\s*[\.\:{_DASH}\s]*\s*(?:description\s+of\s+)?{_spaced('propert')}"),
    "20-F": rf"\b{_ITEM}\s*4a\s*[\.\:{_DASH}\s]|\b{_ITEM}\s*5\s*[\.\:{_DASH}\s]",
}

#: A body that is a cross-reference and not a section: "the information required by this item is incorporated
#: herein by reference", "see pages F-1 through F-30". Six of twenty Item 8 slices in the census were this, and
#: ``ours-1`` called each of them ``short`` -- which reads as a cut that went wrong, when the filing really does
#: hold no prose there and no later run will find any.
_POINTER = re.compile(r"(?i)\b(?:incorporated\s+(?:herein\s+)?by\s+reference"
                      r"|included\s+(?:herein|elsewhere)\s+in\s+this"
                      r"|see\s+(?:the\s+)?(?:pages?|index|financial\s+statements)"
                      r"|reference\s+is\s+(?:hereby\s+)?made"
                      r"|(?:are|is)\s+set\s+forth\s+(?:beginning\s+)?on\s+page"
                      r"|(?:appears?|appearing|begins?)\s+on\s+page"
                      r"|required\s+by\s+this\s+item\s+(?:is|are)\s+(?:set\s+forth|contained|included))")


#: ``ours-1``'s :data:`_REF_BEFORE` with a part named between the reference word and the item: Cal-Maine's "see
#: Note 15 – Segment Reporting in Part II.\nItem 8. Notes to Consolidated Financial Statements" starts a line, so
#: only the words before it say it is a mention; without this Item 1 ended there, 4,153 characters, ``ok`` (13 Nov
#: 2026 real-filing check). A copy, because ``_REF_BEFORE`` is the board's.
_REF_BEFORE_2 = re.compile(r"(?i)\b(see|in|under|refer to)\s*part\s+([ivx]+)\s*([.,]?)[^\S\n]*[“\"‘]?[^\S\n]*$")
#: Only when the part is the item's own: "in Part III.\nItem 1A. Risk Factors" ends a sentence about Part III and
#: is followed by a real heading, which a part-blind test hid from both the cut and the label (Fable review).
_PART_OF = {"i": ("1", "2", "3", "4"), "ii": ("5", "6", "7", "8", "9"), "iii": ("10", "11", "12", "13", "14"),
            "iv": ("15", "16")}
_ITEM_NO = re.compile(rf"(?i)[^\S\n]*{_ITEM}s?\s{{0,3}}(\d+)")


def _part_ref(text: str, m: re.Match, before: str, key: str = "10-K") -> bool:
    """True when ``before`` names the part that ``m``'s item belongs to. A 10-K's parts only: a 20-F numbers its
    parts differently (Item 5 is Part I there), and none of the measured 20-F cuts needed this (Fable review).

    Part I is Item 1's own part, so "...set forth in Part I.\nItem 1A." can be a sentence ending before the real
    end of Item 1: for Part I only "see" / "refer to", or a comma ("in Part I, Item 1A"), make a reference."""
    p, n = _REF_BEFORE_2.search(before), _ITEM_NO.match(text, m.start())
    if key != "10-K" or not (p and n) or n.group(1).lstrip("0") not in _PART_OF.get(p.group(2).lower(), ()):
        return False
    return p.group(2).lower() != "i" or p.group(1).lower() != "in" or p.group(3) == ","


#: Two more reference words, read on the pattern path only: "The disclosures set forth in this item are qualified
#: by Item 1A. Risk Factors" (First Hawaiian, Cullen/Frost, Valley, Park National) and 'please read "Item 4 –
#: Information on the Company' (Teekay) -- 13 Nov 2026 full-disk check.
_BY_BEFORE = re.compile(r"(?i)\b(?:by|read)[^\S\n]*[“\"‘]?[^\S\n]*$")


def _words_before_ref(text: str, m: re.Match, key: str = "10-K") -> bool:
    """True when a reference word, an opening quote or the item's own part comes right before ``m`` mid-line."""
    before = text[max(0, m.start() - 40):m.start()].rstrip()
    return _starts_mid_line(text, m) and (bool(_REF_BEFORE.search(before) or _BY_BEFORE.search(before))
                                          or _part_ref(text, m, before, key))


def _inline_ref(text: str, m: re.Match, key: str = "10-K") -> bool:
    """True when ``m`` is a mention of an item inside a line, rather than a heading of its own.

    The titled fallback below needs this and the line-end search does not, because the headings it reads are
    **inline** patterns: they match "Item 1A. Risk Factors" wherever it appears, including in the middle of a
    sentence that only talks about Item 1A.

    Three tests, and each is here for a measured reason.

    - **The words before it.** :data:`_REF_BEFORE` -- "see", "under", "refer to" or an opening quote, right
      before the match. This is the half that does the work.
    - **A heading that starts its own line is kept even so.** The before-test reads the forty characters before
      the match with the newline stripped off, so a filer who writes "See\nItem 1A. Risk Factors" as a page's
      last word and the next page's heading looks like a reference to it. ``ours-1`` rejects that heading in its
      line rule and then stops on it in its pattern rule, and ends Item 1 in the right place; the line test
      keeps that answer (the shared ``suspect_tail_cross_reference`` case).
    - **A closing quotation mark after it, but only an unopened one.** ``...under "Item 1A. Risk Factors" of
      this report`` closes a quote it did not open, and that is what marks it a mention. :func:`_is_ref` -- the
      shared test, which ``ours-1`` applies to headings that begin a line -- looks for any ``”`` in the
      next 200 characters of the line, and **this function may not reuse it**: for an inline stop those 200
      characters are the next item's prose, so one quoted term in its first sentence ("...the risks (the
      “Risks”) below") made the real Item 1A heading a mention. Measured: ``ok`` on a 52,273-character
      slice with every risk factor in it, where ``ours-1`` answers ``ok`` on 26,999. With straight quotes the
      same filing was already right, which is how the cause was found.

    **The before-test is not applied without the line test, deliberately.** An inline mention that no reference
    word precedes -- "...of this report. Item 1A. Risk Factors" in a filing served as one paragraph -- is
    indistinguishable from a heading here, and ends the slice. ``ours-1`` does the same, so this is parity and
    not a regression; which answer is right is a question for the hand-checked set (#763), not for a shape
    built in a test.
    """
    return _starts_mid_line(text, m) and _ref_ours_2(text, m, key)


def _starts_mid_line(text: str, m: re.Match) -> bool:
    """True when something other than whitespace precedes ``m`` on its own line."""
    return bool(text[text.rfind("\n", 0, m.start()) + 1:m.start()].strip())


def _ref_ours_2(text: str, m: re.Match, key: str = "10-K") -> bool:
    """``ours-2``'s reference test: the words before the match, and a closing quote it did not open.

    Used wherever ``ours-2`` asks "is this heading really a heading" -- the start filter, the line-end search,
    :func:`_holds_another_item` and, through :func:`_inline_ref`, the titled fallback. It is ``ours-1``'s
    :func:`_is_ref` with the second half corrected, and it is a function of its own because ``_is_ref`` is the
    board's and may not move.

    **The correction.** ``_is_ref`` calls a heading a reference when any ``”`` appears in the 200
    characters after it on the same line. That is right for a heading alone on its line and wrong for a
    **run-in** heading, which shares its line with the section's first sentence -- and a filer who writes
    "Item 1A. Risk Factors You should consider the risks (the “Risks”) below" has written a heading,
    not a reference to one. Measured on that filing: the line-end search skipped the real Item 1A, the slice
    closed on Item 1B, :func:`_holds_another_item` skipped the same heading for the same reason, and the answer
    was ``ok`` on 52,273 characters with every risk factor inside Item 1. ``ours-1`` answers the same, so this
    is a defect inherited and not caused -- and it is the exact shape ``ours-2`` exists to stop labelling
    ``ok``. Only a quotation mark that closes one this window never saw opened marks a mention.

    The window ends at the first line ending at or after the match, not at the first one anywhere: a heading
    whose own title wraps ("“Part I, Item\n1A. Risk Factors”") would otherwise have its window cut
    inside itself, and the quote that marks it a mention falls outside.

    **How much of this half is pinned.** The quote test decides an answer only when the opening quote is not
    immediately before the match -- anything closer than that is already a reference by :data:`_REF_BEFORE` --
    and when neither the fallback nor the pattern path reaches the same stop by another route. An earlier round
    called six mutations of the details inside it unpinned; measuring further found that three of them each
    decide a cut (the closer taken from the far end of the window, the window cut at the first newline
    anywhere, and the before-text left unstripped before a blank line), and those are tests now.

    The opening quote's direction belongs to that list, and an earlier round of this issue called it equivalent
    on a measurement that could not see it: the fixture ended with the risk factors, so the titled fallback
    reached the same stop either way. Put an item after them and searching from the far end finds the second
    quoted pair's opener, reads the first pair's closer as unopened and gives ``ok`` on 41,064 characters with
    every risk factor inside Item 1. It is a test now.

    One really is equivalent: ``<=`` for ``<`` in the comparison, because ``closed == opened`` needs both to be
    -1 and ``closed >= 0`` has already excluded that. One more, the window measured from the match's end rather
    than its start, only moves where the 200-character edge falls; it takes a quotation mark exactly 192
    characters of filler past the number to tell the two apart, and neither answer is the right one. The real
    question for all of this is how often each shape occurs in filings, which is #763's hand-checked set and
    not a built string.
    """
    before = text[max(0, m.start() - 40):m.start()].rstrip()
    if _REF_BEFORE.search(before) or _part_ref(text, m, before, key):
        return True
    line_end = text.find("\n", m.end())
    if _cites_its_title(text, m, line_end):
        return True
    window = text[m.start():min(m.start() + 200, line_end if line_end >= 0 else len(text))]
    opened, closed = window.find("“"), window.find("”")
    if closed < 0:
        return False
    return opened < 0 or closed < opened


#: Just past the item's number inside a match, wherever the match itself ends. The four patterns end in
#: different places -- the line-end one after the number, the starts and the stops after the **title** -- and
#: every test below asks what follows the *number*, so each of them needs this and not ``m.end()``.
_NUMBER = re.compile(rf"(?i)[^\S\n]*{_ITEM}s?{_NUM_SEP}(?:[0-9]+(?:[a-c]|[^\S\n][a-c](?=[.:{_DASH}]))?|[il][a-c]?)")


def _after_number(text: str, m: re.Match) -> int:
    """The offset just past the item number ``m`` begins with, or the end of the match when it has none."""
    n = _NUMBER.match(text, m.start())
    return n.end() if n else m.end()


#: The words a filing puts between an item's number and the rest of the sentence when it is **citing** that
#: item rather than starting it: "Item 1A entitled ...", "Item 7 of this report", "Item 1A below". A heading
#: carries its own title in that place, and no item's title is one of these words.
_CITED_PHRASE = re.compile(r"(?i)(?:[.,:;()\[\]]|[^\S\n]){0,8}"
                           r"(?:entitled|captioned|titled|under[^\S\n]+the[^\S\n]+(?:caption|heading)"
                           r"|(?:of|in|to)[^\S\n]+(?:this|our|the[^\S\n]+(?:accompanying|attached))"
                           r"|hereof|above|below)\b")

#: A quoted run right after the match, which :data:`_CITED_PHRASE` may then follow: ``Item 1. Business
#: "Overview" of this report``. The run must begin within four spaces of the match's end, which is what keeps
#: this away from a run-in heading whose first sentence defines a term further along ("Item 1A. Risk Factors
#: You should consider the risks (the "Risks") below") -- there the match ends after the *number*, and the
#: quotation mark is a title and a sentence away from it.
_QUOTED_RUN = re.compile(r"[^\S\n]{0,4}[“‘\"][^\n”’\"]{0,80}[”’\"]")

#: What a citation's closing mark may be followed by on its line, where a heading's may not. A letter is the
#: sentence carrying on; a comma or a semicolon, with spaces allowed before it, is a clause carrying on; a
#: closing bracket with no opening one **between it and the mark** belongs to a parenthetical the sentence
#: began earlier, which is the commonest cross-reference form there is: "(see Part I,\nItem 1A "Risk
#: Factors")." That test looks only at the tail, so ``Item 1A ("Risk Factors").``, whose bracket does open on
#: that line, is a citation as well -- which is the right answer, by the bracket the tail ends with rather
#: than the one it does not see. A heading's line ends after the title, or with a page number (bare, in
#: brackets or after leaders), a full stop, a colon or a dash.
_A_LETTER = re.compile(r"[^\W\d_]")
_CLAUSE_GOES_ON = re.compile(r"[^\S\n]*[,;]")
_UNOPENED_CLOSER = re.compile(r"[^([\n]*[)\]]")

#: An opening quotation mark -- curly, single or straight -- with nothing but punctuation between it and the
#: number, which is where a filing puts the item's **title** when it cites it: ``Item 1A, "Risk Factors," ...``.
_CITED_QUOTE = re.compile(rf"(?:[.,:;(\[\){_DASH}]|[^\S\n]){{0,12}}([“‘\"])")
_CLOSER = {"“": "”", "‘": "’", '"': '"'}


def _cites_its_title(text: str, m: re.Match, line_end: int) -> bool:
    """True when the words right after ``m``'s item number say the sentence is citing that item.

    Three spellings, all measured on the shapes a 10-K really uses, and all asked of the text that follows the
    **number** rather than the match (:func:`_after_number`):

    - a reference word in place of the title (:data:`_CITED_PHRASE`): "Item 1A entitled", "Item 7 of this
      report", "Item 1A below". Nothing in that pattern crosses a line ending. Spelled with ``\\s`` it did,
      and a heading that carries only its number on its line -- the title printed underneath -- became a
      citation whenever the next line opened with one of these words ("Item 1A.\\nIn our business we face
      risks"): ``ok`` on 15,840 characters with the risk factors inside, where ``ours-1`` gives 13,000, and
      :func:`_holds_another_item` reads the same test, so the label was blind to it too;
    - the title in quotation marks after the item's own title, followed by such a phrase
      (:data:`_QUOTED_RUN`): "Item 1. Business "Overview" of this report";
    - the title in quotation marks (:data:`_CITED_QUOTE`), with **any** of four things that a heading's own
      title does not carry: a comma between the number and the opening mark (a heading's number takes a full
      stop, a dash or nothing); a full stop, comma, semicolon or colon inside the closing mark; a line that
      goes on after it in words (:data:`_A_LETTER`); or a tail that goes on in punctuation a heading's line
      never ends with (:data:`_CLAUSE_GOES_ON`, :data:`_UNOPENED_CLOSER`).

      Each of those was measured. Without the first two, a citation that ends its paragraph --
      ``see Part I, Item 1A, "Risk Factors."``, with the full stop inside the quotation marks as US
      punctuation puts it -- has nothing after the closing mark either, and was read as a heading: Item 1
      cut to 13,024 characters of 26,050 and labelled ``ok``. Without the third, a filer's own quoted
      heading -- ``Item 1A. "Risk Factors"`` alone on its line, or with a page number or a full stop after
      it -- was read as a citation, and the slice ran through the risk factors and still said ``ok``. The
      fourth came of the third: once only a letter counted, every parenthetical cross-reference became a
      heading, because its tail is a bracket and a full stop.

      A third cost belongs beside the two below: a citation whose sentence ends with the full stop **outside**
      the mark and which starts its own line -- ``as discussed in Part I,\nItem 1A "Risk Factors".`` -- is
      read as a heading, Item 1 cut to 13,024 characters of 26,049. It is the same line as the heading
      ``Item 1A. "Risk Factors".``, which a test pins the other way, so again no rule can have both.

      What it costs, both sides of it the same size: a heading spelled ``Item 1A, "Risk Factors"``, with a
      comma where the full stop goes, and one spelled ``Item 1A. "Risk Factors."``, by a filer who ends his
      titles with a full stop as "ITEM 1. BUSINESS." does, are both read as citations. The second is the same
      line as a citation that ends its paragraph, so no rule can have them both; no filing measured for this
      issue writes either.

    **What of this is pinned.** Each half decides a cut on a shape in
    ``tests/test_sec_sections_ours_2.py``, and so does asking the question of the number instead of the match.
    Three details are not, and a reader changing one should expect the tests to stay green: the bound on the
    closing mark's search (see the comment below, which shows it cannot change an answer), the four spaces the
    quoted run may begin after the match, and the line restriction on the gaps **inside** a phrase, which no
    heading carries at all -- a filer who wraps a citation between "under" and "the caption" has still
    written one, and that shape is not read as a citation here. The second is a choice about distance, and the pattern path
    recovers the same answer whichever distance is allowed on every sentence measured; the question of how far
    a real filer's quotation mark sits from a heading belongs to the hand-checked set (#763).

    What this does not catch, measured and left: ``Item 1A. Risk Factors, and the section "Forward-Looking
    Statements" below.`` -- the title is in its usual place and the quotation marks are around something else,
    so the line is read as the heading and Item 1 ends there (13,024 characters where ``ours-1`` gives 26,099).
    A combined heading -- ``Item 1A. Risk Factors, and Item 1B. Unresolved Staff Comments`` -- is the same shape
    with the opposite answer, so telling them apart is a question for the hand-checked set (#763).
    """
    a = _after_number(text, m)
    if _CITED_PHRASE.match(text, a):
        return True
    # The title in quotation marks **after** the item's own title, and then a reference phrase: "Item 1.
    # Business "Overview" of this report". Without the phrase this is a run-in heading whose first sentence
    # defines a term -- "Item 1. Business "Acme," "we" and "our" refer to Acme Corp." -- which is the shape
    # round 4 of this issue exists to keep.
    run = _QUOTED_RUN.match(text, m.end())
    if run is not None and _CITED_PHRASE.match(text, run.end()):
        return True
    q = _CITED_QUOTE.match(text, a)
    if q is None:
        return False
    stop = line_end if line_end >= 0 else len(text)
    # The bound on the search is a bound and not a rule: a closing mark past ``stop`` leaves nothing in
    # ``text[close + 1:stop]``, so dropping it changes no answer. A quoted title that wraps onto the next line
    # is not read as a citation either way, and :data:`_REF_BEFORE` is what catches those.
    close = text.find(_CLOSER[q.group(1)], q.end(1), stop)
    if close < 0:
        return False
    if "," in q.group(0):
        return True
    if text[close - 1] in ".,;:":
        return True
    tail = text[close + 1:stop]
    return bool(_CLAUSE_GOES_ON.match(tail) or _UNOPENED_CLOSER.match(tail) or _A_LETTER.search(tail))


#: ``ours-1``'s :data:`_TOC_NEXT` spelled with the separators :data:`_NUM_SEP` allows. Without it the start
#: pattern reads "ITEM - 1." and the contents test does not, so a filing whose contents page is spelled the way
#: its heading is passes the guard and the contents entry is cut as the body -- ``overrun``, from the contents
#: page (Fable review, 13 Nov 2026). A copy, because :data:`_TOC_NEXT` is the board's.
_TOC_NEXT_2 = re.compile(rf"(?i){_ITEM}{_GAP}*(?:[.:|{_DASH}]{_GAP}*)?(?:1{_GAP}?a|2|4{_GAP}?a|5)\b")


def _toc_next_ours_2(text: str, m: re.Match, key: str = "10-K") -> bool:
    """True when a later item is **listed** right after ``m``, as a contents page lists it.

    ``ours-1``'s test is the bare appearance of a later item's number within 400 characters
    (:data:`_TOC_NEXT`), which also throws away a real heading whose own first sentence names another item:
    Southern Missouri Bancorp opens with "Item 1. Description of Business \n The disclosures set forth in this
    Item 1. are qualified by Item 1A. Risk Factors", and that was the filing's only line-start heading. With it
    gone the cut fell through to the pattern path and ran 199,999 characters -- the whole of the risk factors
    and beyond (13 Nov 2026 full-disk check). A contents entry begins its own line or follows another entry; a
    mention inside a sentence is preceded by a word that says so, which is what :func:`_words_before_ref`
    reads.
    """
    return any(not _words_before_ref(text, n, key)
               for n in _TOC_NEXT_2.finditer(text, m.end(), m.end() + 400))


def _line_item_ours_2(text: str, key: str) -> tuple[int, int, bool] | None:
    """``(body start, body end, a heading closed it)`` of the line-start rule, or None when it finds no body.

    The difference from ``ours-1``'s :func:`_line_item`: a start with no closing heading is **not** given up on.
    ``ours-1`` returned None there and let the pattern path decide, which is where its cap came in; nothing is
    capped here, so the slice is kept and labelled instead.

    **Three ways it can end, in this order.** A line-start heading of another item is the real end. Failing that,
    a titled stop -- :data:`STOP_PATTERNS_2`, the same inline patterns ``ours-1``'s pattern path stops on -- ends
    it just as well: "Item 1A. Risk Factors" is where Item 1 stops whether or not it begins a line, and reading
    it is the only thing that keeps the slice as short as ``ours-1``'s. Only when neither is there does the body
    run to the end of the document, and then ``closed`` is False and the label is ``overrun``.

    The titled stop is taken from the start of the body, with no minimum length, and the mentions are filtered
    out by :func:`_inline_ref` instead. An earlier version searched from ``MIN_ITEM_CHARS`` on, to walk past the
    heading a filer names in his first paragraph -- and so walked past a **real** stop in a filing whose Item 1
    is genuinely short, closed the slice on the next stop instead, and called the result ``ok`` with Item 1A
    inside it. ``ours-1`` answered ``short`` on that filing. Position cannot tell a mention from a heading; the
    words around it can.
    """
    stop_re = re.compile(STOP_PATTERNS_2[key], re.I)
    starts = list(_LINE_START_2[key].finditer(text))
    if key == "10-K":
        # The titleless heading is read only when no titled one is left, so a filing that spells its heading in
        # full is cut exactly as before (:data:`_PART_I_ITEM_1`).
        starts += list(_PART_I_ITEM_1.finditer(text))
    for m in starts:
        if _toc_next_ours_2(text, m, key) or _PAGE_NO.match(text, m.end()) or _ref_ours_2(text, m, key):
            continue
        own = m.group(1).lower()
        end = next((e.start() for e in _LINE_END_2.finditer(text, m.end() + MIN_ITEM_CHARS)
                    if _item_no(e) != own and not _ref_ours_2(text, e, key)), None)
        if end is None:
            end = next((t.start() for t in stop_re.finditer(text, m.end()) if not _inline_ref(text, t, key)), None)
        closed = end is not None
        if end is None:
            end = len(text)
        if end - m.start() < _BODY_MIN:
            continue
        if key == "20-F":
            end = _before_regulation(text, m.end(), end)
        return m.end(), end, closed
    return None


def _holds_another_item(item: str, own: str) -> bool:
    """True when ``item`` holds the line-start title of an item that is not ``own``.

    This is what tells VFF's 111,000-character slice from a long Item 1. A slice that swallowed the next item's
    heading is not the section it claims to be, however it ended -- ``ours-1`` could only say so when no stop
    matched at all, and so labelled this one by its length instead.
    """
    return any(_item_no(m) != own and not _ref_ours_2(item, m, "20-F" if own == "4" else "10-K")
               for m in _LINE_END_2.finditer(item))


#: An annual report filed **as** the Form 10-K or 20-F, with a table mapping each item number to places in that
#: report rather than item sections of its own. Five of them were read on 13 Nov 2026 and none can be cut by
#: item: McDonald's index gives a page number and no caption at all ("Item 1 Business Page 3"), and Barclays',
#: Pearson's, Santander's and HSBC's map Item 4.B to scattered ranges and notes -- "ii (Market and other data),
#: 14-24, 186-188, 300-313, 355-357 (Note 2)" -- so there is no one span to cut and no caption to find it by.
#: ``crossref`` says that, where ``short`` and ``missing`` read as a cut that went wrong and invite another try.
#: The ``whole`` row still holds the document, so nothing is lost by not cutting it.
#: Both halves begin a line, because the table has a heading of its own and a sentence does not. Unanchored, the
#: first branch read an ordinary bank's "This cross-reference index to Guide 3 disclosures in this Annual Report
#: on Form 10-K" as an index and told a wrongly-short cut not to try again (Fable review, 13 Nov 2026).
_INDEXED = re.compile(r"(?im)^[^\S\n]*(?:sec[^\S\n]+)?form[^\S\n]*(?:10-k|20-f)[^\n]{0,60}"
                      r"(?:cross(?:[^\S\n]|-){0,3}reference|item[^\S\n]{1,3}number)"
                      r"|^[^\S\n]*cross(?:[^\S\n]|-){0,3}references?[\s\S]{0,120}?form[^\S\n]*(?:10-k|20-f)")


def _quality_ours_2(item: str, own: str, closed: bool, text: str = "") -> str:
    """The label for a slice ``ours-2`` has already cut. Never ``capped``: nothing here caps."""
    if len(item) < MIN_ITEM_CHARS:
        if _POINTER.search(item):
            return "pointer"
        return "crossref" if _INDEXED.search(text) else "short"
    if not closed or _holds_another_item(item, own):
        return "overrun"
    return "ok"


def _slice_item_ours_2(text: str, form: str = "10-K") -> tuple[str | None, str]:
    """``(item_text, quality)``: ok / short / overrun / pointer / crossref / missing. **Never ``capped``**.

    The five fixes of #755, each named after the filing it was measured on:

    - **VFF's "ITE M 1A"** -- ``item`` is spelled with :func:`_spaced` like every other heading word, so the
      stop heading is found and the slice ends where Item 1 ends.
    - **VFF's label** -- a slice that holds another item's own title line is ``overrun``, whether a heading
      closed it or the document ran out. ``ours-1`` could say so only when no stop matched at all, and otherwise
      labelled such a slice by its length: ``capped`` on the 111,000-character filing the census recorded, and
      ``ok`` on one that stays under the cap. Neither reads as "this is the wrong section", which it is.
    - **PCVX's "Item 2.01"** -- an item number followed immediately by a digit is a different item
      (:data:`_NOT_SUBITEM`). The filing's exhibit list carries 8-K item titles, and "Item 2.01 Completion of
      Acquisition" was ``ours-1``'s end of Item 1: 5,305 characters of 10,599.
    - **the pointer items** -- a body that is a cross-reference is ``pointer`` and not ``short``: there is no
      prose to find there, so a later run should not keep looking for it.
    - **PCVX and GLUE's long Item 1** -- nothing is capped, because this version serves a layer that stores a
      section whole and lets its reader cap (165 such reports in the census).
    And from the 13 Nov 2026 check on 383 real 10-Ks, where three wrong cuts still said ``ok``: a
    mention through the item's own part ("in Part II.\nItem 8.", Cal-Maine; :data:`_REF_BEFORE_2`), a mention on
    the pattern path (Cheniere, AES, U.S. GoldMining; :func:`_words_before_ref`), "Item 1 A." with its letter set
    apart (Tempus AI; :data:`_LINE_END_2`) and "Items 1. and 2." as a line start (Cheniere).
    """
    text = text or ""
    if not text.strip():
        return None, "missing"

    key = "20-F" if str(form).upper().startswith("20") else "10-K"
    own = "1" if key == "10-K" else "4"

    line = _line_item_ours_2(text, key)
    if line is not None:
        start, end, closed = line
        return text[start:end].strip(), _quality_ours_2(text[start:end].strip(), own, closed, text)

    # The pattern path, for a document whose headings do not start their own lines (a filing served as one long
    # paragraph). The same starts and stops as ``ours-1``, spelled so that ``item`` may be split, and with no
    # cap: an unstopped start runs to the end of the document and is ``overrun``. The sub-item guard is not
    # here -- these patterns require a title after the number, and :data:`STOP_PATTERNS_2`'s 20-F branch, which
    # does not, is the one place a sub-item can still end a section (see that pattern's own note).
    start_re = re.compile(START_PATTERNS_2[key], re.I)
    stop_re = re.compile(STOP_PATTERNS_2[key], re.I)

    best: str | None = None
    closed = False
    for start_m in start_re.finditer(text):
        # Nor a start: with mention stops skipped, a start at a quoted mention ("“Item 4. Information on the
        # Company,” “Item 5. ...”") no longer ends at once and is passed over; it runs to the real stop and wins
        # (XTL, Frontline and Teekay 20-Fs, 13 Nov 2026 full-disk check).
        if _words_before_ref(text, start_m, key):
            continue
        # A mention is no stop here either: Cheniere's "described in Item 1A. Risk Factors ." and U.S.
        # GoldMining's 'See\n“ Item 2. Properties” for further information' each ended Item 1 at a few
        # thousand characters (13 Nov 2026 real-filing check).
        # Only the words before it: in a document served as one line the quote half of :func:`_ref_ours_2` reads
        # the rest of the filing as the stop's line, and calls a quoted heading a citation (Fable review).
        stop_m = next((s for s in stop_re.finditer(text, start_m.end()) if not _words_before_ref(text, s, key)), None)
        item = text[start_m.end():stop_m.start() if stop_m else len(text)].strip()
        if len(item) >= MIN_ITEM_CHARS:
            best, closed = item, stop_m is not None
            break
        if best is None or len(item) > len(best):
            best, closed = item, stop_m is not None
    if best is None:
        return None, ("crossref" if _INDEXED.search(text) else "missing")
    return best, _quality_ours_2(best, own, closed, text)


# ── suspect flags (#400 10-K package WP2) ───────────────────────────────────────────────────────────────────
# Measured in the 10-K reading report (§8, 8 Nov 2026) on 85 slices, 29 of them wrong: the loose threshold caught
# 28 with 17 false alarms. avo's decision D (8 Nov): the flags ride in the ``quality`` TEXT, "ok;suspect:S2,S4".
# Seven signals, S1-S6 and S8; there is no S7. S3 only ever acts together with S2, and S5 and S8 read the slice's
# place in the document and its length against the cap, so neither is defined for a cutter whose text is not a
# substring of the document (the ``sec_forms`` layer records which flags it computed; issue #757).
_SECTION = re.compile(r"(?im)^[^\S\n]*(competition|human\s+capital|employees|available\s+information)\b")
_TAIL_REF = re.compile(r"(?i)\b(see|refer\s+to|described\s+in)\b[^.!?\n]{0,60}$")
_START_REF = re.compile(r"(?i)\b(see|in|of)\b[^\S\n]*[“\"‘]?[^\S\n]*items?\b[^\n]*$")


def base(quality) -> str | None:
    """The label without its flags: ``ok;suspect:S4`` -> ``ok``."""
    return quality.split(";", 1)[0] if isinstance(quality, str) else quality


#: The signals ``ours-1``'s flagger raises -- all of them, which is what every caller of :func:`flag` has had.
SIGNALS_OURS_1 = ("S1", "S2", "S3", "S4", "S5", "S6", "S8")
#: And ``ours-2``'s (#835). Two of the seven are **not defined** for it, so they are not computed rather than
#: computed and ignored:
#:
#: - **S8** compares the slice against :data:`MAX_ITEM_CHARS`. ``ours-2`` caps nothing (:attr:`Rule.caps` is
#:   False), so the length means nothing there: it would flag every long section, right or wrong.
#: - **S6** asks whether the slice holds another item's heading. ``ours-2`` asks that in the **cut**
#:   (:func:`_holds_another_item` in :func:`_quality_ours_2`) and answers ``overrun``, so no ``ok`` slice of its
#:   can hold one and the signal is dead on arrival -- while ``ours-1``'s own reading of it (:data:`_LINE_END`,
#:   which takes a sub-item for a heading) is live and wrong, marking PCVX's correctly cut Item 1 suspect.
SIGNALS_OURS_2 = ("S1", "S2", "S3", "S4", "S5")


def flag(quality: str, text: str, item: str | None, form: str = "10-K", *,
         signals: tuple[str, ...] = SIGNALS_OURS_1) -> str:
    """``quality`` with ``;suspect:<signals>`` added when an ``ok`` slice of ``text`` still looks wrong.

    ``signals`` is the version's own set -- :data:`SIGNALS_OURS_1` by default, so every caller that had this
    function keeps the label it had. A caller that stores a row names its version instead and reaches this
    through :func:`cut_flag`, because which signals are defined is a property of the cut, not of the filing.
    """
    if quality != "ok":
        return quality
    item, text = item or "", text or ""
    key = "20-F" if str(form).upper().startswith("20") else "10-K"
    own = "4" if key == "20-F" else "1"
    at = text.find(item[:200]) if item else -1
    s = {
        "S1": len(item) < 2_000,
        "S2": len(text) > 150_000 and len(item) < 0.03 * len(text),
        "S3": key == "10-K" and not _SECTION.search(item),
        "S4": bool(_TAIL_REF.search(item[-220:].rstrip())),
        "S5": at > 0 and bool(_START_REF.search(text[max(0, at - 120):at].rstrip())),
        "S6": any(m.group(1).lower() != own and not _is_ref(item, m) for m in _LINE_END.finditer(item)),
        "S8": len(item) >= MAX_ITEM_CHARS,
    }
    s = {k: v for k, v in s.items() if k in signals}
    on = [k for k, v in s.items() if v]
    if any(s.get(k) for k in ("S1", "S4", "S5", "S6", "S8")) or (s.get("S2") and s.get("S3")):
        return "ok;suspect:" + ",".join(on)
    return quality


def strip_html(html: str) -> str:
    """Crude HTML -> text (scripts/styles dropped, entities unescaped)."""
    import html as _html

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# ── ours-2: the business section of an Annual Information Form (40-F), #756 ─────────────────────────────────
#
# The heading titles below are **adapted** from edgartools, not imported, which is the owner's decision of
# 12 Nov 2026 ("Evet bu sekilde uygula") for 40-F: 10-K and 20-F keep the pinned import, and here the start
# and stop titles are written out in our own code so that this rule's answers are ours to change and ours to
# test. The patterns around them are ours and are not in the source: the line anchor, the number in front, the
# contents test, the body minimum, and the three tests an inline title has to pass.
#
#     Source:    edgar/company_reports/forty_f.py, lists ``_BUSINESS_STARTS`` (:244), ``_BUSINESS_ENDS``
#                (:253) and ``_SECTION_PATTERNS`` (:266)
#     Project:   edgartools -- https://github.com/dgunning/edgartools
#     Version:   6.0.0 (479d350), read 12 Nov 2026
#     Licence:   MIT, from the upstream LICENSE.txt, reproduced whole below apart from the author's e-mail
#                address, which ``scripts/private_check.py`` refuses in this tree:
#
#     Copyright (c) 2022-present Dwight Gunning
#
#     Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
#     associated documentation files (the "Software"), to deal in the Software without restriction, including
#     without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#     copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
#     following conditions:
#
#     The above copyright notice and this permission notice shall be included in all copies or substantial
#     portions of the Software.
#
#     THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
#     LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
#     NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
#     WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
#     SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# **What is not adapted is the end of the section**, and that is the measurement's own conclusion
# (``notes/revenuechaser/notes/2026-11-12_1200_sec-40f-business-section.md``): where both cutters fire,
# edgartools' section is shorter every time, median four times shorter, because it ends at the next detected
# heading of any kind -- and six of its eighty-six cuts are under 1,000 characters, BTG's being 338 where
# ours takes the whole document. So the stops here are a list of real section titles, and a pair whose body is
# shorter than :data:`_AIF_MIN` is **rejected and the next start tried**, rather than returned short: a
# 338-character answer reads as a section that was found, and what it found is a contents line or a heading
# whose prose is printed somewhere else. (Round 1 of the review replaced that rejection with a distance a stop
# had to keep; round 2 measured what it cost -- a contents block with no page numbers, and a genuinely short
# section with its risk factors inside, both labelled ``ok`` -- and put the rejection back.)

#: The titles the NI 51-102 business block is written under. Line-anchored, because a title that shares its
#: line with prose is a sentence about the business and not the heading of it; the gaps inside a title are
#: :data:`_H_SP` and never ``\s``, so a title cannot reach over the line ending and swallow the page number or
#: the next heading (measured: ``BUSINESS OF THE COMPANY\n12`` took the page number as a title word).
#:
#: The one tail allowed after the title is "AND OPERATIONS", because PBA's heading is "DESCRIPTION OF PEMBINA'S
#: BUSINESS AND OPERATIONS" and the line anchor would otherwise lose it -- edgartools, searching anywhere in the
#: text, needs no such clause. Any other tail leaves the line a sentence, which is the point of the anchor.
_H_SP = r"[^\S\n]+"
#: What may stand in front of a title on its line: a number of up to three levels, with or without "item" in
#: front of it, and the separator the filer chose. Measured shapes: "6. DESCRIPTION OF THE BUSINESS",
#: "ITEM 4: RISK FACTORS" (EXK's own), "4 - BUSINESS OVERVIEW", "4.1.1 BUSINESS OVERVIEW". A roman numeral is
#: read only behind "item": IMG's AIF numbers its items "ITEM III: Description of the Business" (real filings,
#: 13 Nov), and a bare "I" or "V" would be read off ordinary text.
_AIF_NUMBER = (rf"(?:(?:item{_H_SP}[ivx]{{1,5}}\b|(?:item{_H_SP})?\d{{1,2}}(?:\.\d{{1,2}}){{0,2}})[.)]?"
               rf"(?:[^\S\n]*[:{_DASH}])?{_H_SP})?")
#: What may stand behind it: a colon, a full stop or a dash, and trailing blanks. One star of its own, because
#: two adjacent ``[^\S\n]*`` make a failing line quadratic (measured: 0.63 s on a line of 8,000 blanks, 9.2 s
#: on one of 32,000).
_AIF_TAIL = rf"[^\S\n]*(?:[:.{_DASH}][^\S\n]*)?$"
#: Added from the 225 stored 40-F documents (13 Nov): CGI's title wraps onto a second line after its possessive
#: (:data:`_H_WRAP`); "the ongoing business of the company" (DPRO), "general development and business of the
#: company" (ERO; DPRO's wraps after "the") and "our business" / "about our business" (PD, CCJ, EGO) are titles of their own; and the
#: words after "business of" begin with a letter, because SLF's contents line "Business of Sun Life 5" was
#: read as a title with its page number in it and returned the contents and the front matter as the section.
_H_WRAP = rf"(?:{_H_SP}|[^\S\n]*\n[^\S\n]*)"
_AIF_TITLES_2 = (
    rf"(?:narrative{_H_SP})?description{_H_SP}of{_H_SP}(?:the{_H_SP})?(?:\w[\w'’]*{_H_WRAP})?business(?:es)?",
    rf"(?:general{_H_SP}development{_H_SP}and{_H_SP})?business{_H_SP}of{_H_SP}"
    rf"(?:the{_H_SP})?[^\W\d_][\w'’]*(?:{_H_SP}[^\W\d_][\w'’]*){{0,3}}",
    # DPRO's "ThE\nONGOING BUSINESS OF THE COMPANY". Capital BUSINESS and nothing after the company, because
    # "The principal business of the Company is mining." is a sentence on a line of its own (round 2 review).
    rf"the{_H_WRAP}[^\W\d_]+{_H_SP}(?-i:BUSINESS){_H_SP}of{_H_SP}(?:the{_H_SP})?[^\W\d_][\w'’]*",
    # Already matched by the first pattern (its "the" and its possessive word are both optional), so removing
    # it changes no answer. Kept so that the titles read one-to-one against the list named above.
    rf"description{_H_SP}of{_H_SP}business",
    rf"business{_H_SP}operations",
    rf"business{_H_SP}overview",
    rf"description{_H_SP}of{_H_SP}operations",
)
_AIF_START_2 = re.compile(rf"(?im)^[^\S\n]*{_AIF_NUMBER}(?:{'|'.join(_AIF_TITLES_2)})"
                          rf"(?:{_H_SP}and{_H_SP}operations|{_H_SP}(?:general|overview|summary))?{_AIF_TAIL}")
#: "Our business" is read only where none of the titles above closes a section: it is CCJ's, PD's and EGO's
#: heading, and it is also HBM's sub-heading over its strategy, in front of its "DESCRIPTION OF OUR BUSINESS".
_AIF_TITLES_2B = (rf"(?:about{_H_SP})?our{_H_SP}business",)
_AIF_START_2B = re.compile(rf"(?im)^[^\S\n]*{_AIF_NUMBER}(?:{'|'.join(_AIF_TITLES_2B)}){_AIF_TAIL}")

#: The titles that end it: the parts of an Annual Information Form around the business block in NI 51-102's
#: own order. Two of them come **before** it -- general development of the business and the three-year history
#: are item 4 -- and they are stops because a filing that carries them prints them first and the description
#: after; the rest follow it. Also from the 225 stored 40-F documents (13 Nov): the numbered "4. ENTERPRISE
#: RISK FACTORS" (AQN), OBE's "CAPITALIZATION OF OBSIDIAN ENERGY", "Capital Structure and Market for Shares"
#: and "Dividend Policy" (EFLVF), "CAPITAL STRUCTURE OF PEMBINA" (PBA), the risk headings that do not say
#: "factors" (EQX, CCJ; GIB's "Forward-Looking Information and Risks and Uncertainties"), and DPRO's "RISK" over
#: "FACTORS" and CG's "Directors" over "and Officers" on two lines -- each of which let a section run on through
#: the parts behind it. ``DIVIDENDS`` is item 5 and is in the source's own ``_SECTION_PATTERNS`` though
#: not in its ``_BUSINESS_ENDS``; the two wordings with a tail (regulatory actions, of the company) are the real
#: titles of items 12 and 10, which the line anchor would otherwise lose.
#:
#: ``DIVIDENDS`` is the one title of a single word, and that is why it is a **line** stop only (see
#: :data:`_AIF_STOP_INLINE_2`). A line that holds nothing but the word -- a table's row label among its
#: figures -- still ends the section, which #763's hand-checked set is where it gets judged.
#: The gap in front of an optional tail is a plain blank: ``_H_SP`` also crosses ``\r`` and a form feed, and the
#: inline search would then read "CAPITAL STRUCTURE" plus the next line's first words as one title in mixed
#: case and refuse it -- round 8's ``\r`` shapes, which the first run of these tails failed in CI.
_H_TAIL_SP = r"[ \t\u00a0]+"
_AIF_STOP_TITLES_2 = (
    rf"general{_H_SP}development{_H_SP}of{_H_SP}(?:the{_H_SP})?(?:[\w\-][\w\-'’]*{_H_SP})?business",
    rf"(?:three|3)(?:[^\S\n]|-)+year{_H_SP}history",
    rf"(?:description{_H_SP}of{_H_SP}(?:the{_H_SP})?)?capital{_H_SP}structure"
    rf"(?:{_H_TAIL_SP}and{_H_SP}market{_H_SP}for{_H_SP}(?:securities|shares)|{_H_TAIL_SP}of{_H_SP}[^\W\d_][\w'’]*)?",
    rf"capitali[sz]ation{_H_TAIL_SP}of{_H_SP}[^\W\d_][\w'’]*",
    rf"material{_H_SP}properties",
    rf"legal{_H_SP}(?:proceedings|matters)(?:{_H_SP}and{_H_SP}regulatory{_H_SP}actions)?",
    rf"(?:enterprise{_H_SP})?risk{_H_WRAP}factors",
    rf"risks{_H_SP}(?:related{_H_SP}to|relating{_H_SP}to|in|that{_H_SP}can{_H_SP}affect){_H_SP}(?:the|our){_H_SP}business",
    rf"forward(?:[{_DASH}]|{_H_SP})looking{_H_SP}(?:information|statements){_H_SP}and{_H_SP}risks"
    rf"(?:{_H_SP}and{_H_SP}uncertainties)?",
    rf"code{_H_SP}of{_H_SP}business{_H_SP}conduct",
    rf"market{_H_SP}for{_H_SP}securities",
    rf"dividends(?:{_H_SP}and{_H_SP}distributions|{_H_TAIL_SP}policy)?",
    rf"dividend{_H_TAIL_SP}policy",
    rf"directors{_H_WRAP}and{_H_SP}(?:executive{_H_SP}officers|officers|executive)"
    rf"(?:{_H_SP}of{_H_SP}the{_H_SP}(?:company|corporation))?",
)
_AIF_SUBHEAD = re.compile(r"(?i)\bcode\s+of\s+business\s+conduct\b|\byear\s+history\b")
_AIF_STOP_2 = re.compile(rf"(?im)^[^\S\n]*{_AIF_NUMBER}(?:{'|'.join(_AIF_STOP_TITLES_2)}){_AIF_TAIL}")
#: The same titles wherever they appear, for a filing whose headings do not begin their own lines: five of the
#: measurement's misses are a start with no line-start stop after it. **Without the single-word title**, because
#: "Dividends received from subsidiaries fund the parent" is a sentence and nothing inside a line can tell it
#: from a heading; the multi-word titles are tested by :func:`_printed_as_a_heading` and :func:`_runs_into_prose`
#: instead, and a one-word title would pass both.
_AIF_STOP_INLINE_2 = re.compile(rf"(?i)\b(?:{'|'.join(t for t in _AIF_STOP_TITLES_2 if 'dividends' not in t)})\b")

#: Any AIF heading at all, start or stop, on a line of its own: what a contents block is made of. Used by
#: :func:`_aif_contents_entry` and by nothing else.
_AIF_HEADING_LINE = re.compile(rf"(?im)^[^\S\n]*{_AIF_NUMBER}"
                               rf"(?:{'|'.join(_AIF_TITLES_2 + _AIF_TITLES_2B + _AIF_STOP_TITLES_2)})"
                               rf"(?:{_H_SP}and{_H_SP}operations)?{_AIF_TAIL}")

#: ``ours-1``'s only start title, kept as a **last resort** and asked exactly what ``ours-1`` asks of it: its
#: own stop (a line-start ``RISK FACTORS``), ``ours-1``'s page-number test, ``ours-1``'s distance from the
#: heading and -- the part round 3 had to put back -- the start **dropped** when that first stop is too close,
#: rather than a later stop looked for. No reference test, and no body minimum on a closed pair, because
#: ``ours-1`` applies neither; an unclosed body still has to pass the minimum, or a contents line at the end of
#: a document comes back as an empty string labelled ``overrun`` (round 3's fourth finding).
#:
#: Neither more nor less than ``ours-1``, which is a position both directions were measured into: round 1 lost
#: three shapes by asking more, and round 3 returned a contents block by asking less. :func:`_aif_pass_ours_1`
#: is where that rule lives.
#:
#: The titles above are preferred because a filing that carries both prints this one first and the description
#: after it, and the description is the business block proper; but 44 of the 88 stored exhibits are cut by
#: ``ours-1`` today from this heading, so a version that answered nothing there would be a loss nobody
#: measured. It is a start here while it stays the first stop above.
#:
#: It is ``ours-1``'s own compiled pattern and not a copy, which is the one place this module **reads** an
#: ``ours-1`` object instead of adding its own: a copy could drift from it word by word, and the whole point is
#: that the two read the same heading. ``ours-1``'s pattern is frozen (the golden file pins it), so the
#: coupling only runs one way.
_AIF_START_OURS_1_TITLE = _AIF_START

#: A body shorter than this is not the business block: it is a contents entry or a cross-reference that got
#: through, which is what the measurement found behind 7 of its 40 misses. ``ours-1``'s :data:`_BODY_MIN`, for
#: the same reason. The pair is rejected and the next start tried.
_AIF_MIN = _BODY_MIN

def _printed_as_a_heading(s: str) -> bool:
    """True when ``s`` is printed in capitals, which is how a run-in heading is printed in these exhibits.

    This is the inline search's own test and it is deliberately the strictest of the three rules this module
    has tried, because round 3 measured what the looser ones cost. Round 1's was "no lower-case word in it",
    which refused real title-case headings; round 2's allowed a lower-case connective, and once the inline
    search was in play for every document (round 2's second finding) that accepted ordinary AIF prose --
    "The Board has adopted a Code of Business Conduct. The Code applies to all employees", "See Description of
    the Business - Risk Factors - Commodity Prices", "The Company maintains Directors and Officers Liability
    Insurance". Each of those ended a section early or, inside the first 4,000 characters, lost it altogether.

    **The cost, written down rather than guessed at:** a filer who prints a run-in heading in title case is not
    read, and the section then runs to its next line-start stop or to the end of the document. The measurement
    quotes one run-in heading, EXK's ``9 ITEM 4: RISK FACTORS``, and it is in capitals; a title-case run-in
    heading is a shape nobody has counted, so it goes to #763's hand-checked set and not into this test.
    """
    return s == s.upper() and any(c.isalpha() for c in s)


#: What may stand between an inline title and the first letter of its section: the filer's punctuation, a page
#: number, and the line ending. Bounded and with one star over one class, because two adjacent
#: ``[^\S\n]*`` make a failing input quadratic -- the same defect round 1 removed from :data:`_AIF_TAIL` and
#: round 3 found again here (12.4 s on 32,000 blanks after the title).
_H_AFTER = re.compile(rf"[\s.:{_DASH}\d]{{0,40}}([^\W\d_])")

#: What ends a line. :data:`_H_AFTER` crosses a line ending with ``\s``, so the test for "the prose is on the
#: next line" has to read the same set: round 5 found it asking for ``"\n"`` alone, which let a form feed or a
#: carriage return put the prose on another line while the introduction test was skipped
#: ("We now turn to our RISK FACTORS" ended a section at 27,019 characters behind a ``\x0c``).
#: ``strip_html`` does not remove either character.
_VERTICAL_CHARS = "\n\r\v\f\x1c\x1d\x1e\u0085\u2028\u2029"
_VERTICAL = re.compile("[" + re.escape(_VERTICAL_CHARS) + "]")


def _runs_into_prose(text: str, end: int) -> bool:
    """True when what follows an inline title reads like the start of its section's first sentence.

    A run-in heading is followed by prose that begins with a capital ("RISK FACTORS The Company's business is
    risky"); a sentence that merely names the section goes on in lower case ("the Risk Factors are described
    below", "Legal Proceedings are described in note 14"). That is the second half of the inline test, and it
    is what the first half cannot do on its own.

    **The prose may begin on the next line.** ``strip_html`` turns ``<b>ITEM 4: RISK FACTORS</b><br>The
    Company...`` into a title at the end of its line and the prose at the start of the next, which is EXK's own
    shape in half of the ways a filer can mark it up; round 3 measured the same-line-only version returning
    63,100 characters with every risk factor inside.
    """
    m = _H_AFTER.match(text, end)
    return m is not None and m.group(1) == m.group(1).upper()


#: What may stand between a run-in heading and the start of its line: the page number the filer prints there
#: and the item number in front of the title, which is EXK's ``9 ITEM 4:``. One of the two has to be there,
#: because whitespace alone leaves a title at the end of an English sentence looking like a heading.
#:
#: A page number may stand in front of the item number, as EXK's ``9 ITEM 4:`` does, but only there: left
#: optional on its own it read the year out of "Production fell in 2025. 12" and left the sentence in front of
#: it (round 4).
#:
#: **Read by hand, not by a search over the whole head.** Rounds 4 and 5 each found this pattern quadratic --
#: 21.9 s on a run of 32,000 blanks, then 15.1 s on a run of 32,000 digits -- because a ``search`` for an
#: end-anchored pattern restarts at every position of the run in front of it. A ``\Z`` anchor and then a
#: lookbehind each fixed one shape and left the other, so round 6 stopped patching the pattern and reads the
#: trailing figure off the end of the line with :meth:`str.rstrip`, which walks the run once. Round 6 also
#: measured what the lookbehind had cost in answers: ``-12-``, an ordinary printed page number, and
#: ``completed.12`` were both refused.
#:
#: The parts, in the order they are peeled off the end of the line: the mark a filer puts after a number
#: (:data:`_H_NUMBER_MARK`), the figure itself (:data:`_H_FIGURE_CHARS`, digits and dashes -- not ``.`` or
#: ``:``, which belong to the sentence in front of it), and then the gap (:data:`_H_CLOSERS`).
_H_DIGITS = "0123456789"
_H_FIGURE_CHARS = _H_DIGITS + _DASH_CHARS
_H_NUMBER_MARK = ".:"

#: The punctuation a sentence ends on, as a compiled class. **Not a string tested with ``in``:** :data:`_DASH`
#: is a regex class fragment, so ``".:;!?" + _DASH`` is the characters ``. : ; ! ? \ - u 0 1 2 5`` -- it
#: accepts the letter ``u`` and four digits and refuses a real en or em dash. Round 5 measured what that cost:
#: "had mines in Peru 2 MATERIAL PROPERTIES" ended a section where "in Chile" ran on, and
#: "...completed <em dash> 9 ITEM 4:" lost its heading where an ASCII hyphen kept it.
_H_SENTENCE_END = re.compile(rf"[.:;!?{_DASH}]\Z")

#: The marks that close a quotation or a bracket, which do not un-end the sentence they follow. Whitespace is
#: **not** in this string and is stripped separately, because round 5 wrote ``rstrip(_H_CLOSERS)`` where
#: ``rstrip()`` had been and so refused every gap made of an em space, a thin space or a no-break space --
#: ``strip_html`` collapses runs of ``[ \t]`` and leaves those alone (round 6).
_H_CLOSERS = "\"'\u2019\u201d\u00bb)]}"

#: The whitespace :func:`_rstrip_gap` strips, spelled out because :meth:`str.rstrip` takes characters and not a
#: class. The horizontal spaces ``strip_html`` does **not** collapse are the point of the list: it folds runs of
#: ``[ \t]`` and of ``&nbsp;``, measured, but leaves an em, en or thin space standing (round 7 corrected round
#: 6's claim that a no-break space survives it).
#:
#: **Spelling a list out is how a list loses a member**, which round 8 measured: round 6's bare
#: :meth:`str.rstrip` stripped ``\x1f``, Python counting the four information separators as whitespace, and
#: round 7's hand-written list did not, so ``completed.\x1f9 RISK FACTORS`` went from a heading at 27,013
#: characters to no heading at 54,026. The other three (``\x1c``, ``\x1d``, ``\x1e``) are in
#: :data:`_VERTICAL_CHARS` instead, because they end a line rather than stand inside one.
#: ``test_the_whitespace_this_module_spells_out_is_all_of_python_s`` compares the two spellings, which is the
#: third time this module has needed such a test and the second time one was missing when it was needed.
_H_WHITESPACE = " \t\x1f\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"

#: The word that introduces an item number. A **letter** in front of it disqualifies it, so that "subitem" is
#: not read as an item number (round 6); a digit does not, because a page number is printed tight against the
#: heading often enough -- round 7 measured ``completed.9ITEM 4:`` lost to a plain ``\b``, which treats the
#: figure and the word as one word.
_H_ITEM = re.compile(r"(?<![^\W\d_])item\Z", re.IGNORECASE)

#: The words that make the sentence in front of an item number a cross-reference. :data:`_REF_BEFORE` is
#: ``ours-1``'s and anchored tight against the match, which is right where it is used -- the reference word
#: sits directly in front of the title. In front of an **item number** it does not: "see also Item 4",
#: "pursuant to Item 4", "as described in this Item 4" and "refer to our Item 4" each put a word of filler
#: between the two, and round 8 measured every one of them ending a section at 27,0xx where round 6 ran on to
#: 54,0xx. So this is the same question asked over a short run of filler words, and it is its own pattern
#: because ``_REF_BEFORE`` is read by ``ours-1``'s :func:`_is_ref` and may not move.
#:
#: The filler list is closed on purpose: a determiner, a possessive, "also", and the words a filing uses to
#: point at a place in itself. **"line" is not in it**, which is what keeps the cost row
#: "the line item 12" a heading -- and that remains a cost, not a result, until #763's set counts it.
_REF_BEFORE_ITEM = re.compile(r"(?i)\b(?:see|in|under|refer(?:red)?[^\S\n]+to|pursuant[^\S\n]+to"
                              r"|described[^\S\n]+in|set[^\S\n]+out[^\S\n]+in|herein)"
                              r"(?:[^\S\n]+(?:also|the|this|that|these|those|our|its|their|such"
                              r"|above|below|section|part|item)){0,4}"
                              r"[^\S\n]*[\u201c\"\u2018]?[^\S\n]*$")


def _rstrip_gap(t: str) -> str:
    """``t`` without the whitespace and the closing marks at its end, in either order and however mixed.

    One :meth:`str.rstrip` over both sets rather than a loop over two: round 6 wrote the loop, and round 7
    measured it quadratic on a gap of alternating ``")`` and a blank (0.23 s on 128,000, against 0.02 s).
    :data:`_H_WHITESPACE` is spelled out because ``rstrip`` takes characters and not a class.
    """
    return t.rstrip(_H_WHITESPACE + _H_CLOSERS)


def _figure_at_the_end(bare: str) -> tuple[int, int]:
    """``(where the trailing page-or-item number begins, where it ends)`` in the line ``bare``.

    One backward walk, so a figure of any length costs its own length and no more. It reads, from the end:
    the mark the filer puts after a number, then the figure, then -- while a mark stands between two figures --
    that mark and the figure before it, because ``2.5`` and ``12:30`` and ``Item 4.1`` are **one** number each.

    **What "two figures" asks is a digit in front of the mark and a figure character behind it**, not a digit on
    both sides: a printed page number takes a dash as readily as a digit (``-12-`` is one, measured in round 6),
    so ``4.-`` is one figure too. Round 8 read the older wording as a claim about digits and reported the dash
    as a defect; the wording was wrong and the behaviour is the measured one. The digit it does ask for is an
    **ASCII** digit, which round 8 found it was not: :meth:`str.isdigit` is true of ``\u00b2``, so
    "about 5 km\u00b2.12" joined into one figure and the heading behind it was lost -- 54,061 characters where
    round 6 gives 27,015.

    **Round 7's first finding, and a regression round 6 introduced.** Round 6 took only the digits after the
    last mark, so what was left ended in ``.`` or ``:``, :func:`_H_SENTENCE_END` accepted that as a sentence's
    end, and nothing in front of the number was ever asked about. Round 4's own defect was back for every
    decimal: "the Company had 2" ran on to 54,053 characters, as it should, while "had 2.5" cut at 27,020, and
    so did "an interest of 49.9", "section 5.2", "at 12:30", "$1.5" and "the ratio was 1:2". It also defeated
    round 6's own third fix, since "as discussed under Item 4.1" cut where "under Item 4" does not.

    The cost it buys: a page number glued to a sentence that ends in a figure -- "in 2020.12" -- is read as one
    number and refused, which is the same answer the other figure-before-figure shapes get.
    """
    i = len(bare)
    while i and bare[i - 1] in _H_NUMBER_MARK:
        i -= 1
    end = i
    while True:
        j = i
        while j and bare[j - 1] in _H_FIGURE_CHARS:
            j -= 1
        if j == i:
            return i, end
        i = j
        if not (i >= 2 and bare[i - 1] in _H_NUMBER_MARK and bare[i - 2] in _H_DIGITS):
            return i, end
        i -= 1


def _line_start(text: str, at: int) -> int:
    """Where the line holding ``at`` begins, reading **every** line ending :data:`_VERTICAL` reads.

    Round 6's first finding: :data:`_VERTICAL` made "the prose is on the next line" true of a form feed and a
    carriage return, and this was still ``text.rfind("\n", 0, at)``. A heading alone on its own line in a
    document whose lines end in ``\r`` was then measured against the **previous** line, refused, and the
    section ran to the end of the document -- 54,088 characters labelled ``overrun`` where ``\n`` gives 27,032
    of body. It also made the rule quadratic again, because every candidate sliced back to the last ``\n``:
    1,500 headings in a document with no ``\n`` in it cost 10.2 s where ``\n`` costs 0.02 s.

    **Round 7's third finding: this is the slow way to ask.** Nine or ten of those characters are absent from a
    real document, and :meth:`str.rfind` for an absent character walks the whole prefix, so every candidate cost
    about nine times its own offset -- 1.04 s on 16,000 headings in an ordinary ``\n`` document against 0.04 s
    before, and four times the input for ten to seventeen times the time. The quadratic had moved from ``\r``
    documents to all of them rather than going away.

    So the callers do not call this per candidate: :func:`_aif_ends` collects the line starts once with
    :data:`_VERTICAL` and walks them with a forward-only cursor, the same way it walks the ends themselves. This
    is kept for the one-off question and for the tests of it.
    """
    return max(text.rfind(ch, 0, at) for ch in _VERTICAL_CHARS) + 1


def _introduced_as_a_heading(text: str, start: int, line_start: int | None = None) -> bool:
    """True when a run-in heading at ``start`` is introduced the way one is.

    This is asked only of a title whose prose begins on the **next** line, where the title's own line carries
    no evidence after it. The line is read from its end backwards: the mark after a number, the figure, the gap.

    - **Its line's start**, or nothing but whitespace before it.
    - **An item number** -- EXK's ``No significant acquisitions were completed. 9 ITEM 4: RISK FACTORS``. The
      word "item" is strong evidence and the one thing that can take it away is the module's own test for a
      mention, :data:`_REF_BEFORE`: "see Item 4:" and "as discussed under Item 4" are cross-references and end
      no section, which is round 6's third finding. Round 6 asked instead that something *introduce* the item
      number, and round 7 measured what that refused -- ``(the "Acquisition") ITEM 4:``, a running footer's
      ``Annual Information Form ITEM 4:`` and a list item's ``\u2022 gold and silver ITEM 4:``, each a real
      shape and each losing 27,0xx characters of body to 54,0xx. The reference test is the same question this
      module asks everywhere else, so it is the one asked here.

      Its own cost, stated: "the line item 12" carries no reference word and is read as an item number. That is
      a noun phrase nobody has counted in front of a stop title, and it goes to #763 rather than to a sixth
      rule read off a built string.
    - **A bare page number**, and then what stands in front of it has to be nothing or a sentence's punctuation.
      Without that, any sentence ending in a figure introduced a heading -- "the Company had 2 MATERIAL
      PROPERTIES" ended a section at 27,030 characters where the same sentence written "two" ran on to 54,073
      (round 4).

    **The costs, written down rather than argued away.** A bare figure introduced by another figure is refused,
    whichever it is: a running footer's page count, a table row's last cell, a footnote mark ("completed.1 12"),
    and a year ("fell in 2020 12") all give the same answer, because nothing in the text separates them from
    "had 2 MATERIAL PROPERTIES". Round 6 chose one answer for all four rather than a fifth rule read off built
    strings; which answer is right is a count over real filings and belongs to #763's hand-checked set.
    """
    line_start = _line_start(text, start) if line_start is None else line_start
    bare = text[line_start:start].rstrip()
    if not bare:
        return True
    at, end = _figure_at_the_end(bare)
    if not any(ch.isdigit() for ch in bare[at:end]):     # no page or item number at the end of the line at all
        return False
    before = _rstrip_gap(bare[:at])
    if _H_ITEM.search(before):
        return not _REF_BEFORE_ITEM.search(_rstrip_gap(before[:-len("item")])[-80:])
    return not before or _H_SENTENCE_END.search(before) is not None


def _aif_contents_entry(text: str, m: re.Match) -> bool:
    """True when the heading ``m`` is one line of a contents block rather than the heading of a section.

    What a contents block looks like without page numbers -- which :data:`_PAGE_NO` is the test for -- is
    headings on **consecutive lines** with no prose between them. So the test is the neighbouring non-blank
    line: another AIF heading above or below it, and this is a list of sections and not one of them.

    Round 2 measured why it is needed: once a stop closer than the minimum stopped rejecting a start, a
    contents block with no page numbers returned the rest of the contents and the front matter behind it,
    6,490 characters labelled ``ok``, where the body is 26,999. The item rule has :data:`_TOC_NEXT` for the
    same job; this is the AIF rule's own, because its headings are titles and not numbers.

    The price is a filer who prints two headings back to back with the body under the second. That is the same
    shape as a two-line contents block and nothing in the text tells them apart, so it goes to #763's
    hand-checked set rather than to a guess here.
    """
    for window, forwards in ((text[m.end():m.end() + 300], True), (text[max(0, m.start() - 300):m.start()], False)):
        lines = [l for l in window.split("\n") if l.strip()]
        line = (lines[0] if forwards else lines[-1]) if lines else None
        if line is not None and _AIF_HEADING_LINE.fullmatch(line) and not _AIF_START_2B.fullmatch(line):
            # "Our Business" under a title is its first sub-heading (BN), not a neighbour in a list. Two business titles one above the other are a page header over the heading -- OR's
            # "15 DESCRIPTION OF BUSINESS" over "Description of the Business" -- and not a list of sections: the
            # upper one is skipped (True) and the lower one kept, which a contents block never gives, since it
            # lists the parts around the business block too.
            return forwards or not _AIF_START_2.fullmatch(line)
    return False


def _inline_prose_follows(text: str, m, line_start: int) -> bool:
    """True when the prose of the section ``m`` names begins where a run-in heading's prose would begin.

    Two shapes count, and the difference between them is measured and not assumed.

    - **On the same line**, which is EXK's ``9 ITEM 4: RISK FACTORS The Company is risky.``
    - **On the next line**, which is what :func:`strip_html` makes of
      ``<b>ITEM 4: RISK FACTORS</b><br>The Company...`` -- but then the title also has to
      be introduced the way a heading is (:func:`_introduced_as_a_heading`), because a title at the end of a
      prose line whose next line opens with a capital is a sentence that happens to end on the section's name
      ("We now turn to our RISK FACTORS"). Round 3 asked only for the capital and measured that sentence
      ending a 27,019-character body.
    """
    if not _runs_into_prose(text, m.end()):
        return False
    gap = text[m.end():_H_AFTER.match(text, m.end()).end()]
    return not _VERTICAL.search(gap) or _introduced_as_a_heading(text, m.start(), line_start)


def _aif_inline_ref(text: str, m: re.Match, line_start: int, line_end: int) -> bool:
    """:func:`_inline_ref` with the AIF rule's own idea of where a line begins.

    **Round 7's second finding**, and round 6's first finding one function further along the same ``and`` chain.
    :func:`_inline_ref` keeps a heading that opens its own line, and it asks :func:`_starts_mid_line`, which is
    still ``text.rfind("\n", ...)``. In a document whose lines end in ``\r`` or ``\x0c``, a heading alone on
    its line after a line ending in "see" was therefore read as mid-line, refused as a reference, and the
    section ran to the end of the document: 54,090 characters labelled ``overrun`` where ``\n`` gives 27,034.

    :func:`_starts_mid_line` itself is **not** changed, because ``ours-2``'s item rule shares it (#755) and that
    rule's answers are not this round's business. This is the AIF rule's own test, and it takes the line start
    the caller already has.

    **Round 8's first finding: that fixed half of it and handed the other half straight back.** For a heading
    that does share its line, this calls :func:`_ref_ours_2`, and two things in there read lines with ``\n``
    alone: :data:`_CITED_PHRASE`'s ``[^\\S\\n]`` crosses every other line ending, and the 200-character quote
    window is cut at ``text.find("\n", ...)``, which returns -1. So in a ``\r``-delimited document a run-in
    heading whose **next line** opens "In this", "Below", "In our" or "Above" was read as "RISK FACTORS below"
    -- a citation -- and the section ran to the end of the document: 54,125 characters labelled ``overrun``
    where ``\n`` gives 27,042, and 54,086 against 26,999 where the heading carries only a page number.

    The fix is to hand :func:`_ref_ours_2` the text **cut at the end of this heading's own line**, which the
    caller has from the same cursor. Every offset in front of the match is unchanged, so ``m`` stays valid;
    ``find("\n", ...)`` then returns -1 and the window ends where the line ends, which is what it meant all
    along. :func:`_ref_ours_2` is not touched, because ``ours-2``'s item rule reads it (#755).
    """
    if not text[line_start:m.start()].strip():
        return False
    return _ref_ours_2(text[:line_end], m)


def _aif_ends(text: str) -> list[int]:
    """Every position in ``text`` where a section of an Annual Information Form could end, in order.

    Computed **once per document** and then scanned by position (the list is tens of entries long, so a walk
    costs nothing and a new import would have to pass the module's own import guard), because the rule tries
    one start after another and each start would otherwise re-scan the whole text: round 2 measured 5.8 s on a megabyte with 80,000
    lower-case mentions in it, where one scan costs 0.02 s.

    Two kinds, and the earlier one wins wherever both are found -- the line-start search is not consulted
    first, which round 2 measured returning every risk factor when a filing's run-in heading was followed by a
    numbered line stop further on. A line-start stop has to pass the reference test; an inline one has to pass
    it, be printed as a heading, and run into prose.

    **Where both branches reach the same offset the inline one decides** (round 4). A title in capitals, alone
    on its line, with a capital on the next line is found by both; :func:`_ref_ours_2` refuses it after "see"
    and :func:`_inline_ref` keeps it, because a heading that opens its own line is kept even after a reference
    word -- which is ``ours-2``'s item rule's own measured correction and ``ours-1``'s behaviour in its pattern
    rule. So the reference test on the line branch decides where the inline branch does not match the same
    offset, and round 5 counted three such places rather than the two this said before:

    - a stop the inline search does not read at all (``dividends``);
    - a stop whose next line opens in lower case, which fails :func:`_runs_into_prose`;
    - a stop in **title case**, which fails :func:`_printed_as_a_heading` -- the third, and the one the earlier
      wording missed.

    A **numbered** stop reaches two different offsets: the line stop at the number is refused as a reference and
    the inline stop at the title is taken, so the body ends on a dangling ``12.`` or ``ITEM 12:``. That is the
    same in every version of this rule and is a wart for #763's set, not something another built string settles.
    """
    # Two stop titles are also sub-headings inside the business block, and there the filer prints them in title
    # case: CNR's "Code of Business Conduct" (it lost 83% of its item 4) and MX's "Three-Year History" (80%). In
    # capitals they are parts of their own -- BN's code of conduct, HBM's history -- and still end it.
    out = [e.start() for e in _AIF_STOP_2.finditer(text) if not _ref_ours_2(text, e)
           and not (_AIF_SUBHEAD.search(e.group(0)) and not _printed_as_a_heading(e.group(0)))]
    breaks = list(_VERTICAL.finditer(text))
    starts = [0] + [v.end() for v in breaks]       # where each line begins
    ends = [v.start() for v in breaks]             # and where the line before it ends
    seen = 0
    for e in _AIF_STOP_INLINE_2.finditer(text):
        while seen + 1 < len(starts) and starts[seen + 1] <= e.start():
            seen += 1                    # matches arrive in document order, so the cursor only moves forward
        line_start = starts[seen]
        line_end = ends[seen] if seen < len(ends) else len(text)
        if (_printed_as_a_heading(e.group(0)) and _inline_prose_follows(text, e, line_start)
                and not _aif_inline_ref(text, e, line_start, line_end)):
            out.append(e.start())
    return sorted(out)


def _aif_business_ours_2_q(text: str) -> tuple[str | None, str]:
    """``(business section, quality)`` of an Annual Information Form in ``text``: ok / short / overrun / missing.

    Two passes, and the first one that closes a pair answers.

    **This version's own titles.** A line-start title from :data:`_AIF_START_2` that is neither a contents
    entry (:func:`_aif_contents_entry`, :data:`_PAGE_NO`) nor a cross-reference (:func:`_ref_ours_2`), then the
    first end from :func:`_aif_ends` after it. A pair whose body is shorter than :data:`_AIF_MIN` is
    **rejected** and the next start tried, because a 338-character answer reads as a section that was found and
    is in fact a contents line or a heading whose prose is printed elsewhere -- which is how edgartools'
    shortest cuts arise, and what the measurement found behind 7 of its 40 misses.

    **Then ``ours-1``'s own title** (:func:`_aif_pass_ours_1`), which is ``ours-1``'s rule exactly, so that
    this version answers a section wherever ``ours-1`` does -- 44 of the 88 stored exhibits are cut from that
    heading today. Rounds 1 and 3 each measured a shape that a last resort of our own devising got wrong where
    ``ours-1`` is right, in both directions: asking more than ``ours-1`` lost three shapes, and asking less
    (looking past a near stop instead of dropping the start, as ``ours-1`` does) returned a contents block.
    So it asks neither more nor less.

    **A closed pair always wins**, and among unclosed ones the earliest start does. Every start is tried before
    an unclosed one is returned, because an appendix at the end of the document can carry a heading of its own
    and its tail is not the business section while a real, closed block stands above it (round 1 measured 5,999
    characters of appendix returned in place of 26,999 of body; round 2 the same with both pairs unclosed).

    A start with no end at all keeps its body, which then runs to the end of the document, and the label is
    ``overrun`` -- and such a body has to pass the minimum too, or a contents line at the end of a document
    comes back as an empty string labelled ``overrun`` (round 3). Nothing is capped: five of the stored cuts sat
    at :data:`MAX_ITEM_CHARS`, which the ``sec_forms`` storage decision removes, so the length has to be told
    plainly instead of trimmed.

    Since the 225 stored 40-F documents (13 Nov): "our business" is a second tier of the first pass
    (:data:`_AIF_START_2B`), a run-in title behind a page footer is the very last resort
    (:func:`_aif_inline_start`), and a section ``ours-1``'s pass closes is cut at this version's first stop
    inside it. Round 2: an MD&A exhibit is no AIF (:func:`_aif_is_mda`); a stop whose part was printed before
    the business block is a sub-heading (:func:`_aif_unlisted_ends`); "development of the business" is read
    after it (:data:`_AIF_START_2C`); and ``short`` is the label of a numbered item under the minimum
    (:func:`_aif_short_item`) and of one whose own sub-items go on past its end (:func:`_aif_item_goes_on`).
    """
    text = text or ""
    if not _AIF.search(text):
        return None, "missing"
    ends = _aif_unlisted_ends(text, _aif_ends(text))
    # item 4's own three-year history is a part of it, not the end of it (TECK prints it in capitals)
    item_4 = [e for e in ends if "history" not in _aif_stop_key(text, e)]
    overrun: tuple[int, str] | None = None
    short: str | None = None
    for starts in (_AIF_START_2, _AIF_START_2B):
        # An MD&A is read no further than the AIF's own titles: GIL's carried only "3.0 OUR BUSINESS", and a
        # document holding both an MD&A and an AIF keeps the AIF's description (round 2 review).
        if starts is _AIF_START_2B and _aif_is_mda(text):
            return None, "missing"
        for m, end, body in _aif_pairs(text, ends, starts):
            if len(body) < _AIF_MIN:
                if short is None and _aif_short_item(text, m, end, body):
                    short = body
                continue
            if end is not None:
                return _aif_trim(body), ("short" if _aif_item_goes_on(text, m, end, ends)
                                         or _aif_ends_at_item_4_early(text, m, body, end, ends) else "ok")
            if overrun is None:  # starts arrive in document order, so the first unclosed one is the earliest;
                overrun = (m.start(), body)   # the comparison round 2 needed here was for one loop over two title
    closed, open_ = _aif_pass_ours_1(text)   # sets, and round 3 split those into two passes
    if closed is not None:
        # ``ours-1``'s one stop is RISK FACTORS, so its section can hold the parts this version stops at -- GFR's
        # holds its capital structure and dividends. ``ours-1``'s own answer is not touched; this version's is
        # cut at the first of its stops past the minimum, because a sub-heading straight under the heading is
        # the shape round 1 measured. Not material properties, which a mining filer describes inside its
        # business block (BTG).
        # ``ours-1``'s title is item 4, so its three-year history does not end it either; and a stop inside a
        # line has to be introduced the way a heading is, because "described under the heading RISK FACTORS. The"
        # cut a 27,000-character block at 6,038 (round 2 review) where ``ours-1`` answers the whole of it.
        at = text.find(closed)
        inside = next((e for e in item_4 if at + _AIF_MIN <= e < at + len(closed)
                       and "material" not in text[e:e + 40].lower()
                       and (_AIF_STOP_2.match(text, e) or _introduced_as_a_heading(text, e))), None)
        return _aif_trim(closed if inside is None else text[at:inside].strip()), "ok"
    for m, end, body in _aif_pairs(text, item_4, _AIF_START_2C):
        if end is not None and len(body) >= _AIF_MIN:
            return _aif_trim(body), "ok"
    if open_ is not None and (overrun is None or open_[0] < overrun[0]):
        overrun = open_
    if overrun is not None:
        return _aif_trim(overrun[1]), "overrun"
    found = _aif_inline_start(text, ends)
    if found[0] is None and short is not None:
        return _aif_trim(short), "short"
    return found


def _aif_pairs(text: str, ends: list[int], starts: re.Pattern):
    """Each usable start of ``starts`` with the first end after it: ``(match, end or None, body)``."""
    seen = 0                     # how far into ``ends`` the previous start looked; starts arrive in order, so
    for m in starts.finditer(text):                 # the cursor only ever moves forward (round 3 measured
        if _PAGE_NO.match(text, m.end()):           # 2.3 s on a document holding 8,000 of each)
            continue
        if _ref_ours_2(text, m) or _aif_contents_entry(text, m):
            continue
        while seen < len(ends) and ends[seen] < m.end():
            seen += 1
        end = ends[seen] if seen < len(ends) else None
        yield m, end, text[m.end():(len(text) if end is None else end)].strip()


#: "Development of the Business" without the "general" of ``ours-1``'s title is item 4's heading in some AIFs,
#: and TECK's business block -- operations, reserves, health and safety -- is printed under it with no other
#: title. Read only after every title above and ``ours-1``'s pass, so it moves no cut they make.
_AIF_START_2C = re.compile(rf"(?im)^[^\S\n]*{_AIF_NUMBER}development{_H_SP}of{_H_SP}(?:the|our){_H_SP}business"
                           rf"{_AIF_TAIL}")

#: An exhibit that is a Management's Discussion and Analysis and not an AIF: its title comes before the first
#: "Annual Information Form", which such a document only mentions. GIL's MD&A carried "3.0 OUR BUSINESS" and was
#: cut as the section; no stored AIF names an MD&A before naming itself.
_AIF_MDA = re.compile(r"(?i)management[’'`]?s\s+discussion\s+and\s+analysis")


def _aif_is_mda(text: str) -> bool:
    m = _AIF_MDA.search(text, 0, 2_000)
    return m is not None and _AIF.search(text).start() > m.start()


def _aif_stop_key(text: str, at: int) -> str:
    """The words of the stop title at ``at``, without its number: what makes two printings the same part."""
    m = _AIF_STOP_2.match(text, at) or _AIF_STOP_INLINE_2.match(text, at)
    words = re.findall(r"[^\W\d_]+", m.group(0).lower()) if m else []
    return " ".join(w for w in words if w != "item")


def _aif_unlisted_ends(text: str, ends: list[int]) -> list[int]:
    """``ends`` without a title-case stop whose part the document already printed before its business heading.

    GIB prints "Capital Structure" as a part of its own on page 2, before its business on page 5, and its
    three-year history -- which its own cross-references file under the business section -- has a title-case
    "Capital Structure" sub-heading that cut the section there. A part printed before the business block is not
    what ends it. Only the title-case printing is dropped, because asked of every printing this dropped the risk
    factors of eight exhibits whose contents (all before the business title) list them. And "before" is the
    business **heading** and a **part** -- not a contents entry, and not the first mention of a business title:
    the round 2 review measured a caps contents line "RISK FACTORS 20" before a prose "business of the
    Company" dropping the body's "Risk Factors", and every risk factor came back inside.
    """
    first = next((m for p in (_AIF_START_2, _AIF_START_2B, _AIF_START_2C) for m in p.finditer(text)
                  if not _PAGE_NO.match(text, m.end()) and not _aif_contents_entry(text, m)), None)
    if first is None:
        return ends
    before = {_aif_stop_key(text, e) for e in ends if e < first.start() and _aif_part_at(text, e)}
    return [e for e in ends if e < first.start() or _aif_caps_at(text, e) or _aif_stop_key(text, e) not in before]


def _aif_part_at(text: str, at: int) -> bool:
    """A stop printed as a part of its own: alone on its line, and not a contents entry. In either case, because
    GIB prints its parts in title case and only its contents in capitals."""
    m = _AIF_STOP_2.match(text, at)
    return m is not None and not _PAGE_NO.match(text, m.end()) and not _aif_contents_entry(text, m)


def _aif_caps_at(text: str, at: int) -> bool:
    m = _AIF_STOP_2.match(text, at) or _AIF_STOP_INLINE_2.match(text, at)
    return m is not None and _printed_as_a_heading(m.group(0))


#: A start and an end that are both numbered items: a real section and its successor, which is the one shape in
#: which a body under :data:`_AIF_MIN` is still a section. RCI's item 5 is 3,158 characters of pointers into its
#: MD&A, closed by "ITEM 5.2 – RISK FACTORS"; its contents entry, 332, stays under :data:`_AIF_ITEM_MIN`.
_AIF_ITEM_LINE = re.compile(r"(?i)[^\S\n]*item[^\S\n]+(?:\d|[ivx]+\b)")
_AIF_ITEM_MIN = 1_000


def _aif_short_item(text: str, m: re.Match, end: int | None, body: str) -> bool:
    return (end is not None and len(body) >= _AIF_ITEM_MIN
            and _AIF_ITEM_LINE.match(m.group(0)) is not None and _AIF_ITEM_LINE.match(text, end) is not None)


_AIF_SUB_NUMBERED = re.compile(r"(?im)^[^\S\n]*(?:item[^\S\n]+(\d{1,2})|(?:item[^\S\n]+)?(\d{1,2})\.(\d{1,2}))"
                               r"[.):]?[^\S\n]+[^\W\d_][^\n.]{0,80}$")


#: A body under this share of the way to the next part is suspect when item 4 ended it (see below).
_AIF_ITEM_4_SHARE = 0.025


def _aif_ends_at_item_4_early(text: str, m: re.Match, body: str, end: int, ends: list[int]) -> bool:
    """True when the section ends at "general development of the business" and is a sliver of what lies before
    the next part: a description printed **ahead of** item 4 whose own content goes on under item 4.

    AG's "DESCRIPTION OF BUSINESS" is 4,838 characters and the next part, RISK FACTORS, begins 314,686 characters
    after its heading: 1.5%. Its contents lists nothing under the description and its principal markets and
    material properties under item 4, but nothing in the text tells that from EMA (50%) and MOON (4.9%), whose
    item 4 is history alone -- which is why this is a label and not a later end. The share is set between AG
    and MOON, the two measured shapes either side of it; no other stored cut ends at item 4 below 50%.
    """
    if not _aif_stop_key(text, end).startswith("general development"):
        return False
    nxt = next((e for e in ends if e > end and not re.match(r"general development|.*history", _aif_stop_key(text, e))),
               len(text))
    return len(body) < _AIF_ITEM_4_SHARE * (nxt - m.start())


def _aif_item_goes_on(text: str, m: re.Match, end: int, ends: list[int]) -> bool:
    """True when the end is a numbered part of the start's own item and the item goes on after it.

    CNR's "ITEM 4" ends at "4.8 LEGAL MATTERS", and "4.9 INTANGIBLE PROPERTIES" follows before the next stop: the
    business block was cut short, and the label says so. A stop numbered as the next item, or one with nothing of
    its own item after it, is the end the numbering promises.
    """
    n = (re.match(r"[^\S\n]*(?:item[^\S\n]+)?(\d{1,2})\b", m.group(0), re.I)   # CNR prints "ITEM 4" on the line
         or re.search(r"(?i)\bitem[^\S\n]+(\d{1,2})[^\S\n]*[.:]?[^\S\n]*\n[^\S\n]*\Z",  # above its title
                      text[max(0, m.start() - 40):m.start()]))
    own = re.match(rf"[^\S\n]*(?:item[^\S\n]+)?{n.group(1)}\.(\d{{1,2}})", text[end:end + 20], re.I) if n else None
    if own is None or "risk" in _aif_stop_key(text, end):   # risk factors are 5.2 of item 5 and still its end
        return False
    # The next numbered line after the end's own: a heading (no full stop, short) numbered after the end inside
    # the same item. "4.5 million dollars." under "4.8 LEGAL MATTERS" is a figure (round 2 review).
    nxt = _AIF_SUB_NUMBERED.search(text, end + 1)
    return (nxt is not None and nxt.group(2) == n.group(1) and int(nxt.group(3)) > int(own.group(1)))


#: A start title in capitals in the middle of a line, behind a printed page number and followed by prose: EQX
#: prints its pages as single lines, "... Annual Information Form - 13 - DESCRIPTION OF THE BUSINESS Equinox Gold
#: is ...". The next word has to hold a lower-case letter, because that same document's contents is one line
#: of capital titles and page numbers. Read only when nothing else answered, so no line-start cut moves.
#: Title 2 reads one word after "business of (the)" here: inline nothing ends the title, so its three optional
#: words took the prose ("BUSINESS OF THE COMPANY Acme Gold is") and failed the capitals test (round 2 review).
_AIF_START_INLINE_2 = re.compile(rf"(?i)\b(?:{'|'.join(_AIF_TITLES_2).replace('{0,3}', '{0}')})\b")
_AIF_PAGE_BEFORE = re.compile(rf"\d{{1,3}}[^\S\n]*[{_DASH}]?[^\S\n]*$")
_AIF_PROSE_AFTER = re.compile(r"[^\S\n]+([^\W\d_]+)")


def _aif_inline_start(text: str, ends: list[int]) -> tuple[str | None, str]:
    """The last resort: a run-in start title (:data:`_AIF_START_INLINE_2`) and the first end after it."""
    seen = 0                     # matches arrive in order, so the cursor into ``ends`` only moves forward
    for m in _AIF_START_INLINE_2.finditer(text):
        word = _AIF_PROSE_AFTER.match(text, m.end())
        if not (_printed_as_a_heading(m.group(0)) and word and word.group(1) != word.group(1).upper()
                and _AIF_PAGE_BEFORE.search(text, max(0, m.start() - 20), m.start())):
            continue
        while seen < len(ends) and ends[seen] < m.end():
            seen += 1
        end = ends[seen] if seen < len(ends) else None
        body = text[m.end():(len(text) if end is None else end)].strip()
        if len(body) >= _AIF_MIN:
            return _aif_trim(body), ("ok" if end is not None else "overrun")
    return None, "missing"


#: What a stop split over two lines leaves at the end of the section: "DESCRIPTION OF THE" over PBA's
#: "CAPITAL STRUCTURE OF PEMBINA", "ITEM" over HSLV's "7: RISK FACTORS", and then the page number above either.
#: A figure alone is never trimmed -- a table's last cell is a figure -- and nothing repeats, because a repeated
#: group ending in ``\Z`` was quadratic on a run of figure lines (91 s on 20,000, round 2 review).
_AIF_DANGLING = re.compile(rf"(?i)\n[^\S\n]*(?:item|description{_H_SP}of(?:{_H_SP}the)?)[^\S\n]*\Z")
_AIF_PAGE_LINE = re.compile(rf"\n[^\S\n]*[{_DASH}]?\d{{1,3}}[{_DASH}]?[^\S\n]*\Z")


def _aif_trim(body: str) -> str:
    cut = _AIF_DANGLING.search(body)
    if cut is None:
        return body.rstrip()
    body = body[:cut.start()]
    page = _AIF_PAGE_LINE.search(body)
    return (body[:page.start()] if page else body).rstrip()


def _aif_pass_ours_1(text: str) -> tuple[str | None, tuple[int, str] | None]:
    """``ours-1``'s own pass over ``ours-1``'s own start title: ``(closed section, unclosed candidate)``.

    This is :func:`_aif_business_ours_1`'s rule, line for line, and deliberately not a line more: the page
    number test and nothing else of the start, the **first** line-start ``RISK FACTORS`` that is not a
    reference, the distance measured from the heading, and -- the part that matters -- the start **dropped**
    when that first stop is too close, rather than the search going on to a later one.

    That last line is what round 3 measured: looking past a near stop turned a contents block into a section,
    because dropping the start is ``ours-1``'s own contents test ("an index block the contents check missed",
    :data:`_BODY_MIN`). Round 1 had measured the opposite direction -- a subheading printed directly under the
    heading, where ``ours-1``'s one stop is the far one and it answers 27,018 characters. Both shapes are right
    only if this pass is ``ours-1``.

    The one thing it adds is the ``overrun`` candidate: ``ours-1`` answers nothing for a start it never closes,
    and this version says how long the text behind it is instead, provided it passes :data:`_AIF_MIN`.
    """
    for m in _AIF_START_OURS_1_TITLE.finditer(text):
        if _PAGE_NO.match(text, m.end()):
            continue
        e = next((e for e in _AIF_END.finditer(text, m.end()) if not _is_ref(text, e)), None)
        if e is None:
            body = text[m.end():].strip()
            return None, ((m.start(), body) if len(body) >= _AIF_MIN else None)
        if e.start() - m.start() >= _BODY_MIN:
            return text[m.end():e.start()].strip(), None
    return None, None


def _aif_business_ours_2(text: str) -> str | None:
    """The business section alone, for the caller that asks for text and not a label."""
    return _aif_business_ours_2_q(text)[0]


# ── the version table ───────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Rule:
    """One version of the cut. ``slice_item`` and ``aif_business`` have the signatures the boards already call."""

    version: str
    slice_item: object
    aif_business: object
    #: True while ``slice_item`` caps its text at :data:`MAX_ITEM_CHARS` and can therefore answer ``capped``.
    #: Nothing reads it yet: it is here for the ``sec_forms`` layer, which stores sections uncut, so a version it
    #: serves has this False and may not be labelled ``capped`` (issue #757). Until then it only records the fact.
    #: It is about ``slice_item`` alone. ``ours-2``'s AIF block (#756) caps nothing either, and says so by
    #: carrying :attr:`aif_business_q`; ``ours-1``'s does cap, at the same limit.
    caps: bool = True
    #: ``(text, quality)`` where the version has a label for its AIF section, None where it has not.
    #: ``ours-1``'s block answers text or nothing and the board never asked it for more, so inventing a label
    #: for it here would be inventing behaviour. :func:`cut_aif_business_q` raises for such a version rather
    #: than making one up.
    aif_business_q: object | None = None
    #: The suspect signals :func:`flag` may raise for this version's slices (#835). The version is what says
    #: which are defined, so a stored row's ``rules_version`` names its flagger as well as its cut, and no
    #: second column is needed to record which flags were computed.
    signals: tuple[str, ...] = SIGNALS_OURS_1


RULES: dict[str, Rule] = {
    OURS_1: Rule(version=OURS_1, slice_item=_slice_item_ours_1, aif_business=_aif_business_ours_1, caps=True),
    # ``ours-2``'s AIF block is its own since #756: six start titles and ten stop titles adapted from
    # edgartools (the provenance block above it), our own section end, and a label.
    OURS_2: Rule(version=OURS_2, slice_item=_slice_item_ours_2, aif_business=_aif_business_ours_2,
                 caps=False, aif_business_q=_aif_business_ours_2_q, signals=SIGNALS_OURS_2),
}


#: "no version given", which is not the same as ``None``. A row's ``rules_version`` read back as NULL arrives as
#: ``None``, and that must raise rather than quietly cut with whatever the default is today.
_UNSET: object = object()


def rule(version: str | None | object = _UNSET) -> Rule:
    """The :class:`Rule` of ``version``; ``ValueError`` for one this module cannot cut with.

    Every caller that stores what it cuts names its version, so that adding a version cannot quietly move text a
    board already stored; the boards do (``module_value_chain.splitter.VERSION``). Leaving the argument out means
    :data:`DEFAULT_VERSION`, and is for a caller that only wants to look at a section, never one that writes a
    row. The default is resolved here, at the call, rather than frozen into the signature, so that a test can move
    it and see whether a caller was really naming its own version.

    An explicit ``None`` is **not** the default: it raises. A row's ``rules_version`` read back as NULL arrives as
    ``None``, and a stored section whose version nobody can reproduce is worse than a failed run -- which is the
    same reason an unknown name raises instead of falling back.
    """
    name = DEFAULT_VERSION if version is _UNSET else str(version)
    try:
        return RULES[name]
    except KeyError:
        raise ValueError(f"unknown section rule version {version!r}; known: {', '.join(VERSIONS)}") from None


def cut_item(text: str, form: str = "10-K", *, version: str | None | object = _UNSET) -> tuple[str | None, str]:
    """``(item_text, quality)`` of the business item in ``text``, cut by ``version``."""
    return rule(version).slice_item(text, form)


def cut_flag(quality: str, text: str, item: str | None, form: str = "10-K", *,
             version: str | None | object = _UNSET) -> str:
    """``quality`` with ``version``'s own ``;suspect`` flags -- what a layer that stores the row calls.

    :func:`flag` raises every signal, which is ``ours-1``'s set; this picks the set the version defines
    (:attr:`Rule.signals`). A row written through here can be read back by its ``rules_version`` alone.
    """
    return flag(quality, text, item, form, signals=rule(version).signals)


def cut_aif_business(text: str, *, version: str | None | object = _UNSET) -> str | None:
    """The business section of an Annual Information Form in ``text``, cut by ``version``; None when it holds none."""
    return rule(version).aif_business(text)


def cut_aif_business_q(text: str, *, version: str | None | object = _UNSET) -> tuple[str | None, str]:
    """``(business section, quality)`` -- ok / short / overrun / missing -- for a version that labels it.

    ``ValueError`` for one that does not, which is ``ours-1``: its block answers text or nothing, and a label
    made up here would be a label no measurement stands behind.
    """
    r = rule(version)
    if r.aif_business_q is None:
        raise ValueError(f"{r.version} has no labelled AIF cutter; call cut_aif_business for its text")
    return r.aif_business_q(text)
