"""LLM structured business profile for one company.

The prompt asks for a fixed JSON schema with short, lowercase tags — no prose —
so the local similarity is well-defined and comparable across companies.
"""
from __future__ import annotations

import json
import re
import threading

from RevenueChaser.core.providers import deepseek

from . import config

SYSTEM = (
    "You are a financial analyst. Given a company's business description, output "
    "a JSON object describing what the company actually does. Use short, lowercase "
    "noun phrases only (1-3 words). No sentences, no explanations. "
    "Schema: {\"products\": [...], \"end_markets\": [...], \"business_model\": [...], "
    "\"customer_types\": [...], \"value_chain_role\": [...], \"keywords\": [...]}. "
    "Every field is an array of strings; omit a field only if truly unknown."
)


class BudgetExceeded(Exception):
    """The run's LLM-call budget is spent; the caller leaves the group pending."""


class CallBudget:
    """A run's remaining LLM-call count, shared by the worker threads.

    ``max_calls`` of ``None`` means unlimited; ``0`` means no call at all. The
    budget counts **calls**, so a thinking retry (the second, uncached request in
    :func:`profile_for`) is counted too.
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


PROVIDERS = ("deepseek", "grok")


def llm(provider: str = "deepseek"):
    """The chat client module of a profile writer: ``deepseek`` (default, off-peak gated) or ``grok`` (SuperGrok,
    allowance gated, #388). Both expose ``complete`` (same signature) and ``MODEL``; ``complete`` is looked up at call
    time, so a test that patches ``deepseek.complete`` still reaches the profile path."""
    if provider == "deepseek":
        return deepseek
    if provider == "grok":
        from RevenueChaser.core.providers import grok
        return grok
    raise ValueError(f"unknown profile writer {provider!r}; one of {', '.join(PROVIDERS)}")


def system_prompt(version: str | None = None) -> str:
    """``SYSTEM`` with the prompt version appended.

    ``deepseek.complete`` hashes ``(system, user)`` for its cache key and does not
    take ``prompt_version``; appending the version here is what makes a
    ``PROMPT_VERSION`` bump miss the LLM cache instead of replaying the old reply.
    """
    return f"{SYSTEM}\n\nPrompt version: {version or config.PROMPT_VERSION}."


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


def build_user(name: str, item_text: str) -> str:
    """The user prompt: the company's name and its **whole** filing text (#317).

    ``config.MAX_INPUT_CHARS`` is a context ceiling, not a cost control, and it is at least what the splitter
    stores, so a stored slice reaches the writer uncut.
    """
    text = (item_text or "")[:config.MAX_INPUT_CHARS]
    return f"Company: {name}\n\nBusiness description (10-K Item 1 / 20-F Item 4):\n{text}"


def _clean(values) -> list[str]:
    out, seen = [], set()
    if isinstance(values, str):          # a lone string is a comma/semicolon list
        values = re.split(r"[,;\n]", values)
    if not isinstance(values, list):
        return out
    for v in values:
        s = re.sub(r"\s+", " ", str(v or "").strip().lower())
        s = s.strip(" .,;:-")
        if not s or len(s) > 60 or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= 30:
            break
    return out


def parse_profile(raw: str) -> dict:
    """Parse the model's JSON into the fixed schema (never raises)."""
    if isinstance(raw, str):
        raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(),
                     flags=re.IGNORECASE)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {f: [] for f in config.PROFILE_FIELDS}
    if not isinstance(data, dict):
        return {f: [] for f in config.PROFILE_FIELDS}
    return {f: _clean(data.get(f)) for f in config.PROFILE_FIELDS}


def token_budget(thinking: bool) -> int:
    """``max_tokens`` of a profile call: thinking spends tokens on reasoning, so it gets more (the ping sends this
    too, so it tests what a run sends)."""
    return 8000 if thinking else 1500


def profile_for(ticker: str, name: str, item_text: str, thinking: bool = False,
                effort: str | None = None, max_tokens: int | None = None,
                client=None, budget: "CallBudget | None" = None,
                provider: str = "deepseek") -> dict:
    """Call the LLM and return the parsed profile (empty fields when unusable).

    ``provider`` picks the writer (``llm``); the prompt, the token budget and the parse are the same for both, so a
    Grok profile hashes to the same ``input_hash`` as a DeepSeek one made from the same filing.

    Thinking mode spends tokens on reasoning that count against ``max_tokens``,
    so it gets a larger budget. An answer that does not parse into a profile —
    a fresh one or an unparseable reply already in ``llm_cache`` — is retried
    once, bypassing the cache (within ``budget``), so a bad cached reply cannot
    pin the company forever.

    The reply is only cached when it parses into a profile (the ``validate``
    hook), so the next run asks again instead of replaying it.
    """
    user = build_user(name, item_text)
    budget_tokens = max_tokens or token_budget(thinking)
    client_mod = llm(provider)

    def _call(use_cache: bool, tokens: int) -> str:
        if budget is not None and not budget.consume():
            raise BudgetExceeded()
        return client_mod.complete(
            system_prompt(), user, thinking=thinking, reasoning_effort=effort,
            max_tokens=tokens, json_mode=True, use_cache=use_cache, client=client,
            ticker=ticker, prompt_version=config.PROMPT_VERSION,
            validate=lambda c: any(parse_profile(c).values()))

    raw = _call(True, budget_tokens)
    prof = parse_profile(raw)
    if not any(prof.values()):
        raw = _call(False, budget_tokens * 2 if thinking else budget_tokens)
        prof = parse_profile(raw)
    return prof
