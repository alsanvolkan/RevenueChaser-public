"""KL (#377, Card E): the shared filter kit on the LEAPS board.

Spec: plans/2026-10-15_card_e_filter_bars.md §1-2. Facets Index, Market cap, Sector, Theme; the kit composes
(AND) with the board's own controls; every tier is filtered, the Watchlist too (avo 2026-10-16); a failed ``filter_fields`` read
never fails a render.
"""
import json
import re

import pytest

import RevenueChaser.module_leaps.report as ld


def _snap(t, score=70, name=None, label="Accumulating"):
    return {"ticker": t, "name": name or f"{t} Corp", "label": label, "score": score, "frequency": 4,
            "notional": 400000.0, "net_notional": 100000.0, "ask_share": 0.7, "near_term_event": False}


def _fields(tickers):
    return {t: {"ticker": t, "name": f"{t} Corp", "index": ["sp500"], "market_cap": 5e9,
                "gics": ["Technology", "Software"], "theme_ids": [], "earnings_date": None}
            for t in tickers}


@pytest.fixture
def render(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", _fields)
    monkeypatch.setattr("RevenueChaser.core.universe.theme_catalog", lambda *a, **k: {"t1": {"label": "AI"}})

    def go(watched, unwatched, **kw):
        return ld.render_leaps_dashboard(watched, unwatched, **kw).read_text(encoding="utf-8")
    return go


def _json_var(page, name):
    m = re.search(rf"var {name}\s*=\s*(.*?);\n", page)
    assert m, f"{name} not found"
    return json.loads(m.group(1))


def test_bar_mount_and_four_facets(render):
    page = render([_snap("WL1")], [_snap("U1"), _snap("U2", score=30)])
    assert 'id="rc-filters"' in page
    assert "RCFilters.mount(" in page
    meta = _json_var(page, "META")
    assert meta["board"] == "leaps"
    assert meta["facets"] == ["index", "mc", "gics", "theme"]


def test_data_has_one_row_per_board_ticker(render):
    page = render([_snap("WL1")], [_snap("U1"), _snap("U2", score=30)])
    data = _json_var(page, "DATA")
    assert sorted(r["ticker"] for r in data) == ["U1", "U2", "WL1"]
    assert {"ticker", "name", "index", "market_cap", "gics", "theme_ids"} <= set(data[0])


def test_every_tr_has_data_ticker(render):
    page = render([_snap("WL1")], [_snap("U1")])
    bodies = re.findall(r"<tbody>(.*?)</tbody>", page, re.S)
    trs = re.findall(r"<tr[^>]*>", "".join(bodies))
    assert trs and all("data-ticker=" in tr for tr in trs)


def test_the_kit_filters_every_tier_watchlist_included(render):
    page = render([_snap("WL1")], [_snap("U1")])
    # avo 2026-10-16 (KL card "Watchlist de"): the kit filters all four tiers, the Watchlist too
    # (static half; module_testing/e2e/test_board_leaps.py TestFilterKitInTheBrowser checks it in Chrome)
    assert "KIT_TIERS = ['watchlist','high','emerging','quiet']" in page
    assert "SCAN_TIERS" not in page


def test_failed_filter_fields_still_renders_with_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "OUTPUT_DIR", tmp_path)

    def boom(_t):
        raise RuntimeError("store down")
    monkeypatch.setattr("RevenueChaser.core.universe.filter_fields", boom)
    page = ld.render_leaps_dashboard([_snap("WL1")], [_snap("U1")]).read_text(encoding="utf-8")
    assert "could not be read" in page
    assert 'id="rc-filters"' in page
    data = _json_var(page, "DATA")
    assert sorted(r["ticker"] for r in data) == ["U1", "WL1"]
    assert all(set(r) == {"ticker", "name"} for r in data)


def test_hostile_ticker_and_name_are_escaped_in_data(render):
    bad = '</script><img src=x onerror=alert(1)>'
    page = render([], [_snap("U1", name=bad)])
    assert bad not in page


def test_count_line_and_tier_counts_present(render):
    page = render([_snap("WL1")], [_snap("U1")])
    assert 'id="kit-count"' in page
    assert 'class="n"' in page and "data-total=" in page


def test_mount_wires_onchange_to_apply_kit(render):
    page = render([_snap("WL1")], [_snap("U1")])
    assert "onChange: applyKit" in page
    assert "applyKit(RCF)" in page          # the initial pass also applies a filter carried over from another board
