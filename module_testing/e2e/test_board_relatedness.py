"""Relatedness board, end to end (#114, #118 C29).

The mock provider server stands in for DeepSeek (``RC_DEEPSEEK_BASE``), so the
tests can count every ``/chat/completions`` request the dashboard subprocess
makes. ``e2e_env`` is overridden here rather than in ``conftest.py``: the dummy
``DEEPSEEK_API_KEY`` must be set explicitly, otherwise ``core.config`` would
``load_dotenv`` the developer's real key.
"""
from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import time

import pytest

from RevenueChaser.module_relatedness import config
from RevenueChaser.module_testing.e2e.harness.dashboard_driver import REPO_PARENT

_XSS = "</script><img src=x onerror=alert(1)>"
# Long enough for the profile floor (#313, ``config.MIN_INPUT_CHARS``): the engine sends no shorter Item 1
# to the writer, so a seed under it would make every Fetch below spend no call at all.
_TEXT = ("Item 1. Business\nWe design and sell products to customers.\n"
         + "This annual report describes the segments, the customers and the competition. " * 30)


@pytest.fixture
def e2e_env(e2e_env, provider_routes):
    return {**e2e_env, "RC_DEEPSEEK_BASE": provider_routes.url,
            "DEEPSEEK_API_KEY": "e2e-test-token", "RC_DEEPSEEK_OFF_PEAK_ONLY": "0"}


def _profiles():
    out = {}
    for i in range(30):
        out[f"G{i}"] = {"products": ["gpu", "ai"], "end_markets": ["data center"],
                        "business_model": ["semis"], "customer_types": ["hyperscaler"],
                        "value_chain_role": ["designer"], "keywords": ["accelerated"]}
        out[f"B{i}"] = {"products": ["loans", "deposits"], "end_markets": ["retail"],
                        "business_model": ["interest"], "customer_types": ["consumer"],
                        "value_chain_role": ["lender"], "keywords": ["branch"]}
        out[f"H{i}"] = {"products": ["drugs", "biologics"], "end_markets": ["patients"],
                        "business_model": ["royalty"], "customer_types": ["hospital"],
                        "value_chain_role": ["manufacturer"], "keywords": ["clinical"]}
    return out


def _empty_profile_llm(provider_routes):
    """DeepSeek stand-in that always answers ``{}``: valid JSON, no usable profile."""
    provider_routes.route(
        "/chat/completions", with_body=True,
        payload=lambda path, body: {"choices": [{"message": {"content": "{}"}}]})


def _llm_calls(provider_routes) -> int:
    return len(provider_routes.requests("/chat/completions"))


def _data(html: str) -> list[dict]:
    return json.loads(html.split("var DATA = ", 1)[1].split(";\nvar PEERS", 1)[0])


