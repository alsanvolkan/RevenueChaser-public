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
  slice holding another item's own title line is ``overrun`` however it ended. No board reads it, and moving one
  onto it is its own change.
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
_DASH = r"\-\u2010-\u2015\u2212"
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
# heading we know, 5 with no stop, 7 rejected as too short). Issue #756 is where the improvement will land.
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
#: tab, a no-break space, a narrow no-break space and an ideographic space -- the ones a tag or an entity
#: becomes that a word boundary does not already stop. Everything else keeps its page number a page number:
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

_LINE_START_2 = {
    "10-K": re.compile(rf"(?im)^[^\S\n]*{_ITEM}s?\s{{0,3}}(1|i)\b(?:\s*(?:and|&|,)\s*2\b)?"
                       rf"\s*[.:]?\s*(?:[{DASH}]+\s*)?(?:(?:description\s+of|our)\s+(?:the\s+)?)?"
                       rf"{spaced('business')}"),
    "20-F": re.compile(rf"(?im)^[^\S\n]*{_ITEM}\s{{0,3}}(4)\b\s*[.:]?\s*(?:[{DASH}]+\s*)?"
                       rf"{spaced('information')}\s+(?:on|about|of)\s+(?:the\s+)?"
                       r"(?!(?:our\s+)?executive)[\w&.-]+"),
}
_LINE_END_2 = re.compile(rf"(?im)^[^\S\n]*{_ITEM}\s{{0,3}}(1[abc]|[2-9][abc]?|1[0-9])\b{_NOT_SUBITEM}")

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
    "10-K": (rf"\b{_ITEM}\s*1a\s*[\.\:{_DASH}\s]*\s*{_spaced('risk')}\s+{_spaced('factors')}"
             rf"|\b{_ITEM}\s*1b\s*[\.\:{_DASH}\s]*\s*{_spaced('unresolved')}"
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


def _inline_ref(text: str, m: re.Match) -> bool:
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
    return _starts_mid_line(text, m) and _ref_ours_2(text, m)


def _starts_mid_line(text: str, m: re.Match) -> bool:
    """True when something other than whitespace precedes ``m`` on its own line."""
    return bool(text[text.rfind("\n", 0, m.start()) + 1:m.start()].strip())


def _ref_ours_2(text: str, m: re.Match) -> bool:
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

    Two really are equivalent, and a reader changing either should expect the tests to stay green:

    - ``<=`` for ``<`` in the comparison. ``closed == opened`` needs both to be -1, which ``closed >= 0``
      has already excluded.
    - the opening quote searched from the far end of the window. No shape was found where the first and the
      last opening quote fall on different sides of the closing one.

    A third, the window measured from the match's end rather than its start, only moves where the
    200-character edge falls; it takes a quotation mark exactly 192 characters of filler past the number to
    tell the two apart, and neither answer is the right one. The real question for all of this is how often
    each shape occurs in filings, which is #763's hand-checked set and not a built string.
    """
    if _REF_BEFORE.search(text[max(0, m.start() - 40):m.start()].rstrip()):
        return True
    line_end = text.find("\n", m.end())
    window = text[m.start():min(m.start() + 200, line_end if line_end >= 0 else len(text))]
    opened, closed = window.find("“"), window.find("”")
    if closed < 0:
        return False
    # The quoted thing is this item's own title: ``Item 1A, "Risk Factors," of this report``. The quote opens
    # right after the number and closes after the title, so the unopened test alone reads it as a heading --
    # and that is the citation style a 10-K uses for a cross-reference, which cost Item 1 half its text.
    if _TITLE_QUOTED.match(text, m.end()):
        return True
    return opened < 0 or closed < opened


#: An opening quote that follows a heading's number with nothing but punctuation between: the quotation marks
#: are around the item's **title**, which is how a filing cites an item it is not starting.
_TITLE_QUOTED = re.compile(rf"[\s.,:;{_DASH}]{{0,12}}[“‘]")


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
    for m in _LINE_START_2[key].finditer(text):
        if _TOC_NEXT.search(text[m.end():m.end() + 400]) or _PAGE_NO.match(text, m.end()) or _ref_ours_2(text, m):
            continue
        own = m.group(1).lower()
        end = next((e.start() for e in _LINE_END_2.finditer(text, m.end() + MIN_ITEM_CHARS)
                    if e.group(1).lower() != own and not _ref_ours_2(text, e)), None)
        if end is None:
            end = next((t.start() for t in stop_re.finditer(text, m.end()) if not _inline_ref(text, t)), None)
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
    return any(m.group(1).lower() != own and not _ref_ours_2(item, m)
               for m in _LINE_END_2.finditer(item))


def _quality_ours_2(item: str, own: str, closed: bool) -> str:
    """The label for a slice ``ours-2`` has already cut. Never ``capped``: nothing here caps."""
    if len(item) < MIN_ITEM_CHARS:
        return "pointer" if _POINTER.search(item) else "short"
    if not closed or _holds_another_item(item, own):
        return "overrun"
    return "ok"


def _slice_item_ours_2(text: str, form: str = "10-K") -> tuple[str | None, str]:
    """``(item_text, quality)``: ok / short / overrun / pointer / missing. **Never ``capped``**.

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
    """
    text = text or ""
    if not text.strip():
        return None, "missing"

    key = "20-F" if str(form).upper().startswith("20") else "10-K"
    own = "1" if key == "10-K" else "4"

    line = _line_item_ours_2(text, key)
    if line is not None:
        start, end, closed = line
        return text[start:end].strip(), _quality_ours_2(text[start:end].strip(), own, closed)

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
        stop_m = stop_re.search(text, start_m.end())
        item = text[start_m.end():stop_m.start() if stop_m else len(text)].strip()
        if len(item) >= MIN_ITEM_CHARS:
            best, closed = item, stop_m is not None
            break
        if best is None or len(item) > len(best):
            best, closed = item, stop_m is not None
    if best is None:
        return None, "missing"
    return best, _quality_ours_2(best, own, closed)


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


def flag(quality: str, text: str, item: str | None, form: str = "10-K") -> str:
    """``quality`` with ``;suspect:<signals>`` added when an ``ok`` slice of ``text`` still looks wrong.

    **This is ``ours-1``'s flagger, and the only one there is.** S6 reads :data:`_LINE_END`, so a sub-item is a
    heading to it: it marks PCVX's correctly cut Item 1 ``ok;suspect:S6``, undoing at the label what ``ours-2``
    fixed in the cut. S8 compares the slice against :data:`MAX_ITEM_CHARS`, which ``ours-2`` does not cap at, so
    every long ``ours-2`` slice is ``ok;suspect:S8``. The right place for a per-version flagger is a ``flag``
    member on :class:`Rule`, and the caller that needs it is the ``sec_forms`` layer (#757); until then nothing
    calls this on an ``ours-2`` slice, and a caller that starts to must read this paragraph first.
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
    on = [k for k, v in s.items() if v]
    if any(s[k] for k in ("S1", "S4", "S5", "S6", "S8")) or (s["S2"] and s["S3"]):
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
    #: It is about ``slice_item`` alone -- ``ours-2`` borrows ``ours-1``'s AIF block, which still caps, and #756
    #: is where that block gets a version of its own.
    caps: bool = True


RULES: dict[str, Rule] = {
    OURS_1: Rule(version=OURS_1, slice_item=_slice_item_ours_1, aif_business=_aif_business_ours_1, caps=True),
    # ``ours-2`` keeps ``ours-1``'s AIF block for now: improving it is #756, and a version that changed two
    # rules at once would leave nobody able to say which of them moved a stored section.
    OURS_2: Rule(version=OURS_2, slice_item=_slice_item_ours_2, aif_business=_aif_business_ours_1, caps=False),
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


def cut_aif_business(text: str, *, version: str | None | object = _UNSET) -> str | None:
    """The business section of an Annual Information Form in ``text``, cut by ``version``; None when it holds none."""
    return rule(version).aif_business(text)
