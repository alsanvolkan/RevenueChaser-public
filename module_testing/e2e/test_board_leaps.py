"""LEAPS board, end to end: fetch → store → restart → refresh.

The first board wired into the harness, and the reference for the rest. It is a
good canary because it is UW-only (no yfinance), writes a table whose contents
are easy to reason about (``leaps_flow_prints``), and its refresh path is a
model-only re-render — exactly the "no network" claim under test.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest


@pytest.fixture
def fetched(dashboard):
    """A completed LEAPS fetch against the mock provider."""
    result = dashboard.run_job("/run/module_leaps")
    assert result.ok, f"LEAPS fetch failed: {result}\n{dashboard.logs()}"
    return result


class TestFetch:
    def test_the_dashboard_serves_its_shell(self, dashboard):
        """Sanity: the subprocess really is the app, not a stub."""
        r = dashboard.get("/")
        assert r.status_code == 200
        assert "LEAPS" in r.text

    def test_fetch_persists_prints_to_the_store(self, dashboard, fetched):
        assert dashboard.table_count("leaps_flow_prints") > 0

    def test_index_tickers_are_excluded_from_the_scan(self, dashboard, fetched):
        """SPX is in the fixture tape and must not become a candidate."""
        html = dashboard.view("module_leaps").text
        assert "NVDA" in html
        assert ">SPX<" not in html

    def test_fetch_writes_the_board_artifact(self, dashboard, fetched):
        assert dashboard.artifact("leaps_latest.html").exists()

    def test_the_board_shows_the_fetched_names(self, dashboard, fetched):
        html = dashboard.view("module_leaps").text
        assert "NVDA" in html and "AMD" in html

    def test_provenance_reports_a_data_date(self, dashboard, fetched):
        """`/api/data_asof` is the store-backed proof that a fetch landed."""
        assert dashboard.data_as_of("module_leaps") is not None

    def test_the_fetch_actually_called_the_provider(self, dashboard, provider_routes, fetched):
        assert provider_routes.requests("/api/option-trades"), \
            "fetch must hit the tape endpoint"


class TestRefreshIsOfflineAndFaithful:
    def test_refresh_rebuilds_a_deleted_artifact(self, dashboard, fetched, clear_cache):
        clear_cache()
        assert not dashboard.artifact("leaps_latest.html").exists()
        r = dashboard.view("module_leaps", refresh=True)
        assert r.status_code == 200
        assert "NVDA" in r.text

    def test_refresh_makes_no_network_calls(self, dashboard, provider_routes, fetched, clear_cache):
        """The contract in docs/data_architecture.md, asserted rather than assumed."""
        clear_cache()
        before = provider_routes.request_count
        r = dashboard.view("module_leaps", refresh=True)
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            "refresh reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}"
        )

    def test_refresh_survives_a_restart(self, dashboard, fetched, clear_cache):
        """Nothing in memory may be load-bearing — only .store survives a restart."""
        clear_cache()
        dashboard.restart()
        r = dashboard.view("module_leaps", refresh=True)
        assert r.status_code == 200
        assert "NVDA" in r.text

    def test_refresh_re_caches_the_artifact(self, dashboard, fetched, clear_cache):
        """Refresh must leave a served artifact behind, not just a response body."""
        clear_cache()
        dashboard.view("module_leaps", refresh=True)
        assert dashboard.artifact("leaps_latest.html").exists()


class TestEmptyStore:
    def test_a_board_with_no_data_renders_an_empty_state(self, dashboard):
        """No fetch has run: the tab must say so, not 500."""
        r = dashboard.view("module_leaps")
        assert r.status_code == 200
        assert "No LEAPS data yet" in r.text or "LEAPS" in r.text


class TestPinnedFootprint:
    """L1 (#377 LP-A): a pinned name's footprint is saved when its reads answer, and not when one failed."""

    def _pin(self, dashboard, ticker="NVDA"):
        r = dashboard.client.post(f"/api/watchlist/toggle/{ticker}")
        assert r.status_code == 200 and r.json()["in_watchlist"] is True

    def test_a_pinned_name_whose_reads_answer_is_cached(self, dashboard, provider_routes):
        self._pin(dashboard)
        result = dashboard.run_job("/run/module_leaps")
        assert result.ok, f"LEAPS fetch failed: {result}\n{dashboard.logs()}"
        assert provider_routes.requests("/api/stock/NVDA/oi-per-expiry")
        assert dashboard.table_count("leaps_cache") == 1

    def test_a_pinned_name_whose_oi_read_failed_is_not_cached(self, dashboard, provider_routes):
        provider_routes.route("/api/stock/NVDA/oi-per-expiry", payload={"message": "not found"}, status=404)
        self._pin(dashboard)
        result = dashboard.run_job("/run/module_leaps")
        assert result.ok, f"LEAPS fetch failed: {result}\n{dashboard.logs()}"
        assert provider_routes.requests("/api/stock/NVDA/oi-per-expiry")
        assert dashboard.table_count("leaps_cache") == 0


class TestHistoryBaselines:
    """L3 (#377): the label comes from the recorded sessions, and a Refresh re-derives it with no network."""

    def test_a_refresh_after_two_fetches_makes_no_request_and_leaves_the_cache(
            self, dashboard, provider_routes, clear_cache):
        r0 = dashboard.client.post("/api/watchlist/toggle/NVDA")
        assert r0.status_code == 200 and r0.json()["in_watchlist"] is True
        for _ in range(2):
            result = dashboard.run_job("/run/module_leaps")
            assert result.ok, f"LEAPS fetch failed: {result}\n{dashboard.logs()}"
        cached = dashboard.table_count("leaps_cache")
        assert cached > 0
        clear_cache()
        before = provider_routes.request_count
        r = dashboard.view("module_leaps", refresh=True)
        assert r.status_code == 200
        assert provider_routes.request_count == before, (
            "the Refresh re-deriving from the recorded sessions reached the network: "
            f"{[q.path for q in provider_routes.requests()[before:]]}"
        )
        assert dashboard.table_count("leaps_cache") == cached      # a Refresh writes nothing


class TestFilterKit:
    """KL (#377, Card E): the shared filter bar is on the served board, on Fetch and on Refresh, with no network."""

    def test_the_fetched_board_carries_the_bar_and_row_keys(self, dashboard, fetched):
        html = dashboard.view("module_leaps").text
        assert 'id="rc-filters"' in html and "RCFilters.mount(" in html
        assert 'data-ticker="NVDA"' in html and 'id="kit-count"' in html

    def test_refresh_keeps_the_bar_and_makes_no_network_calls(self, dashboard, provider_routes, fetched, clear_cache):
        clear_cache()
        before = provider_routes.request_count
        r = dashboard.view("module_leaps", refresh=True)
        assert r.status_code == 200
        assert 'id="rc-filters"' in r.text and 'data-ticker="NVDA"' in r.text
        assert provider_routes.request_count == before


# ── KL review (#377): the kit in a real browser ─────────────────────────────────────────────────────────────────
# The served board above proves the bar is there; this proves what it does. Headless Chrome (``--dump-dom``, as in
# test_board_html_browser.py) loads a rendered board whose filter fields are fixed here, with the shared state in the
# URL hash (a filter carried over from another board) or picked after load through ``RCF.set`` (the onChange path).
_FIELDS = {  # WL* pinned, U* scan: U1 and U2 High, U3 Quiet
    "WL1": ("Technology", "2026-12-01"), "WL2": ("Energy", None),
    "U1": ("Technology", None), "U2": ("Energy", None), "U3": ("Energy", None)}


def _chrome_path() -> str | None:
    override = os.environ.get("RC_CHROME")
    if override:
        return override if Path(override).exists() or shutil.which(override) else None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


@pytest.fixture
def kit_page(tmp_path, monkeypatch):
    """Render the board with fixed filter fields; ``dom(state=None, after_load_js="")`` → the DOM after the scripts."""
    chrome = _chrome_path()
    if chrome is None:
        msg = "no Chrome/Chromium for the LEAPS filter-kit check: set RC_CHROME"
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(msg)
        pytest.skip(msg)
    import RevenueChaser.module_leaps.report as ld
    from RevenueChaser.core import rawstore
    monkeypatch.setattr(rawstore, "DB_PATH", tmp_path / "raw.duckdb")      # no store file: reads come back empty
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", lambda ts: {
        t: {"ticker": t, "name": f"{t} Corp", "index": [], "market_cap": 5e10, "gics": [_FIELDS[t][0], ""],
            "theme_ids": [], "earnings_date": _FIELDS[t][1]} for t in ts})
    monkeypatch.setattr("RevenueChaser.core.universe.theme_catalog", lambda *a, **k: {})

    def snap(t, score):
        return {"ticker": t, "name": f"{t} Corp", "label": "Accumulating", "score": score, "frequency": 4,
                "notional": 4e5, "net_notional": 1e5, "ask_share": 0.7, "near_term_event": False}
    html = ld.render_leaps_dashboard([snap("WL1", 80), snap("WL2", 80)],
                                     [snap("U1", 70), snap("U2", 60), snap("U3", 5)]).read_text(encoding="utf-8")
    html = html.replace("<head>", "<head><script>window.fetch=function(){return new Promise(function(){});};"
                                  "</script>", 1)
    n = [0]

    def dom(state: dict | None = None, after_load_js: str = "") -> str:
        n[0] += 1
        page = tmp_path / f"leaps_{n[0]}.html"
        h = html
        if after_load_js:
            i = h.lower().rfind("</body>")
            h = (h[:i] + "<script>window.addEventListener('load',function(){setTimeout(function(){"
                 + after_load_js + "},0);});</script>" + h[i:])
        page.write_text(h, encoding="utf-8")
        url = page.as_uri() + ("#f=" + quote(json.dumps(state)) if state else "")
        out = subprocess.run(
            [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--disable-extensions",
             f"--user-data-dir={tmp_path / f'profile_{n[0]}'}",
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
             "--virtual-time-budget=3000", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", timeout=90)
        assert "<html" in out.stdout.lower(), f"Chrome dumped no DOM: {out.stderr[-1500:]}"
        return out.stdout
    return dom


def _state(**kw) -> dict:
    s = {"v": 1, "index": "all", "mc": {"b": [], "min": None, "max": None}, "gics": None, "theme": None,
         "earn": "any", "earnFilter": False}
    s.update(kw)
    return s


def _hidden(dom: str) -> dict[str, bool]:
    """ticker → hidden by the kit, for every table row of every tier (the pin buttons are not rows)."""
    out = {}
    for tr in re.findall(r"<tr\b[^>]*\bdata-ticker=[^>]*>", dom):
        t = re.search(r'data-ticker="([^"]*)"', tr).group(1)
        cls = re.search(r'class="([^"]*)"', tr)
        out[t] = bool(cls and "kit-hide" in cls.group(1).split())
    return out


def _tier_n(dom: str, tier: str) -> str:
    return re.search(rf'id="tier-{tier}".*?<span class="n"[^>]*>([^<]*)</span>', dom, re.S).group(1)


def _count_line(dom: str) -> str:
    return re.search(r'id="kit-count"[^>]*>([^<]*)<', dom).group(1)


class TestFilterKitInTheBrowser:
    def test_a_carried_over_sector_hides_rows_in_every_tier_watchlist_included(self, kit_page):
        dom = kit_page(_state(gics={"key": "Technology", "label": "Technology", "level": 1}))
        assert _hidden(dom) == {"WL1": False, "WL2": True, "U1": False, "U2": True, "U3": True}
        assert _tier_n(dom, "high") == "1/2" and _tier_n(dom, "quiet") == "0/1"
        assert _tier_n(dom, "watchlist") == "1/2"
        assert _count_line(dom) == "2 of 5 names"

    def test_a_sector_picked_on_the_page_runs_through_onchange(self, kit_page):
        dom = kit_page(after_load_js="RCF.set({gics:{key:'Energy',label:'Energy',level:1}});")
        assert _hidden(dom) == {"WL1": True, "WL2": False, "U1": True, "U2": False, "U3": False}
        assert _count_line(dom) == "3 of 5 names" and _tier_n(dom, "watchlist") == "1/2"
        assert _tier_n(dom, "high") == "1/2"

    def test_no_filter_hides_nothing_and_shows_no_count_line(self, kit_page):
        dom = kit_page()
        assert _hidden(dom) == dict.fromkeys(_FIELDS, False)
        assert _count_line(dom) == "" and _tier_n(dom, "high") == "2"

    def test_the_bar_counts_every_name_it_filters_watchlist_included(self, kit_page):
        dom = kit_page()
        hint = re.search(r'data-v="all"[^>]*>All<span class="rcf-hint">([^<]*)<', dom).group(1)
        assert hint == "5"

    def test_an_earnings_week_from_another_board_does_not_filter_leaps(self, kit_page):
        """LEAPS has no Earnings facet: a week picked on Co-movement neither hides rows nor shows a chip here."""
        dom = kit_page(_state(earn="0"))
        assert _hidden(dom) == dict.fromkeys(_FIELDS, False)
        assert _count_line(dom) == ""
        chips = re.search(r'<div class="rcf-active"[^>]*>(.*?)</div>', dom, re.S).group(1)
        assert "No filters." in chips and "Earnings:" not in chips
