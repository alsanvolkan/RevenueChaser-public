"""The LEAPS board (#746): one session of ``option_far_contract``, drawn from the read API with no fetch.

The store is filled the real way, by the ``uw/far_contracts`` dataset against UW mocked at the HTTP boundary (the
same helpers as ``rcdata/tests/test_options_far_contracts.py``): an evening sweep, then the next morning's chains.
"""
from __future__ import annotations

import json
import re
from datetime import date

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.module_leaps import board
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401
from RevenueChaser.rcdata.tests.options_fixture import SESSION, UWOptions
from RevenueChaser.rcdata.tests.test_options_far_contracts import (AFTER_CLOSE, MORNING, _swept, at, chain_row,
                                                                   contract)


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    at(monkeypatch, AFTER_CLOSE)


@pytest.fixture
def stored(http_mock, monkeypatch):
    """One session: NVDA's roll (closing C100 2027, opening C110 2029), a put, an ETF, an index and a stale call."""
    uw = UWOptions()
    http_mock.add("uw.test/api", uw)
    near = contract("NVDA", "C", 100, volume=3000, oi=15725, premium="900000.00", expiry=date(2027, 12, 17))
    far = contract("NVDA", "C", 110, volume=3000, oi=130, premium="2500000.00", expiry=date(2029, 1, 19))
    put = contract("AAA", "P", 40, volume=500, oi=1000, premium="60000.00")
    etf = contract("SPY", "C", 700, volume=800, oi=50, premium="400000.00", issue_type="ETF")
    idx = contract("SPX", "P", 5000, volume=100, oi=10, premium="1200000.00", issue_type="Index")
    stale = contract("BBB", "C", 50, volume=200, oi=10, premium="55000.00")
    _swept(uw, [near, far, put, etf, idx, stale])
    uw.far_chain = {"NVDA": [chain_row(near, oi=14567), chain_row(far, oi=3584)],
                    "AAA": [chain_row(put, oi=1300)], "SPY": [chain_row(etf, oi=850)],
                    "SPX": [chain_row(idx, oi=60)], "BBB": [chain_row(stale, oi=60, prev_oi=11)]}
    at(monkeypatch, MORNING)
    from RevenueChaser.rcdata.build import base
    from RevenueChaser.rcdata.build.options import uw_far_contracts as far_ds
    assert base.run_request({"dataset": far_ds.NAME, "params": {}})["result"] != "failed"
    return {"near": near, "far": far, "put": put, "etf": etf, "idx": idx, "stale": stale}


def _data_of(page: str) -> dict:
    m = re.search(r"var LP_DATA = (\{.*?\});\n", page, re.S)
    assert m, "the page carries its session as LP_DATA"
    return json.loads(m.group(1).replace("\\u003c", "<"))


# ── Pure helpers ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ticker, issue_type, want", [
    ("NVDA", "Common Stock", "stocks"), ("SPY", "ETF", "etfs"), ("SPX", "Index", "index"),
    ("SPXW", None, "index"), ("VIX", "Common Stock", "index"), ("ABC", None, "stocks"), ("QQQ", "etf", "etfs")])
def test_each_contract_falls_in_one_of_three_groups(ticker, issue_type, want):
    assert board.product_of(ticker, issue_type) == want


def test_a_roll_partner_is_named_short_enough_for_a_chip():
    assert board.partner_label("NVDA271217C00100000") == "C100 Dec 2027"
    assert board.partner_label("SPXW290119P05012500") == "P5,012.5 Jan 2029"
    assert board.partner_label("not-an-occ") == "not-an-occ"


def test_a_session_parameter_is_a_date_or_nothing():
    assert board.parse_session("2026-10-26") == date(2026, 10, 26)
    assert board.parse_session("26 Oct") is None and board.parse_session(None) is None


# ── The payload, from the real table ───────────────────────────────────────────────────────────────────────────

def test_nothing_stored_is_no_payload():
    assert board.payload() is None


def test_one_stored_session_is_collecting_with_its_counts(stored):
    data = board.payload()
    assert data["session"] == SESSION.isoformat()
    assert (data["stored"], data["min_sessions"], data["collecting"]) == (1, 20, True)
    assert data["counts"] == {"contracts": 6, "names": 5, "matched": 5, "pending": 0, "stale": 1}
    assert [r["t"] for r in data["rows"]] == ["NVDA", "SPX", "NVDA", "SPY", "AAA", "BBB"], "premium, largest first"


