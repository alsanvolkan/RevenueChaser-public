"""How important a US economic-calendar event is: 3 high, 2 medium, 1 low (News board plan §4).

One tested table, shared by every board that shows the calendar. Unusual Whales' calendar has no importance field
(`core/providers/unusual_whales.py` ``economic_calendar_result``: ``event``, ``type``, ``time``, ``forecast``,
``prev``), so the level comes from the event's name and type. Matching is on whole words of the lower-cased name, so
"PPI" never reads as "CPI". The owner reviews and edits the two keyword lists below.

SPX Gamma's week card keeps its own ``vol_calendar.HIGH_IMPACT`` for now: it marks PPI high, this table marks it
medium, and moving that card onto this table would change what the card shows.
"""
from __future__ import annotations

import re

HIGH = 3
MEDIUM = 2
LOW = 1

#: Phrases (whole words, lower case) that make an event high importance.
HIGH_PHRASES = (
    "cpi", "pce", "employment report", "employment situation", "nonfarm", "gdp", "retail sales", "ism",
    "fomc", "fed interest rate", "rate decision",
)
#: Phrases that make an event medium importance when no high phrase matched.
MEDIUM_PHRASES = (
    "ppi", "jolts", "jobless claims", "consumer sentiment", "inflation expectations", "housing starts",
)

_WORDS = re.compile(r"[a-z0-9]+")


def _words(text) -> str:
    return " " + " ".join(_WORDS.findall(str(text or "").lower())) + " "


def _has(words: str, phrases) -> bool:
    return any(f" {p} " in words for p in phrases)


def importance(event, kind) -> int:
    """The level of one calendar row from its ``event`` name and UW ``type`` (report, fed-speaker, fomc)."""
    words, k = _words(event), str(kind or "").strip().lower()
    if k == "fomc" or _has(words, HIGH_PHRASES):
        return HIGH
    if k == "fed-speaker":
        is_chair = " chair " in words and " vice chair " not in words
        return HIGH if is_chair else MEDIUM
    if _has(words, MEDIUM_PHRASES):
        return MEDIUM
    return LOW


def dots(level: int) -> str:
    """The level as three dots, filled from the left: ``●●○`` for medium."""
    n = max(LOW, min(HIGH, int(level)))
    return "●" * n + "○" * (HIGH - n)
