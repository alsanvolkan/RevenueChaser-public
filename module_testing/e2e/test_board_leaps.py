"""LEAPS board, end to end: options store -> Fetch -> restart -> Refresh (G9, build plan PR 19).

The footprints and prints are a fixture ``options.duckdb`` in the dashboard's market root, as ``uw/option_list``
serves it, for names put on the option watch list through the dashboard's own route. The session is today and the
expiries are relative to today, because the scan's print window and the far line are date-based. Neither the Fetch
nor the Refresh calls a provider, and the old store's ``leaps_cache`` and ``leaps_flow_prints`` are not written.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest

from RevenueChaser.module_leaps.tests.options_store import print_row, store_footprint

_PANEL = {"X-RC-Panel": "leaps"}


def _toggle(dashboard, kind, ticker):
    path = f"/api/watchlist/option/toggle/{ticker}" if kind == "option" else f"/api/watchlist/toggle/{ticker}"
    r = dashboard.client.post(path, headers=_PANEL)
    assert r.status_code == 200, r.text


@pytest.fixture
def stored(dashboard):
    """NVDA and AMD on the option watch list with far-dated large prints today, NVDA pinned; SPX prints too (an
    index, never a row); TSLA pinned off the option list, with stored rows that must not be read."""
    today = date.today()
    far, far2 = today + timedelta(days=400), today + timedelta(days=500)
    market = Path(dashboard.env["RC_MARKET_DIR"])
    for t in ("NVDA", "AMD"):
        _toggle(dashboard, "option", t)
    for t in ("NVDA", "TSLA"):
        _toggle(dashboard, "stock", t)
    store_footprint("NVDA", today, {far: (900, 100), far2: (300, 0)}, market=market,
                    prints=[print_row("NVDA", "n1", today, far), print_row("NVDA", "n2", today, far2)])
    store_footprint("AMD", today, {far: (500, 500)}, market=market,
                    prints=[print_row("AMD", "a1", today, far, premium=300_000.0)])
    store_footprint("SPX", today, {far: (10, 10)}, market=market, prints=[print_row("SPX", "s1", today, far)])
    store_footprint("TSLA", today, {far: (900, 0)}, market=market, prints=[print_row("TSLA", "t1", today, far)])
    return dashboard


@pytest.fixture
def fetched(stored, provider_routes):
    """A completed LEAPS Fetch over the stored fixture."""
    result = stored.run_job("/run/module_leaps")
    assert result.ok, f"LEAPS fetch failed: {result}\n{stored.logs()}"
    return result


class TestFetch:
    def test_the_dashboard_serves_its_shell(self, dashboard):
        """Sanity: the subprocess really is the app, not a stub."""
        r = dashboard.get("/")
        assert r.status_code == 200
        assert "LEAPS" in r.text

    def test_index_tickers_are_excluded_from_the_scan(self, dashboard, fetched):
        """SPX is on the list and has a far-dated print: it must not become a candidate."""
        html = dashboard.view("module_leaps").text
        assert "NVDA" in html
        assert ">SPX<" not in html

    def test_fetch_writes_the_board_artifact(self, dashboard, fetched):
        assert dashboard.artifact("leaps_latest.html").exists()

    def test_the_board_shows_the_stored_names(self, dashboard, fetched):
        html = dashboard.view("module_leaps").text
        assert 'data-ticker="NVDA"' in html and 'data-ticker="AMD"' in html

    def test_a_pinned_name_off_the_list_shows_the_label(self, dashboard, fetched):
        html = dashboard.view("module_leaps").text
        block = html[html.index('data-ticker="TSLA"'):]
        assert "not on the option watch list" in block[:block.index("</tr>")]

    def test_provenance_reports_a_data_date(self, dashboard, fetched):
        assert dashboard.data_as_of("module_leaps") == date.today().isoformat()

    def test_the_fetch_is_a_builder_request_and_calls_no_provider(self, dashboard, provider_routes, fetched):
        assert provider_routes.request_count == 0, [q.path for q in provider_routes.requests()]
        assert "Builder request: uw/option_list not queued" in dashboard.view("module_leaps").text

    def test_nothing_is_written_to_the_old_store(self, dashboard, fetched):
        assert dashboard.table_count("leaps_cache") == 0 and dashboard.table_count("leaps_flow_prints") == 0


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
        """Nothing in memory may be load-bearing: only the stores survive a restart."""
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
