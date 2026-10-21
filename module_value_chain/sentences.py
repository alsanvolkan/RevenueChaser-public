"""Sentence spans shared by the concentration and mention extractors.

A sentence ends at ``.`` or ``;`` followed by whitespace and an upper-case
letter, quote or ``(`` — but not after a legal-form or common abbreviation
("Apple Inc. and Dell"). Newlines do not end a sentence: Item 1 prose is often
hard-wrapped mid-sentence.
"""
from __future__ import annotations

import re
from bisect import bisect_right

_ABBREV = r"(?<!\bInc)(?<!\bCorp)(?<!\bCo)(?<!\bLtd)(?<!\bU\.S)(?<!\bN\.V)(?<!\bS\.A)(?<!\bNo)"
SENT_END = re.compile(_ABBREV + r"[.;]\s+(?=[A-Z(“\"])")


def spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each sentence; the end punctuation stays in the sentence."""
    out, start = [], 0
    for m in SENT_END.finditer(text):
        out.append((start, m.start() + 1))
        start = m.end()
    out.append((start, len(text)))
    return out


class Bounds:
    """Sentence lookup by position, computed once per text."""

    def __init__(self, text: str):
        self._spans = spans(text or "")
        self._starts = [a for a, _ in self._spans]

    def at(self, pos: int) -> tuple[int, int]:
        """The sentence holding ``pos`` (the whitespace between two sentences maps to the earlier one)."""
        return self._spans[max(0, bisect_right(self._starts, pos) - 1)]
