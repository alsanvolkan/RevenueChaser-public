"""#388 D5 (REL-3 + REL-5, DF61): a profile is redone when its input changes, the prompt names the company, and the
prompt is v2.

- **Input hash:** a stored profile carries the hash of the exact prompt it was made from (``cache_key.input_hash``).
  ``build_profiles`` re-profiles a group whose filing text (or company name) changed since, and leaves one whose
  input is the same alone. Before D5 a usable profile in the current mode was never redone, whatever the filing said.
- **Company name:** the prompt says ``Company: <name from equity_universe>``; it used to say the ticker
  (``chain_filings`` has no name column, so ``_filing_text`` fell back to it).
- **v2:** ``PROMPT_VERSION`` moves to "v2", so every v1 profile reads stale (kept and badged until redone).

Offline, temp store: ``profile_for`` / ``deepseek.complete`` are fakes, nothing reaches DeepSeek or SEC.
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import deepseek
from RevenueChaser.module_relatedness import config, engine, store
from RevenueChaser.module_relatedness.tests.test_relatedness import _prof, _seed_universe

TICKERS = ["AAA", "BBB", "CCC"]


def _seed_filings(texts: dict[str, str]) -> None:
    rawstore.write_rows("chain_filings", [
        {"ticker": t, "cik": "", "form": "10-K", "filed": "2026-01-01", "accession": "a-" + t,
         "primary_doc": "u", "item_text": text, "quality": "ok", "fetched_at": 1.0}
        for t, text in texts.items()])


@pytest.fixture
def calls(rawstore_tmp_db, monkeypatch):
    """Every LLM profile request: (ticker, name, text)."""
    _seed_universe(TICKERS)
    _seed_filings({t: f"{t} makes gpus for data centers" for t in TICKERS})
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: "" for t in ts})
    seen: list[tuple[str, str, str]] = []

    def fake_profile(ticker, name, text, **k):
        seen.append((ticker, name, text))
        return _prof(products=[f"{ticker.lower()} gpu"], end_markets=["data center"])

    monkeypatch.setattr(engine.profiles_mod, "profile_for", fake_profile)
    return seen


def test_the_prompt_is_v2():
    assert config.PROMPT_VERSION == "v2" and store.PROMPT_VERSION == "v2"


def test_the_same_input_is_not_profiled_again(calls):
    engine.build_profiles(TICKERS)
    assert sorted(t for t, _, _ in calls) == TICKERS
    calls.clear()
    engine.build_profiles(TICKERS)
    assert calls == []


def test_a_new_filing_text_is_profiled_again(calls):
    engine.build_profiles(TICKERS)
    calls.clear()
    _seed_filings({"BBB": "BBB now sells banking software to regional banks"})
    engine.build_profiles(TICKERS)
    assert [(t, text) for t, _, text in calls] == [("BBB", "BBB now sells banking software to regional banks")]
    calls.clear()
    engine.build_profiles(TICKERS)                                    # the new profile holds the new hash
    assert calls == []


def test_a_new_company_name_is_profiled_again(calls):
    engine.build_profiles(TICKERS)
    calls.clear()
    rawstore.execute("UPDATE equity_universe SET name = 'Triple C Holdings' WHERE ticker = 'CCC'")
    engine.build_profiles(TICKERS)
    assert [(t, n) for t, n, _ in calls] == [("CCC", "Triple C Holdings")]


def test_a_changed_input_waits_for_the_cap_like_any_other(calls, monkeypatch):
    monkeypatch.setattr(engine.config, "EST_USD_PER_CALL", 0.001)
    engine.build_profiles(TICKERS)
    calls.clear()
    _seed_filings({t: f"{t} changed its business" for t in TICKERS})
    engine.build_profiles(TICKERS, usd_cap=0.002)                     # -> 2 calls this run
    assert len(calls) == 2
    calls.clear()
    engine.build_profiles(TICKERS, usd_cap=0.002)                     # the third, next run
    assert len(calls) == 1


def test_a_text_gone_keeps_the_profile(calls):
    """No filing text any more: the usable profile stays, and nothing is asked."""
    engine.build_profiles(TICKERS)
    calls.clear()
    _seed_filings({"AAA": ""})
    engine.build_profiles(TICKERS)
    assert calls == [] and any(store.load_profiles(["AAA"])["AAA"].values())


def test_the_prompt_names_the_company(rawstore_tmp_db, monkeypatch):
    _seed_universe(["AAA"])
    _seed_filings({"AAA": "AAA makes gpus for data centers"})
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: "" for t in ts})
    users = []

    def fake_complete(system, user, **k):
        users.append(user)
        return json.dumps({"products": ["gpu"], "end_markets": ["data center"]})

    monkeypatch.setattr(deepseek, "complete", fake_complete)
    engine.build_profiles(["AAA"])
    assert users and users[0].startswith("Company: AAA Inc\n")


def test_a_name_missing_from_the_universe_falls_back_to_the_ticker(calls):
    rawstore.execute("DELETE FROM equity_universe WHERE ticker = 'AAA'")
    engine.build_profiles(["AAA"])
    assert calls[0][:2] == ("AAA", "AAA")


def test_v1_profiles_read_stale_and_are_redone(calls):
    v1 = store.cache_key(deepseek.MODEL, config.THINKING, config.REASONING_EFFORT, "v1", "AAA", "h")
    store.save_profile("AAA", "", _prof(products=["old"]), v1, deepseek.MODEL, prompt_version="v1")
    assert store.stale_tickers(["AAA"]) == {"AAA"}
    engine.build_profiles(["AAA"])
    assert [t for t, _, _ in calls] == ["AAA"]
    assert store.stale_tickers(["AAA"]) == set()


def test_input_hash_reads_the_stored_key():
    ck = store.cache_key("m", True, "high", "v2", "AAA", "abc")
    assert store.input_hash(ck) == "abc"
    assert store.input_hash(json.loads(ck)) == "abc"                  # rawstore decodes JSON text on read
    assert store.input_hash("") is None and store.input_hash("not json") is None
