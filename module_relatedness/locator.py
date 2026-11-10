"""Find Item 1 / Item 4 in a filing from its heading list, with the writer (avo, 8 Nov 2026).

The rule-based cut (``module_value_chain/splitter.py``) stays the default. Only when its cut is ``overrun`` (no stop
header followed the start), ``missing`` or ``short`` does the profile run ask the board's writer (EVREN by default, no
fee) where the business section starts and ends. The writer is sent the numbered heading lines of the whole document,
never its text, and answers two heading numbers; the text between them is the section.

Pure functions plus ``locate``, which makes the one call.
"""
from __future__ import annotations

import json
import re

from . import profile_v3

PROMPT_VERSION = "loc1"
MAX_HEADINGS = 400
# The rule cut's labels that ask the locator (``splitter.slice_item``).
ASK_ON = ("overrun", "missing", "short")


def asks(quality) -> bool:
    """True when the rule cut ``quality`` sends the filing to the locator: a label of ``ASK_ON``, or an ``ok`` the
    splitter flags as suspect (``ok;suspect:S4``, #400 WP7: 24 of the 29 wrong cuts of the 10-K reading report were
    labelled ``ok``, so the old labels alone never asked)."""
    q = str(quality or "")
    return q.split(";", 1)[0] in ASK_ON or ";suspect" in q

SYSTEM = (
    "You are given the numbered heading lines of a US annual report (10-K, 20-F or 40-F), in document order. Find "
    "the business section: Item 1 \"Business\" of a 10-K, Item 4 \"Information on the Company\" of a 20-F, or the "
    "description of the business of a 40-F. Skip the table of contents: the section starts at the heading where its "
    "body begins. Return ONE JSON object {\"start\": n, \"end\": m}: start is the number of the heading that opens "
    "the section; end is the number of the first heading after the section (usually Item 1A Risk Factors), or null "
    "when the section runs to the end. Return {\"start\": null} when there is no business section."
)

_ITEM = re.compile(r"^\s*item\s*\d", re.I)


def system_prompt() -> str:
    return f"{SYSTEM}\n\nPrompt version: {PROMPT_VERSION}."


def headings(text: str) -> list[tuple[int, str]]:
    """``(offset, heading)`` of the document's heading lines, in order, at most ``MAX_HEADINGS``.

    A heading is a line ``profile_v3._is_heading`` reads as one and that holds no digit (a table row is not a
    heading), or any ``Item N`` line."""
    out, pos = [], 0
    for line in (text or "").splitlines(keepends=True):
        s = line.strip()
        if s and (_ITEM.match(s) and len(s) <= 100 or profile_v3._is_heading(s) and not re.search(r"\d", s)):
            out.append((pos + line.index(s), s))
            if len(out) >= MAX_HEADINGS:
                break
        pos += len(line)
    return out


def build_user(name: str, form: str | None, heads: list[tuple[int, str]]) -> str:
    lines = "\n".join(f"{i}: {h}" for i, (_o, h) in enumerate(heads))
    return f"Company: {name}\nForm: {form or '10-K'}\nHeadings:\n{lines}"


def _int(v):
    if isinstance(v, bool):
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse(raw, n: int) -> tuple[int, int] | None:
    """``(start, end)`` heading numbers (``end`` = ``n`` for "to the end"), or None when the reply is not usable."""
    if isinstance(raw, str):
        raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    start = _int(data.get("start"))
    end = n if data.get("end") is None else _int(data.get("end"))
    if start is None or end is None or not 0 <= start < end <= n:
        return None
    return start, end


def cut(text: str, heads: list[tuple[int, str]], start: int, end: int) -> str:
    stop = heads[end][0] if end < len(heads) else len(text)
    return text[heads[start][0]:stop].strip()


def locate(ticker: str, name: str, form: str | None, text: str, *, budget=None, provider: str = "deepseek",
           model: str | None = None, client=None) -> str | None:
    """The business section of ``text`` (a whole filing) as the writer places it, or None.

    One call (counted by ``budget``), JSON mode, no thinking, cached like a profile chunk. A reply that names no
    usable pair of headings, or a section under the profile floor, is None: the caller keeps the rule cut."""
    from . import config, profiles

    heads = headings(text)
    if len(heads) < 2:
        return None
    if budget is not None and not budget.consume():
        raise profiles.BudgetExceeded()
    extra = {"model": model} if model else {}
    raw = profiles.llm(provider).complete(
        system_prompt(), build_user(name, form, heads), thinking=False, reasoning_effort=None, max_tokens=200,
        json_mode=True, use_cache=True, client=client, ticker=ticker, prompt_version=config.PROMPT_VERSION,
        validate=lambda c: parse(c, len(heads)) is not None, **extra)
    got = parse(raw, len(heads))
    if got is None:
        return None
    section = cut(text, heads, *got)
    return section if profiles.text_status(section) is None else None
