"""#380 AG W1 S7: the Asymmetric Growth cards leave the HTML for the K10 payload route (#383 C6a, card F).

- The page is chrome and scripts; its cards are ``GET /view/module_asymmetric_growth/payload.json``
  (``report.board_payload``: ``{asof, cards}``), fetched with ``rcPayload``.
- A Fetch and a Refresh write both files (``reporting_common.write_board``); a Refresh rewrites the payload from
  the stored rows.
- Card F: what one board view downloads, compressed (HTML as served + the stored gzip payload), stays under 2 MB for
  a whole-universe AG scan.
- A payload that does not load says so on the board instead of showing an empty grid.
Offline, temp store and folders.
"""
from __future__ import annotations

import gzip
import json
import random

import pytest

from RevenueChaser.app import dashboard, runners
from RevenueChaser.core import rawstore, reporting_common as rc, ticker_store
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_asymmetric_growth import engine, freeze
from RevenueChaser.module_asymmetric_growth import report

LIMIT = 2 * 1024 * 1024      # card F: one board view, compressed


def _signals(rnd=None):
    rnd = rnd or random.Random(0)
    out = []
    for key, name, params in (("growth", "Accelerated Growth", ("rev_growth", "accelerating", "gross_margin")),
                              ("survival", "Survival / Runway", ("fcf", "net_debt_assets", "runway")),
                              ("momentum", "Momentum / Volume", ("dist_52w", "vol_ratio", "above_sma50"))):
        subs = [{"label": f"{p} rule", "value": round(rnd.uniform(-1, 3), 4), "unit": "pct", "threshold": "≥ 20%",
                 "op": "gte", "param": p, "passed": rnd.random() < 0.5} for p in params]
        passed = sum(1 for s in subs if s["passed"])
        out.append({"key": key, "name": name, "passed": passed, "known": 3, "total": 3, "subs": subs})
    return out


def _card(ticker, points=8, rnd=None, **kw):
    rnd = rnd or random.Random(ticker)
    card = {"ticker": ticker, "name": f"{ticker} Holdings Incorporated", "sector": "Technology",
            "industry": "Software - Application", "price": round(rnd.uniform(1, 500), 2),
            "market_cap": rnd.uniform(5e7, 5e11), "ps_ratio": rnd.uniform(0.1, 40), "rev_growth": rnd.uniform(-0.5, 2),
            "gross_margin": rnd.uniform(0, 0.9), "fcf": rnd.uniform(-1e9, 1e10), "runway_years": rnd.uniform(0, 30),
            "rd_intensity": rnd.uniform(0, 0.5), "dilution_yoy": rnd.uniform(-0.1, 0.5), "earnings_date": 1.79e9,
            "total_points": points, "tech_breakthrough": rnd.random() < 0.3, "dilution_warning": rnd.random() < 0.1,
            "us_listed": True, "source": "sec_edgar", "inst_holders": rnd.randint(0, 3000), "inst_new": rnd.randint(0, 90),
            "inst_exited": rnd.randint(0, 90), "inst_buy_flow": rnd.uniform(0, 1e8), "inst_sell_flow": rnd.uniform(0, 1e8),
            "inst_shares": rnd.uniform(0, 1e9), "inst_shares_delta_pct": rnd.uniform(-0.3, 0.3),
            "inst_ownership_pct": rnd.uniform(0, 1), "themes": ["Artificial Intelligence"], "signals": _signals(rnd),
            "index": ["S&P 500"]}
    card.update(kw)
    return card


def _result(cards, asof="2026-10-15T22:05:00+00:00"):
    return {"asof": asof, "universe_total": len(cards), "summary": {"scanned": len(cards)},
            "cards": cards, "thresholds": engine.THRESHOLDS}


def test_the_html_no_longer_inlines_the_cards(rawstore_tmp_db):
    html = report.render_asymmetric_growth_hunter_html(_result([_card("ZQXV", name="Quixotic Vortex Corp")]))
    assert "ZQXV" not in html and "Quixotic Vortex" not in html
    assert 'rcPayload("module_asymmetric_growth")' in html
    assert "function rcPayload" in html                      # the shared page side (PAYLOAD_JS)


def test_the_payload_is_the_stored_card_shape_with_the_filter_fields(rawstore_tmp_db):
    cards = [_card("AAA"), _card("BBB", points=5)]
    payload = report.board_payload(_result(cards))
    assert payload["asof"] == "2026-10-15T22:05:00+00:00"
    assert [c["ticker"] for c in payload["cards"]] == ["AAA", "BBB"]
    shown = payload["cards"][0]
    assert isinstance(shown.pop("gics"), list) and isinstance(shown.pop("theme_ids"), list)
    assert shown == ticker_store.card_view(cards[0])
    json.dumps(payload)                                       # JSON-ready, as the gzip file stores it


