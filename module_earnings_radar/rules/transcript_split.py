"""Split a provider earnings-call transcript into prepared remarks and Q&A.

The provider (Alpha Vantage ``EARNINGS_CALL_TRANSCRIPT``, measured on the live key 21 Nov 2026) answers a list of
turns, each with exactly ``speaker``, ``title``, ``content`` and ``sentiment``. **It carries no prepared-remarks /
Q&A field**, so the boundary has to be read off ``title``: an earnings call's order is fixed — investor relations
opens, then the CEO and CFO, then the operator opens the line, then analysts ask — so the **first analyst turn is
the split**. Before it the prepared remarks, from it onward the Q&A.

That boundary is **inferred, not labelled**, which is the one thing a caller must respect: a transcript with no
analyst turn has no Q&A half, and this function then answers ``(prepared, None)`` rather than guessing. R8 reads
that as its single-argument branch (``rule8_sentiment.score(prepared_remarks=...)``) and never computes a
``tone_delta`` whose "Q&A" is really more prepared remarks.

The per-turn ``sentiment`` the provider sends is **not** read here. R8 measures Loughran-McDonald dictionary rates
over our own tokenizer; the provider's number is a different measurement and swapping it in would change what
every threshold means (its own decision, with its own measurement — #1214).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Iterable, Optional

#: A turn is an analyst's when its title names one. Matched on the lowercased title so that ``Analyst``,
#: ``Analysts`` and a title that carries a firm name all count; the measured call used the bare ``Analyst``.
ANALYST = "analyst"

JOIN = "\n\n"


def is_analyst(title) -> bool:
    return ANALYST in str(title or "").strip().lower()


def _text(turns: Iterable[Mapping]) -> Optional[str]:
    """The turns' ``content`` joined, or ``None`` when they carry no text at all."""
    parts = [str(t.get("content") or "").strip() for t in turns]
    body = JOIN.join(p for p in parts if p)
    return body or None


def split_at_first_analyst(turns) -> tuple[Optional[str], Optional[str]]:
    """``(prepared_remarks, qa_section)``, either one ``None`` when that half has no text.

    ``turns`` is the provider's ``transcript`` list. A half with no turns, or whose turns are all empty, is
    ``None``, so a caller can pass the pair straight to ``rule8_sentiment.score`` and get the right branch:
    both halves present scores the tone delta, prepared alone scores the degraded single half, neither leaves
    the rule unavailable.
    """
    rows = [t for t in (turns or []) if isinstance(t, Mapping)]
    boundary = next((i for i, t in enumerate(rows) if is_analyst(t.get("title"))), None)
    if boundary is None:
        return _text(rows), None
    return _text(rows[:boundary]), _text(rows[boundary:])