def test_a_row_carries_what_the_card_draws(stored):
    rows = {r["sym"]: r for r in board.payload()["rows"]}
    far = rows[stored["far"]["option_symbol"]]
    assert far["con"] == "Call 110 · 19 Jan 2029" and far["side"] == "C" and far["prod"] == "stocks"
    assert (far["prem"], far["vol"], far["oi"], far["next"], far["chg"]) == (2_500_000.0, 3000, 130, 3584, 3454)
    assert far["st"] == "matched" and far["share"] == pytest.approx(1.1513, abs=1e-4) and far["out"] is True
    assert far["roll"] == "C100 Dec 2027"
    near = rows[stored["near"]["option_symbol"]]
    assert near["share"] == pytest.approx(-0.386, abs=1e-3) and near["out"] is False and near["roll"] == "C110 Jan 2029"
    assert rows[stored["stale"]["option_symbol"]]["st"] == "stale"
    assert rows[stored["stale"]["option_symbol"]]["share"] is None
    assert rows[stored["etf"]["option_symbol"]]["prod"] == "etfs"
    assert rows[stored["idx"]["option_symbol"]]["prod"] == "index"
    assert rows[stored["put"]["option_symbol"]]["con"].startswith("Put 40 · ")


def test_an_unknown_session_falls_back_to_the_newest(stored):
    assert board.payload(date(2020, 1, 2))["session"] == SESSION.isoformat()
    assert board.payload(SESSION)["session"] == SESSION.isoformat()


# ── The page ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_page_says_collecting_and_carries_the_session(stored):
    page = board.render_html(board.payload())
    assert "Collecting" in page and "1 / 20 sessions" in page
    assert f'<option value="{SESSION.isoformat()}" selected>' in page
    assert _data_of(page)["rows"][0]["t"] == "NVDA"
    assert 'rcWatchInit("leaps");' in page, "the shared watch-list pair, under the board's old panel name"
    assert "Sort: opened share" not in page, "while collecting nothing is ranked by the measure"
    for word in ("Accumulat", "conviction", "Tier"):
        assert word not in page, "a measurement, not a label"


def test_the_page_uses_the_theme_tokens_and_no_palette_of_its_own(stored):
    page = board.render_html(board.payload())
    css = page[page.index("<style>"):page.index("</style>")]
    own = css[css.index("body{margin:0"):]
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", own), "colours come from THEME_CSS by name"
    assert "var(--data-1" in own and "var(--accent" in own and "var(--amber" in own


def test_a_hostile_ticker_cannot_close_the_script(stored, monkeypatch):
    data = board.payload()
    data["rows"][0]["t"] = "</script><img src=x onerror=alert(1)>"
    page = board.render_html(data)
    assert "</script><img" not in page


# ── The tab ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_tab_serves_the_board_once_a_session_is_stored(stored):
    from RevenueChaser.app import dashboard
    c = dashboard.app.test_client()
    body = c.get("/view/module_leaps").get_data(as_text=True)
    assert "No data yet" not in body and "1 / 20 sessions" in body
    assert _data_of(body)["session"] == SESSION.isoformat()
    picked = c.get("/view/module_leaps?session=not-a-date").get_data(as_text=True)
    assert _data_of(picked)["session"] == SESSION.isoformat(), "an unknown session shows the newest"


def test_the_tab_never_serves_the_old_board_left_on_disk(stored, tmp_path, monkeypatch):
    import RevenueChaser.app.runners as rn
    from RevenueChaser.app import dashboard
    monkeypatch.setattr(rn, "LEAPS_LATEST", tmp_path / "leaps_latest.html")
    (tmp_path / "leaps_latest.html").write_text("<h1>LEAPS ACCUMULATION RADAR</h1>", encoding="utf-8")
    body = dashboard.app.test_client().get("/view/module_leaps").get_data(as_text=True)
    assert "ACCUMULATION RADAR" not in body and "Collecting" in body


def test_after_twenty_sessions_the_board_stops_collecting_and_offers_the_share_sort(stored):
    data = dict(board.payload(), stored=20, collecting=False)
    page = board.render_html(data)
    assert "Collecting" not in page and "20 sessions stored." in page and "Sort: opened share" in page


def test_the_counts_say_how_many_wait_for_their_open_interest(stored):
    data = board.payload()
    data["counts"] = dict(data["counts"], matched=0, pending=6, stale=0)
    page = board.render_html(data)
    assert "waiting for open interest" in page and "stale or missing" in page, "a partial read keeps both counts"
    assert "waiting for open interest" not in board.render_html(board.payload()), "shown only while some wait"