@pytest.fixture
def out(tmp_path, monkeypatch, rawstore_tmp_db):
    folder = tmp_path / "out"
    monkeypatch.setattr(runners, "OUTPUT_DIR", folder)
    monkeypatch.setattr(runners, "ASYMMETRIC_GROWTH_LATEST", folder / "asymmetric_growth_latest.html")
    monkeypatch.setattr(freeze, "freeze_result", lambda *a, **k: None)
    monkeypatch.setattr(runners, "_freeze_run_id", lambda: "fetch-1")
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1e9, "price": 10.0, "name": "A"}])
    return folder


def _stored_payload():
    return json.loads(gzip.decompress(rc.payload_path(runners.ASYMMETRIC_GROWTH_LATEST).read_bytes()))


def test_a_fetch_writes_the_html_and_the_payload_next_to_it(out, monkeypatch):
    result = _result([_card("AAA", model_version=freeze.model_version()), _card("BBB", points=5)])
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", lambda uw, **k: result)
    path = runners.run_asymmetric_growth(None)
    assert path == runners.ASYMMETRIC_GROWTH_LATEST and path.exists()
    assert "AAA" not in path.read_text(encoding="utf-8")
    assert [c["ticker"] for c in _stored_payload()["cards"]] == ["AAA", "BBB"]


def test_a_refresh_rewrites_the_payload_from_the_stored_rows(out, monkeypatch):
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", lambda uw, **k: _result([_card("AAA")]))
    runners.run_asymmetric_growth(None)
    # A later Fetch wrote other rows (no header of its own): the Refresh shows them, not the old payload.
    ticker_store.write_from_cards([_card("ZZZ", points=9)], asof="2026-10-16T22:05:00+00:00")
    runners.run_asymmetric_growth(None, no_fetch=True)
    assert [c["ticker"] for c in _stored_payload()["cards"]] == ["ZZZ"]
    assert _stored_payload()["asof"] == "2026-10-16T22:05:00+00:00"


def test_a_refresh_with_no_stored_scan_writes_an_empty_payload(out):
    runners.run_asymmetric_growth(None, no_fetch=True)
    assert _stored_payload()["cards"] == []
    assert "No stored scan yet" in runners.ASYMMETRIC_GROWTH_LATEST.read_text(encoding="utf-8")


def test_card_f_a_whole_universe_view_stays_under_2_mb(out, monkeypatch):
    """Card F: the HTML as served plus the payload as served (stored gzip), for 4,500 cards (the live scan scores
    about 4,187, plan §0). Before S7 the same view was one uncompressed page of about 10.7 MB (core review)."""
    rnd = random.Random(380)
    cards = [_card(f"T{i:04d}", points=rnd.randint(0, 9), rnd=rnd) for i in range(4500)]
    monkeypatch.setattr(engine, "run_asymmetric_growth_hunter", lambda uw, **k: _result(cards))
    runners.run_asymmetric_growth(None)
    monkeypatch.setattr(dashboard, "_module_file", lambda mod: runners.ASYMMETRIC_GROWTH_LATEST
                        if mod == "module_asymmetric_growth" else None)
    dashboard.app.config["TESTING"] = True
    client = dashboard.app.test_client()
    page = client.get("/view/module_asymmetric_growth", headers={"Accept-Encoding": "gzip"})
    data = client.get("/view/module_asymmetric_growth/payload.json", headers={"Accept-Encoding": "gzip"})
    assert page.status_code == 200 and data.status_code == 200
    assert data.headers["Content-Encoding"] == "gzip"
    total = len(page.data) + len(data.data)
    assert total < LIMIT, f"one AG view downloads {total:,} bytes"
    assert len(json.loads(gzip.decompress(data.data))["cards"]) == 4500


def test_the_page_renders_the_cards_it_loads(tmp_path, rawstore_tmp_db):
    result = _result([_card("AAA", points=9), _card("BBB", points=4)])
    html = report.render_asymmetric_growth_hunter_html(result)
    log = run_board_js(html, """
console.log(JSON.stringify({grid: _els['grid'].innerHTML, count: _els['count'].textContent}));
""", tmp_path, payload=report.board_payload(result))
    assert "AAA" in log["grid"] and "BBB" in log["grid"]
    assert log["count"].startswith("2 ")


def test_a_payload_that_does_not_load_says_so(tmp_path, rawstore_tmp_db):
    html = report.render_asymmetric_growth_hunter_html(_result([_card("AAA")]))
    log = run_board_js(html, """
globalThis.fetch = function(){ return Promise.resolve({ok: false, status: 404, json: function(){ return null; }}); };
init();
setTimeout(function(){ console.log(JSON.stringify({grid: _els['grid'].innerHTML})); }, 0);
""", tmp_path)
    assert "could not be loaded" in log["grid"] and "404" in log["grid"]
    assert "No companies match" not in log["grid"]
