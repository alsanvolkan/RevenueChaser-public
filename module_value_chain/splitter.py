"""Slice Item 1 (10-K) / Item 4 (20-F) out of a stripped filing body.

**The rule itself now lives in ``rcdata/sec_sections.py``**, which owns every version of it; this module is the
board's view of it and pins the version the board reads. Nothing here cuts text of its own.

The board reads :data:`VERSION` (``ours-2``) since #847 -- plan v31 step 1b. It read ``ours-1``, the rule exactly
as it was on 12 Nov 2026, for as long as ``ours-2`` was unmerged, and the reason for pinning at all has not
changed: ``rcdata/build/fundamentals/filings.document_sections`` writes ``filing_item`` and
``edgar_text.slice_document`` writes ``chain_filings``, neither with a version column, so a rule change reaching
them mixes two rules in one table with no way to tell the rows apart, and restales every Relatedness profile -- a
paid model run. That is why moving this constant is its own change, with its own re-slice and the owner's
before/after look, and not a side effect of some other PR.

What the move buys, *measured* on real filings (13 Nov 2026, read-only, plan v31 section 9): about one filing in
eight that this board profiles is cut differently, and each of the 12 differences read at both boundaries was an
``ours-1`` defect -- a 1-character ``short`` where the filer spells it "ITEM l. BUSINESS" with a lowercase L, two
``overrun`` slices running through the signatures, a slice that started inside a cross-reference, and the
200,000-character cap. The 40-F leg changes nothing either way: no stored 40-F primary document holds a business
section either rule can find, which is the exhibit path's question (#810 / #827) and not this pin's.

**Already-stored rows were cut by ``ours-1`` and are not re-labelled by this constant.** ``chain_filings`` is
rewritten by ``reslice.apply``, which rides the first full SEC cut as one run; until that run the stored corpus is
``ours-1`` text under an ``ours-2`` pin. ``filing_item`` is the held fundamentals build's.

The names below are re-exported, not reimplemented: ``flag``, ``base``, ``strip_html`` and every helper are the
rule module's own objects, so a caller cannot get a different answer here than there. ``slice_item`` and
``aif_business`` are the two exceptions, and they are wrappers for one reason: to name :data:`VERSION`. They do
nothing else, and ``rcdata/tests/test_sec_sections.py`` holds them to that.
"""
from __future__ import annotations

from ..rcdata import sec_sections as _rule

#: The version of the cut this board reads. See the module docstring for why it is pinned and not taken by default.
#: Moving it moves the cut, the labels (:func:`cut_flag` is handed this same constant by both stores) and what
#: ``module_relatedness.locator.asks()`` pays a model to re-cut, so it moves with a re-slice and nothing else.
VERSION = _rule.OURS_2

# The rule's own helpers, re-exported for the callers and tests that read them (``_LINE_START`` is asserted on
# directly by ``tests/test_splitter_foreign_400.py``).
_LINE_START = _rule._LINE_START
_LINE_END = _rule._LINE_END
_TOC_NEXT = _rule._TOC_NEXT
_PAGE_NO = _rule._PAGE_NO
_REF_BEFORE = _rule._REF_BEFORE
_BODY_MIN = _rule._BODY_MIN
_is_ref = _rule._is_ref
_line_item = _rule._line_item
_OVERVIEW = _rule._OVERVIEW
_REGULATION = _rule._REGULATION
_before_regulation = _rule._before_regulation
_AIF = _rule._AIF
_AIF_START = _rule._AIF_START
_AIF_END = _rule._AIF_END
_SECTION = _rule._SECTION
_TAIL_REF = _rule._TAIL_REF
_START_REF = _rule._START_REF

#: ``quality`` without its suspect flags: ``ok;suspect:S4`` -> ``ok``.
base = _rule.base
#: ``quality`` with ``;suspect:<signals>`` added when an ``ok`` slice still looks wrong. Raises every signal,
#: which is ``ours-1``'s set; a caller that STORES the label calls :data:`cut_flag` with its own
#: :data:`VERSION` instead, so that bumping ``VERSION`` moves the flagger with the cut (#835).
flag = _rule.flag
#: ``quality`` with the flags the named version defines for the cutter that made the text: pass ``aif=True``
#: for a block of :data:`aif_business`, because the two sets differ (#835, #838).
cut_flag = _rule.cut_flag
#: Crude HTML -> text (scripts/styles dropped, entities unescaped).
strip_html = _rule.strip_html


def slice_item(text: str, form: str = "10-K") -> tuple[str | None, str]:
    """``(item_text, quality)`` -- ok / capped / short / overrun / missing -- by the board's pinned version.

    The rule and its reasoning are in ``rcdata.sec_sections``; this wrapper only names the version.
    """
    return _rule.cut_item(text, form, version=VERSION)


def aif_business(text: str) -> str | None:
    """The business section of an Annual Information Form in ``text``; None when it holds none."""
    return _rule.cut_aif_business(text, version=VERSION)
