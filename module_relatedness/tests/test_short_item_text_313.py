"""#313: a filing text under Value Chain's ``MIN_ITEM_CHARS`` is never sent to the profile writer.

20 of the pilot's 1,503 names carry an empty Item 1 and 58 carry one under 2,000 characters (live store,
2026-10-30); a model asked from a 1-character description invents the profile. The guard refuses the call,
marks the name ``short_text`` (``no_text`` stays for an empty one), keeps a usable profile it already has,
and counts both apart from Pending.

Offline, temp store; no LLM and no SEC call (``profile_for`` is replaced).
"""
from __future__ import annotations

import json

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import config, engine, report, store
from RevenueChaser.module_relatedness.tests.test_relatedness import _prof, _seed_universe

TICKERS = ["AAA", "BBB"]
LONG = "We design gpu accelerators for data center customers. " * 60      # >= MIN_INPUT_CHARS


def _seed_filings(texts: dict[str, str]) -> None:
    rawstore.write_rows("chain_filings", [
        {"ticker": t, "cik": "", "form": "10-K", "filed": "2026-01-01", "accession": "a-" + t,
         "primary_doc": "u", "item_text": text, "quality": "ok", "fetched_at": 1.0}
        for t, text in texts.items()])


@pytest.fixture
def calls(rawstore_tmp_db, monkeypatch):
    """Every ticker the profile writer was asked for."""
    _seed_universe(TICKERS)
    monkeypatch.setattr(engine, "_cik_for", lambda ts: {t: "" for t in ts})
    seen: list[str] = []

    def fake_profile(ticker, name, text, **k):
        seen.append(ticker)
        return _prof(products=["gpu"], end_markets=["data center"])

    monkeypatch.setattr(engine.profiles_mod, "profile_for", fake_profile)
    return seen


def test_the_floor_is_value_chains_own():
    from RevenueChaser.module_value_chain import config as vc_config
    assert config.MIN_INPUT_CHARS == vc_config.MIN_ITEM_CHARS == 2000


def test_a_short_item_text_is_never_sent_to_the_writer(calls):
    _seed_filings({"AAA": "Advance Auto Parts, Inc.", "BBB": LONG})
    engine.build_profiles(TICKERS, usd_cap=1.0)
    assert calls == ["BBB"]
    assert store.profile_meta(["AAA"])["AAA"]["status"] == "short_text"
    assert not any(store.load_profiles(["AAA"])["AAA"].values())
    assert store.profile_meta(["BBB"])["BBB"]["status"] == "ok"


def test_a_text_at_the_floor_is_profiled(calls):
    _seed_filings({"AAA": "x" * config.MIN_INPUT_CHARS, "BBB": "y" * (config.MIN_INPUT_CHARS - 1)})
    engine.build_profiles(TICKERS, usd_cap=1.0)
    assert calls == ["AAA"]
    assert store.profile_meta(["BBB"])["BBB"]["status"] == "short_text"


def test_an_empty_text_is_still_no_text(calls):
    _seed_filings({"AAA": "", "BBB": "   "})
    engine.build_profiles(TICKERS, usd_cap=1.0)
    assert calls == []
    meta = store.profile_meta(TICKERS)
    assert (meta["AAA"]["status"], meta["BBB"]["status"]) == ("no_text", "no_text")


def test_a_short_text_never_overwrites_a_usable_profile(calls):
    _seed_filings({"AAA": "Advance Auto Parts, Inc.", "BBB": LONG})
    store.save_profile("AAA", "", _prof(products=["car parts"]), "k", "m")
    engine.build_profiles(TICKERS, usd_cap=1.0)
    assert calls == ["BBB"]
    assert store.load_profiles(["AAA"])["AAA"]["products"] == ["car parts"]
    assert store.profile_meta(["AAA"])["AAA"]["status"] == "ok"


# ── the status tile and the run's history ───────────────────────────────────
@pytest.fixture
def ran(rawstore_tmp_db, monkeypatch, tmp_path):
    """One usable profile, one name whose filing text is too short, one with none at all."""
    tickers = ["AAA", "BBB", "CCC"]
    _seed_universe(tickers)
    _seed_filings({"AAA": LONG, "BBB": "Advance Auto Parts, Inc.", "CCC": ""})
    monkeypatch.setattr(engine.config, "RELATEDNESS_JSON", tmp_path / "r.json")
    monkeypatch.setattr(engine.vc_engine, "fetch_corpus", lambda *a, **k: {"no_fetch": True})
    store.save_profile("AAA", "cik-AAA", _prof(products=["gpu"]), "k", "m")
    store.save_profile("BBB", "cik-BBB", _prof(), "", "m", status="short_text")
    store.save_profile("CCC", "cik-CCC", _prof(), "", "m", status="no_text")
    return tickers


def test_a_name_with_too_short_a_filing_text_is_not_pending(ran):
    payload = engine.run(no_fetch=True, tickers=ran)
    assert (payload["n"], payload["short_text"],
            payload["no_text"], payload["pending"]) == (1, 1, 1, 0)


def test_the_pending_tile_says_how_many_have_too_short_a_filing_text(ran):
    html = report.render_relatedness_dashboard(engine.run(no_fetch=True, tickers=ran))
    assert "1 with too short a filing text" in html
    none = report.render_relatedness_dashboard({"pending": 3, "no_text": 0, "short_text": 0})
    assert "too short a filing text" not in none


def test_the_fetch_records_the_short_text_count(ran, monkeypatch):
    monkeypatch.setattr(engine, "build_profiles", lambda tickers, **k: store.load_profiles(tickers))
    engine.run(no_fetch=False, tickers=ran, run_id="r1")
    reg = rawstore.get("run_registry", "relatedness_run")["value"]
    reg = reg if isinstance(reg, dict) else json.loads(reg)
    assert (reg["pending"], reg["no_text"], reg["short_text"]) == (0, 1, 1)
    (head,) = rawstore.query("SELECT context FROM result_runs WHERE board = 'module_relatedness'", db="history")
    ctx = head["context"] if isinstance(head["context"], dict) else json.loads(head["context"])
    assert (ctx["requested"], ctx["profiled"], ctx["no_text"], ctx["short_text"]) == (3, 1, 1, 1)
