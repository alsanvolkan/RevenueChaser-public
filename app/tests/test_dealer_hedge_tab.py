"""Dealer Hedge (demo) tab: a static teaching page (app/dealer_hedge.py, /view/dealer_hedge, #376).

No data, no Fetch, no Refresh, no data date, no request from the page. The Black-Scholes numbers are checked
against a Python reference in module_testing/e2e/test_dealer_hedge_tab.py.
"""
from __future__ import annotations

import re

import pytest

from RevenueChaser.app import dashboard, dealer_hedge
from RevenueChaser.core import provenance
from RevenueChaser.core.tests.open_issues_shell import strip_open_issues_data

TEMPLATE = dashboard.Path(dashboard.__file__).resolve().parent.parent / "templates" / "dashboard.html"


@pytest.fixture()
def client():
    dashboard.app.config["TESTING"] = True
    return dashboard.app.test_client()


def test_the_tab_follows_spx_gamma_in_the_research_group():
    tabs = dashboard.TABS
    keys = [t["key"] for t in tabs if t["type"] == "tab"]
    assert keys.index("dealer_hedge") == keys.index("module_spx_gamma") + 1
    tab = next(t for t in tabs if t.get("key") == "dealer_hedge")
    assert tab["url"] == "/view/dealer_hedge" and tab["label"] == "Dealer Hedge (demo)"
    head = max(i for i, t in enumerate(tabs) if t["type"] == "header" and tabs.index(tab) > i)
    assert tabs[head]["label"] == "Research"


def test_the_shell_lists_the_tab(client):
    html = strip_open_issues_data(client.get("/").get_data(as_text=True))
    assert 'data-tab="dealer_hedge"' in html and 'data-url="/view/dealer_hedge"' in html


def test_the_tab_is_a_read_only_view_with_no_fetch_or_refresh():
    """Not a board backed by data: no Fetch, no Refresh, no run route, no data date, no job."""
    assert "dealer_hedge" not in dashboard._MODEL_ONLY_RENDERERS
    assert "dealer_hedge" not in dashboard._STORE_REFRESHABLE
    assert dashboard._module_file("dealer_hedge") is None
    assert "dealer_hedge" in provenance.RESOLVERS and provenance.RESOLVERS["dealer_hedge"] is None
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "'dealer_hedge'" not in html.split("var STORE_BACKED")[1].split("];")[0]
    assert "dealer_hedge:" not in html.split("var RUN_URLS")[1].split("};")[0]
    show_tab = html.split("function showTab")[1].split("checkDataAsOf(key)")[0]
    refresh_line = next(ln for ln in show_tab.splitlines() if "rfBtn.style.display" in ln)
    assert "key==='dealer_hedge'" in refresh_line, "the Refresh button must be hidden on this tab"
    fetch_line = next(ln for ln in show_tab.splitlines() if "ftBtn.style.display" in ln)
    assert "dealer_hedge" not in fetch_line
    assert "'dealer_hedge': true" in html.split("var FILL_WIDTH")[1].split("};")[0], "the page fills the pane"


def test_the_page_renders_its_controls(client):
    resp = client.get("/view/dealer_hedge")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert len(html.encode("utf-8")) < 200_000, "a static page, far under the 2 MB board threshold"
    presets = re.findall(r'<button data-p="(\w+)"', html)
    assert presets == ["index", "shortgamma", "longgamma", "pin"]
    for slider in ("spot", "iv", "dte"):
        assert re.search(rf'<input type="range" id="{slider}"', html)
    assert 'id="reset"' in html
    scenarios = re.findall(r'<details data-s="(\w+)"', html)
    assert scenarios == ["dull", "squeeze", "crash", "pin", "crush"]
    for tile in ("bookDelta", "hedge", "trade", "bookGamma", "bookVanna", "bookCharm", "bookVega"):
        assert f'id="{tile}"' in html
    assert 'id="c1"' in html and 'id="c2"' in html and 'id="decomp"' in html and 'id="verdict"' in html


def test_the_page_is_in_turkish_and_names_its_source_and_caveat(client):
    html = client.get("/view/dealer_hedge").get_data(as_text=True)
    assert '<html lang="tr"' in html
    assert dealer_hedge.SOURCE in html.replace("&#39;", "'")
    assert "sayılar örnektir" in html, "the footer says the numbers are illustrative"


def test_the_page_makes_no_request():
    """No web font, script, stylesheet, image, frame, import or fetch: only plain links the reader may open."""
    html = dealer_hedge.render_page()
    low = html.lower()
    for tag in re.findall(r"<(?:script|link|img|iframe|source|video|audio|embed|object)\b[^>]*>", low):
        assert not re.search(r"""(?:src|href|data)\s*=\s*["']?(?:https?:)?//""", tag), tag
    assert "@import" not in low and not re.search(r"url\(\s*[\"']?(?:https?:)?//", low)
    for needle in ("fetch(", "xmlhttprequest", "websocket", "sendbeacon", "eventsource"):
        assert needle not in low, needle
    anchors = re.findall(r"<a\s[^>]*href=\"https?://[^>]*>", html)
    assert anchors, "the source list is part of the page"
    for a in anchors:
        assert 'target="_blank"' in a and "noopener" in a, a


def test_the_source_note_is_escaped():
    out = dealer_hedge.render_page('<img src=x onerror=alert(1)>')
    assert "<img src=x onerror=alert(1)>" not in out and "&lt;img src=x" in out
