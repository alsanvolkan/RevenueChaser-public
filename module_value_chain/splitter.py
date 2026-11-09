"""Slice Item 1 (10-K) / Item 4 (20-F) out of a stripped filing body.

The stop regex is the part that breaks: ``Item 1A`` appears as ``ITEM 1A.``,
``Item 1A — Risk Factors``, and inside iXBRL. We search case-insensitively for a
header at a line/whitespace boundary and take the *first* start and the first
stop after it.
"""
from __future__ import annotations

import re

from . import config


# ── line-start headings (#400 10-K package WP1) ─────────────────────────────────────────────────────────────
# The served text breaks a heading over lines ("ITEM\n1A.", "ITEM 4. \n INFORMATION ON THE COMPANY") and splits
# words ("B usiness", "INFO RMATION"), and an item named inside a sentence ("see “Item 5. Operating ...”") is not a
# heading. So a heading starts a line, the gaps inside it may hold a new line, and the body ends at the next
# line-start item heading that is not a quoted reference. Measured on 32 filings of the #400 input checks (9 Nov
# 2026): main's rule was wrong on about 20, most of them labelled ``ok`` (ITUB 16k of 293k, IH, ALKS, MREO, NBP).
_LINE_START = {
    "10-K": re.compile(r"(?im)^[^\S\n]*items?\s{0,3}(1|i)\b(?:\s*(?:and|&|,)\s*2\b)?\s*[.:]?\s*"
                       rf"(?:[{config.DASH}]+\s*)?(?:(?:description\s+of|our)\s+(?:the\s+)?)?{config.spaced('business')}"),
    "20-F": re.compile(r"(?im)^[^\S\n]*item\s{0,3}(4)\b\s*[.:]?\s*"
                       rf"(?:[{config.DASH}]+\s*)?{config.spaced('information')}\s+(?:on|about|of)\s+(?:the\s+)?[\w&.-]+"
                       r"(?!\s+(?:our\s+)?executive)"),
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
        end = next((e.start() for e in _LINE_END.finditer(text, m.end() + config.MIN_ITEM_CHARS)
                    if e.group(1).lower() != own and not _is_ref(text, e)), None)
        if end is None:
            return None          # no closing heading: the old rule decides (and labels it overrun)
        if end - m.start() < _BODY_MIN:
            continue
        return m.end(), end
    return None


def slice_item(text: str, form: str = "10-K") -> tuple[str | None, str]:
    """Return ``(item_text, quality)``: ok / short / overrun / missing.

    A 10-K opens with a table of contents that lists ``Item 1. Business`` and
    ``Item 1A. Risk Factors`` back to back, so the *first* start match is the
    TOC and the slice is a few dozen characters. We therefore try every start
    and keep the longest section — the real body, not the contents page.

    ``overrun``: no stop header follows the start, so the slice runs on into
    Item 1A and beyond (or up to the cap). It is kept, but not labelled ``ok``.
    """
    text = text or ""
    if not text.strip():
        return None, "missing"

    key = "20-F" if str(form).upper().startswith("20") else "10-K"
    start_re = re.compile(config.START_PATTERNS[key], re.I)
    stop_re = re.compile(config.STOP_PATTERNS[key], re.I)

    line = _line_item(text, key)
    if line is not None:
        return text[line[0]:line[1]].strip()[:config.MAX_ITEM_CHARS], "ok"

    best: str | None = None
    stopped = False
    for start_m in start_re.finditer(text):
        stop_m = stop_re.search(text, start_m.end())
        end = stop_m.start() if stop_m else min(len(text), start_m.end() + config.MAX_ITEM_CHARS)
        item = text[start_m.end():end].strip()
        if len(item) >= config.MIN_ITEM_CHARS:
            # The first section long enough is the real body; the TOC's entry is
            # tiny, and a later cross-reference can otherwise win on length.
            best, stopped = item, stop_m is not None
            break
        if best is None or len(item) > len(best):
            best, stopped = item, stop_m is not None
    if best is None:
        return None, "missing"

    best = best[:config.MAX_ITEM_CHARS]
    if len(best) < config.MIN_ITEM_CHARS:
        return best, "short"
    return best, ("ok" if stopped else "overrun")


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
