"""#317: the whole sliced Item 1 / Item 4 reaches the profile writer.

``build_user`` used to cut the filing text at 12,000 characters as a cost control, so for a large filer the
writer never saw the segment, customer-concentration and competition sections the profile schema is built
from. The limit is gone (avo 2026-10-30); what is left is a hard ceiling for the model's **context**, large
enough that a stored slice is never truncated (the splitter caps what it stores at
``module_value_chain.config.MAX_ITEM_CHARS``) and small enough to fit the writer's context with the answer.

Offline, no store and no LLM: the prompt builder and the constants only, plus one engine run whose writer is
replaced by a recorder.
"""
from __future__ import annotations

import pytest

from RevenueChaser.module_relatedness import config, engine, profiles
from RevenueChaser.module_relatedness.tests.test_relatedness import _prof, _seed_universe
from RevenueChaser.module_value_chain import config as vc_config

SENTENCE = "We design gpu accelerators for data center customers. "


def _body(chars: int) -> str:
    """A filing-like text of about ``chars`` characters, ending in a distinctive sentence."""
    tail = "Our largest customer accounted for 14% of net revenue."
    head = SENTENCE * max(1, (chars - len(tail)) // len(SENTENCE))
    return head + tail


def test_the_whole_item_1_reaches_the_writer():
    text = _body(60_000)                       # five times the old 12,000-character cut
    user = profiles.build_user("Nvidia Corporation", text)
    assert text in user
    assert user.endswith("Our largest customer accounted for 14% of net revenue.")


def test_the_ceiling_is_the_splitters_own_store_cap():
    """The ceiling is explicit and at least what the store can hold, so a stored slice is never cut."""
    assert config.MAX_INPUT_CHARS == vc_config.MAX_ITEM_CHARS == 200_000


def test_a_text_above_the_ceiling_is_still_cut():
    text = _body(config.MAX_INPUT_CHARS + 50_000)
    user = profiles.build_user("Huge Filer Inc", text)
    assert text[:config.MAX_INPUT_CHARS] in user
    assert not user.endswith("Our largest customer accounted for 14% of net revenue.")


def test_the_ceiling_fits_the_writers_context_with_the_answer():
    """Ceiling + answer budget stay inside the writer's context window, both stated in config."""
    prompt_tokens = config.MAX_INPUT_CHARS / config.CHARS_PER_TOKEN
    assert prompt_tokens + profiles.token_budget(thinking=True) < config.WRITER_CONTEXT_TOKENS


def test_the_per_call_estimate_was_raised_with_the_input():
    """``EST_USD_PER_CALL`` drives the ``$`` cap; 0.00143 was measured on the 12,000-character prompt."""
    assert config.EST_USD_PER_CALL > 0.00143


def test_an_engine_run_sends_the_long_text_whole(rawstore_tmp_db, monkeypatch):
    from RevenueChaser.core import rawstore

    _seed_universe(["AAA"])
    text = _body(60_000)
    rawstore.write_rows("chain_filings", [
        {"ticker": "AAA", "cik": "", "form": "10-K", "filed": "2026-01-01", "accession": "a-AAA",
         "primary_doc": "u", "item_text": text, "quality": "ok", "fetched_at": 1.0}])
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: "" for t in ts})
    seen: list[str] = []

    def fake_profile(ticker, name, item_text, **k):
        seen.append(item_text)
        return _prof(products=["gpu"])

    monkeypatch.setattr(engine.profiles_mod, "profile_for", fake_profile)
    engine.build_profiles(["AAA"], usd_cap=1.0)
    assert seen == [text]
    assert text in profiles.build_user("Nvidia Corporation", text)
