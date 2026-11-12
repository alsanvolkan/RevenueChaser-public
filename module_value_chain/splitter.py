"""Slice Item 1 (10-K) / Item 4 (20-F) out of a stripped filing body.

**The rule itself now lives in ``rcdata/sec_sections.py``**, which owns every version of it; this module is the
board's view of it and pins the version the board reads. Nothing here cuts text of its own.

The board stays on :data:`VERSION` (``ours-1``), the rule exactly as it was on 12 Nov 2026. That pin is the point:
``rcdata/build/fundamentals/filings.document_sections`` writes ``filing_item`` and ``edgar_text.slice_document``
writes ``chain_filings``, neither with a version column, so a rule change reaching them would mix two rules in one
table with no way to tell the rows apart, and would restale every Relatedness profile -- a paid model run. The improved rule (``ours-2``,
issues #755 and #756) is used by the ``sec_forms`` layer only; moving a board onto it is its own change, with the
owner's before/after look at what the board shows.

The names below are re-exported, not reimplemented: ``flag``, ``base``, ``strip_html`` and every helper are the
rule module's own objects, so a caller cannot get a different answer here than there. ``slice_item`` and
``aif_business`` are the two exceptions, and they are wrappers for one reason: to name :data:`VERSION`. They do
nothing else, and ``rcdata/tests/test_sec_sections.py`` holds them to that.
"""
from __future__ import annotations

from ..rcdata import sec_sections as _rule

#: The version of the cut this board reads. See the module docstring for why it is pinned and not taken by default.
VERSION = _rule.OURS_1

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
#: ``quality`` with ``;suspect:<signals>`` added when an ``ok`` slice still looks wrong.
flag = _rule.flag
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
