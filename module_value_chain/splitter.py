"""Slice Item 1 (10-K) / Item 4 (20-F) out of a stripped filing body.

The stop regex is the part that breaks: ``Item 1A`` appears as ``ITEM 1A.``,
``Item 1A — Risk Factors``, and inside iXBRL. We search case-insensitively for a
header at a line/whitespace boundary and take the *first* start and the first
stop after it.
"""
from __future__ import annotations

import re

from . import config


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