def _fetch(dashboard) -> None:
    """The Relatedness Fetch is the off-peak CLI (``--run``), not a dashboard job: run
    the real entry point in a subprocess on the dashboard's store, cache and mock."""
    env = {**os.environ, **dashboard.env, "RC_STORE_DIR": str(dashboard.store_dir),
           "RC_CACHE_DIR": str(dashboard.cache_dir), "RC_STORE_AUTO_ENSURE": "1",
           "PYTHONUNBUFFERED": "1"}
    done = subprocess.run([sys.executable, "-m", "RevenueChaser.module_relatedness", "--run"],
                          cwd=str(REPO_PARENT), env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"


@pytest.fixture
def store(dashboard, monkeypatch):
    """The dashboard's temp store, opened from the test process (dashboard idle)."""
    import RevenueChaser.core.rawstore as rawstore
    monkeypatch.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    monkeypatch.setattr("RevenueChaser.core.config.MARKET_DIR", dashboard.env["RC_MARKET_DIR"])  # the prices store (G3)
    return rawstore


def _seed_universe(rawstore, tickers):
    """Universe rows, a CIK map and a cached filing per ticker, so ``fetch_corpus``
    and ``build_profiles`` have nothing to fetch from SEC."""
    rawstore.write_rows("equity_universe", [
        {"ticker": t, "name": t + " Inc", "market_cap": 1e10,
         "sector": "Technology", "industry": "Semiconductors"} for t in tickers])
    rawstore.write_rows("sec_exchange_file", [     # #115: the CIK map production reads (#301 D21)
        {"ticker": t, "cik": str(1000 + i), "name": t, "exchange": "Nasdaq", "valid_from": None, "status": "present",
         "source": "sec_exchange", "fetched_at": 1.0, "run_id": "seed", "run_at": 1.0}
        for i, t in enumerate(tickers)])
    rawstore.write_rows("chain_filings", [{
        "ticker": t, "cik": "1000", "form": "10-K", "filed": "2026-02-01",
        "accession": "acc-" + t, "primary_doc": "http://x", "item_text": _TEXT,
        "quality": "ok", "fetched_at": time.time()} for t in tickers])


def _seed_profiles(rawstore, profs, cache_key="k", prompt_version=config.PROMPT_VERSION):
    from RevenueChaser.module_relatedness import store
    for t, p in profs.items():
        store.save_profile(t, "", p, cache_key, "deepseek-flash", prompt_version)


def _small_build(monkeypatch):
    from RevenueChaser.module_relatedness import themes
    monkeypatch.setattr(themes, "build_shadow", functools.partial(
        themes.build_shadow, candidates=(2, 3), k_medium=2, k_coarse=2))
    return themes


@pytest.fixture
def rebuilt(dashboard, store, monkeypatch):
    """Two rebuilds through the real CLI pipeline (never ``relabel_from_tags`` by hand)."""
    from RevenueChaser.module_relatedness.__main__ import main

    profs = _profiles()
    _seed_universe(store, profs)
    _seed_profiles(store, profs)
    _small_build(monkeypatch)
    assert main(["--rebuild-themes", "--run-id", "first"]) == 0
    assert main(["--rebuild-themes", "--run-id", "second"]) == 0     # the rebuild
    return dashboard


def test_board_renders_after_a_rebuild(rebuilt, store):
    r = rebuilt.view("module_relatedness", refresh=True)
    assert r.status_code == 200
    assert "Relatedness" in r.text and "G0" in r.text and "B0" in r.text
    data = _data(r.text)
    assert data and all(c["theme_coarse"] for c in data)
    # Cards carry the sector (Morningstar name) and the industry text for the filter cascade (#266).
    assert all(c["sector"] == "Technology" and c["industry"] == "Semiconductors" for c in data)
    assert all("sub_industry" not in c and "industry_group" not in c for c in data)
    # Card 10 / REL-1: the cutover keeps the older run; the board reads the current one.
    runs = {row["id"]: row["is_current"] for row in store.get_all("theme_runs")}
    assert runs == {"first": 0, "second": 1}
    kept = {row["theme_run_id"] for row in store.get_all("ticker_themes")}
    assert {"first", "second"} <= kept
    # The run's own result, so what the coarse assertions rely on is stated: three fine themes here, a medium cut
    # held one below them (one pair of fines merged, one single fine child), and two coarse themes (#433).
    run = store.get("theme_runs", "second")
    assert (run["k_fine"], run["k_medium"], run["k_coarse"]) == (3, 2, 2)
    # The card's label comes from the one Theme vocabulary (core.universe.theme_catalog, #118 C24), which renames a
    # sector-named coarse theme to its largest medium child; the stored copy is the fallback when it has no row.
    from RevenueChaser.core.universe import theme_catalog
    catalog = theme_catalog()
    coarse = {row["ticker"]: (catalog.get(row["theme_id"]) or {}).get("label", row["theme"])
              for row in store.get_all("ticker_themes")
              if row["theme_run_id"] == "second" and row["level"] == "coarse"}
    assert all(c["theme_coarse"] == coarse[c["ticker"]] for c in data)
    # One literal, so the expectation does not only mirror the engine (R2 review S3): both coarse themes are
    # sector-named "Technology" (the second suffixed " (2)" by unique_theme_labels), and the card shows its largest
    # medium child instead. The medium cut pairs the bank and healthcare fines, so that coarse's largest child is
    # the merged theme; the semiconductor fine kept a single child and its own name.
    stored = {row["ticker"]: row["theme"] for row in store.get_all("ticker_themes")
              if row["theme_run_id"] == "second" and row["level"] == "coarse"}
    by_ticker = {c["ticker"]: c for c in data}
    assert stored["G0"].startswith("Technology") and stored["B0"].startswith("Technology")
    assert set(stored.values()) == {"Technology", "Technology (2)"}
    assert by_ticker["G0"]["theme_coarse"] == "accelerated, data center, designer"
    # The bank and healthcare fines are equal in size and in tag counts, so the offline label of their merged medium
    # theme is a tie between the two groups' top tags. Since #20 the tie resolves to the child with the lowest
    # theme_id -- but *which* group that is is not this test's to pin: the fixture's three groups give exactly tied
    # singular values, so which one NMF numbers f0 is down to the LAPACK build (that, not the scan order, is what
    # split the Windows and the Linux run in #20). The rule itself is pinned in
    # module_relatedness/tests/test_theme_label_determinism_20.py, where the theme_ids are fixed. The test states
    # what is guaranteed here: the merged theme carries one of the two groups' own labels, and both groups' cards
    # show the same one.
    merged = {"branch, consumer, deposits", "biologics, clinical, drugs"}
    assert by_ticker["B0"]["theme_coarse"] in merged
    assert by_ticker["H0"]["theme_coarse"] == by_ticker["B0"]["theme_coarse"]
    assert rebuilt.table_count("ticker_themes") > 0
    assert rebuilt.table_count("relatedness_theme_etfs") >= 0


def test_rebuild_fills_the_theme_facet(rebuilt):
    """#118 N1: the CLI rebuild alone leaves no blank labels on the board. Since #383 C1b the Theme filter is the
    shared kit's facet: its catalog is the pinned run, and every card carries that run's ``theme_ids``."""
    html = rebuilt.view("module_relatedness", refresh=True).text
    assert '<select id="theme"' not in html
    meta = json.loads(html.split("var META = ", 1)[1].split(";\n", 1)[0])
    assert meta["themes"], "the Theme facet has no themes"
    assert all(t["label"] for t in meta["themes"].values())
    assert {t["level"] for t in meta["themes"].values()} == {"coarse", "medium", "fine"}
    assert all(i.startswith("second:") or i == "second" for i in meta["themes"])     # the pinned run only
    data = _data(html)
    assert all(c["theme_fine"] and c["theme_medium"] for c in data)
    assert all(c["theme_ids"] and set(c["theme_ids"]) <= set(meta["themes"]) for c in data)


def test_refresh_is_pure(rebuilt, provider_routes, store):
    """Refresh makes zero provider requests and writes no scores or registry rows."""
    store.write_rows("run_registry", [{"key": "relatedness_run", "value": json.dumps(
        {"as_of": "2026-10-01T00:00:00+00:00", "n": 60, "pending": 0,
         "next_off_peak": None})}])
    store.write_rows("relatedness_scores", [
        {"ticker": "G0", "peer": "G1", "score": 0.9}, {"ticker": "B0", "peer": "B1", "score": 0.8}])
    provider_routes.reset_requests()
    registry = store.get("run_registry", "relatedness_run")
    n_scores = rebuilt.table_count("relatedness_scores")
    assert registry and n_scores == 2

    for _ in range(2):
        assert rebuilt.view("module_relatedness", refresh=True).status_code == 200
    assert provider_routes.request_count == 0              # every path, not only DeepSeek
    assert store.get("run_registry", "relatedness_run") == registry
    assert rebuilt.table_count("relatedness_scores") == n_scores


def test_mode_change_never_wipes_profiles(dashboard, store, provider_routes):
    """D4: profiles made under another prompt stay visible and stale; a Fetch that gets
    no usable answer keeps them."""
    profs = {t: p for t, p in list(_profiles().items())[:6]}
    _seed_universe(store, profs)
    _seed_profiles(store, profs, cache_key=json.dumps(
        {"model": "deepseek-flash", "thinking": 1, "effort": "high",
         "prompt_version": "v0", "ticker": "X", "input_hash": "h"}, sort_keys=True),
        prompt_version="v0")
    _empty_profile_llm(provider_routes)

    _fetch(dashboard)
    assert _llm_calls(provider_routes) > 0                  # the stale ones were retried
    assert dashboard.table_count("relatedness_profiles") == len(profs)
    assert all(m["status"] == "ok" for m in store.get_all("relatedness_profiles"))
    data = _data(dashboard.view("module_relatedness", refresh=True).text)
    assert len(data) == len(profs) and all(c["stale"] for c in data)


def test_empty_profile_is_retried_at_most_three_runs(dashboard, store, provider_routes):
    """D2 (the Fetch is the CLI): an empty answer is retried in 3 runs, then left alone. P2: an empty reply is
    never cached, so each of the 3 runs costs the first call plus the uncached thinking retry -> 2, 2, 2, and the
    group is left alone (0) on the fourth run."""
    _seed_universe(store, ["EMP1"])
    _empty_profile_llm(provider_routes)

    per_run = []
    for _ in range(4):
        before = _llm_calls(provider_routes)
        _fetch(dashboard)
        per_run.append(_llm_calls(provider_routes) - before)
    assert per_run == [2, 2, 2, 0]
    row = store.get("relatedness_profiles", "EMP1")
    assert row["status"] == "empty" and row["attempts"] == 3


def test_markup_in_theme_names_is_escaped_on_the_relatedness_board(rebuilt, store):
    """A theme label with ``</script>`` in it never reaches the Relatedness page raw.

    Co-movement and Sector Rotation need their own seeded data to render a theme control
    (Co-movement a payload from a Fetch, Rotation a snapshot), so they are not driven
    here: Rotation's sink has a unit test (``test_theme_focus.py``), Co-movement's is
    still uncovered (follow-up on #118).
    """
    rows = store.get_all("relatedness_themes")
    store.write_rows("relatedness_themes", [{**r, "label": _XSS} for r in rows])
    store.execute("UPDATE ticker_themes SET theme = ?", [_XSS])
    r = rebuilt.view("module_relatedness", refresh=True)
    assert r.status_code == 200
    assert "</script><img" not in r.text
    # Since #383 C1b the Theme filter is the shared kit's facet: the label reaches the page only inside the META
    # JSON (script_json escapes "<"), and the kit escapes it (rcEsc) before it writes markup.
    meta = json.loads(r.text.split("var META = ", 1)[1].split(";\n", 1)[0])
    assert any(t["label"] == _XSS for t in meta["themes"].values())
    data = _data(r.text)                                   # the JSON blob decodes to the label
    assert data and all(c[f"theme_{lv}"] == _XSS for c in data
                        for lv in ("coarse", "medium", "fine"))
