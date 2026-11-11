"""LLM structured business profile for one company (v3, #400).

The writer returns one structured JSON (``profile_v3``): read whole, in chunks above 60k characters, and merged.
``profile_for`` returns the board's attribute rows (``config.PROFILE_FIELDS``, what the filters and today's
similarity read) as a ``Profile``: a dict that also carries the v3 JSON (``.v3``) and where each field came from
(``.labels``), which the store keeps in ``profile_json`` / ``label_source``.
"""
from __future__ import annotations

import threading

from RevenueChaser.core.providers import deepseek

from . import config
from . import profile_v3

# The v3 prompt (#400): one structured JSON per company, see ``profile_v3``.
SYSTEM = profile_v3.SYSTEM


class BudgetExceeded(Exception):
    """The run's LLM-call budget is spent; the caller leaves the group pending."""


class CallBudget:
    """A run's remaining LLM-call count, shared by the worker threads.

    ``max_calls`` of ``None`` means unlimited; ``0`` means no call at all. The
    budget counts **calls**: every chunk, and a retry (the second, uncached request in
    :func:`profile_for`), is counted.
    """

    def __init__(self, max_calls: int | None):
        self._remaining = max_calls
        self._lock = threading.Lock()
        self.calls = 0

    def consume(self) -> bool:
        with self._lock:
            if self._remaining is not None and self._remaining <= 0:
                return False
            if self._remaining is not None:
                self._remaining -= 1
            self.calls += 1
            return True


def _writers():
    from . import writers
    return writers


PROVIDERS = ("evren", "deepseek", "grok", "claude")      # module_relatedness.writers.WRITERS (#360)


def llm(provider: str = "deepseek"):
    """The chat client module of a profile writer (``writers.WRITERS``: evren, deepseek, grok). Each exposes
    ``complete`` (same signature) and ``MODEL``; ``complete`` is looked up at call time, so a test that patches
    ``deepseek.complete`` still reaches the profile path."""
    if provider == "deepseek":
        return deepseek
    if provider not in PROVIDERS:
        raise ValueError(f"unknown profile writer {provider!r}; one of {', '.join(PROVIDERS)}")
    return _writers().client(provider)


def system_prompt(version: str | None = None) -> str:
    """``SYSTEM`` with the prompt version appended (``profile_v3.system_prompt``).

    ``deepseek.complete`` hashes ``(system, user)`` for its cache key and does not
    take ``prompt_version``; appending the version here is what makes a
    ``PROMPT_VERSION`` bump miss the LLM cache instead of replaying the old reply.
    """
    return profile_v3.system_prompt(version or config.PROMPT_VERSION)


def text_status(item_text: str | None) -> str | None:
    """``None`` when the filing text is long enough to profile, else why it is not.

    ``no_text``: nothing at all (or whitespace). ``short_text``: under
    :data:`config.MIN_INPUT_CHARS`, Value Chain's own floor for a sliced Item 1 — the slice is then a
    table-of-contents fragment, and a model asked from it writes the profile from the company name
    instead of refusing (#313), so the call is never made.
    """
    text = (item_text or "").strip()
    if not text:
        return "no_text"
    return "short_text" if len(text) < config.MIN_INPUT_CHARS else None


def build_user(name: str, item_text: str, context: dict | None = None, part: tuple[int, int] | None = None) -> str:
    """One call's user prompt: the whole text (no cut, #400), or chunk ``part`` of it."""
    return profile_v3.build_user(name, item_text, context, part)


def prompts(name: str, item_text: str, context: dict | None = None) -> list[str]:
    """The user prompts of one profile: one per chunk (``profile_v3.split_chunks``), in order."""
    chunks = profile_v3.split_chunks(item_text or "")
    return [build_user(name, c, context, (i, len(chunks))) for i, c in enumerate(chunks)]


def input_hash(name: str, item_text: str) -> str:
    """The staleness hash a profile's cache key carries: the company and the whole text it was read from.

    The context (SIC, sector, segment note, candidates) is left out on purpose (review of PR #557): a Value Chain
    re-extract, a reclassification or a failed context read must not pay for the profile again. The LLM cache key
    is still over the exact bytes of every prompt sent."""
    from .store import prompt_hash
    return prompt_hash(SYSTEM, build_user(name, item_text))


def parse_profile(raw: str) -> dict:
    """The reply as the board's attribute rows (empty fields when unusable; never raises)."""
    return profile_v3.to_attributes(profile_v3.parse(raw))


class Profile(dict):
    """The attribute rows of a profile (``config.PROFILE_FIELDS``) plus its v3 JSON and per-field source."""

    def __init__(self, attrs: dict, v3: dict | None = None, labels: dict | None = None):
        super().__init__(attrs)
        self.v3 = v3
        self.labels = labels or {}


def token_budget(thinking: bool = False) -> int:
    """``max_tokens`` of a profile call (the ping sends this too, so it tests what a run sends). The v3 JSON with
    segments, quoted parties (``profile_v3.MAX_QUOTE``) and keywords is longer than v2's tag lists; a reply cut off
    at the limit does not parse and is paid twice, so the budget is generous. No thinking is sent (#400)."""
    return 8000


def profile_for(ticker: str, name: str, item_text: str, thinking: bool = False,
                effort: str | None = None, max_tokens: int | None = None,
                client=None, budget: "CallBudget | None" = None,
                provider: str = "deepseek", model: str | None = None,
                context: dict | None = None) -> Profile:
    """Call the writer once per chunk and return the merged profile (empty fields when unusable).

    ``provider`` picks the writer (``llm``); the prompt, the token budget and the parse are the same for every writer,
    so profiles made from the same filing by two writers hash to the same ``input_hash``. JSON mode, no thinking
    (#400): ``thinking`` / ``effort`` are accepted for old callers and not sent.

    A chunk whose answer does not parse into a v3 profile - a fresh one or an unparseable reply already in
    ``llm_cache`` - is retried once, bypassing the cache (within ``budget``), so a bad cached reply cannot pin the
    company forever. A reply is only cached when it parses (the ``validate`` hook).
    """
    tokens = max_tokens or token_budget()
    client_mod = llm(provider)
    users = prompts(name, item_text, context)

    def _call(user: str, use_cache: bool) -> str:
        if budget is not None and not budget.consume():
            raise BudgetExceeded()
        # The board's model (#360) only when one is chosen, so a caller's fake ``complete`` keeps its signature.
        extra = {"model": model} if model else {}
        return client_mod.complete(
            system_prompt(), user, thinking=False, reasoning_effort=None,
            max_tokens=tokens, json_mode=True, use_cache=use_cache, client=client,
            ticker=ticker, prompt_version=config.PROMPT_VERSION,
            validate=lambda c: profile_v3.parse(c) is not None, **extra)

    parts = []
    for user in users:
        got = profile_v3.parse(_call(user, True))
        if got is None:
            got = profile_v3.parse(_call(user, False))
        parts.append((got, len(user)))
    merged, labels = profile_v3.merge_chunks(parts)
    missing = [i for i, (got, _n) in enumerate(parts) if got is None]
    if merged is not None and missing:
        # A partial profile is kept and shown, and says which chunks it lacks; the engine redoes it within the
        # attempt rule (review of PR #557: a dropped chunk used to leave no trace and was never asked again).
        labels = {**labels, "missing_chunks": missing}
    return Profile(profile_v3.to_attributes(merged), merged, labels)
