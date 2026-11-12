"""LEAPS board, end to end: the scan is switched off (#730).

Until 12 Nov 2026 this file drove the whole fetch -> store -> restart -> refresh path; the board was the
first one wired into the harness. The scan is now off (avo's decision card, option "B: Kapat"): the job is
not registered and ``POST /run/module_leaps`` answers 409, so there is no fetch to drive. What is left is
the refusal itself, the empty board, and the shared filter bar's tier behaviour in a real browser (the
only board with tiers), which renders the report directly and never asks the dashboard for a scan.
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

from . import harness


class TestScanIsSwitchedOff:
    def test_the_dashboard_serves_its_shell(self, dashboard):
        """Sanity: the subprocess really is the app, not a stub."""
        r = dashboard.get("/")
        assert r.status_code == 200
        assert "Earnings Radar" in r.text

    def test_the_fetch_route_refuses_with_a_reason(self, dashboard):
        r = dashboard.post_fetch("/run/module_leaps")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["ok"] is False
        assert "switched off" in body["error"] and "#730" in body["error"]

    def test_the_refusal_starts_no_job_and_calls_no_provider(self, dashboard, provider_routes):
        before = provider_routes.request_count
        dashboard.post_fetch("/run/module_leaps")
        dashboard.wait_until_idle()
        assert provider_routes.request_count == before
        assert dashboard.table_count("leaps_flow_prints") == 0
        assert dashboard.table_count("leaps_cache") == 0

    def test_the_board_with_no_data_renders_instead_of_failing(self, dashboard):
        """No scan can run, so the tab must still answer 200 rather than 500."""
        r = dashboard.view("module_leaps")
        assert r.status_code == 200


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
            capture_output=True, text=True, encoding="utf-8", timeout=harness.CHROME_TIMEOUT_S)
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
