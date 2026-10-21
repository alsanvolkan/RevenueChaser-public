"""#386 F5: the corporate-event flag on the Fundamentals cards.

A synthetic store (``rawstore_tmp_db``: its own temp ``raw.duckdb`` and ``edgar.duckdb``) and synthetic scoring rows;
no real store, no network. The flag is display only, so the scoring test is the one that matters most.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

from RevenueChaser.core import rawstore
from RevenueChaser.core.tests.board_js import run_board_js
from RevenueChaser.module_fundamentals import config, events, freeze, pipeline
from RevenueChaser.module_fundamentals.pipeline import build_payload
from RevenueChaser.module_fundamentals.report import render_fundamental_dashboard
from RevenueChaser.module_fundamentals.tests.test_fundamental import _m

TODAY = date(2026, 10, 16)
_INSIDE = (TODAY - timedelta(days=30)).isoformat()
_OUTSIDE = (TODAY - timedelta(days=config.CORPORATE_EVENT_WINDOW_DAYS + 1)).isoformat()
_AT_THE_EDGE = (TODAY - timedelta(days=config.CORPORATE_EVENT_WINDOW_DAYS)).isoformat()


def _master(*rows):
    """``security_master`` rows, ``(ticker, cik)``."""
    rawstore.write_rows("security_master", [{"ticker": t, "cik": c} for t, c in rows])


def _filing(cik, accession, filing_date, items, form="8-K"):
    return {"cik": cik, "accession": accession, "form": form, "filing_date": filing_date, "items": items}


def _filings(*rows):
    rawstore.write_rows("sec_submissions", list(rows))


# ── the reader ───────────────────────────────────────────────────────────────

def test_a_qualifying_8k_inside_the_window_is_an_event(rawstore_tmp_db):
    _master(("AAA", "0001234"))
    _filings(_filing("1234", "a1", _INSIDE, "2.01,9.01"))

    found = events.corporate_events(["AAA"], today=TODAY)

    assert found == {"AAA": {"items": ["2.01"], "last_filing_date": _INSIDE}}


def test_an_8k_with_no_corporate_item_is_not_an_event(rawstore_tmp_db):
    _master(("AAA", "1234"))
    _filings(_filing("1234", "a1", _INSIDE, "2.02,9.01"))

    assert events.corporate_events(["AAA"], today=TODAY) == {}


def test_an_8k_older_than_the_window_is_not_an_event(rawstore_tmp_db):
    _master(("AAA", "1234"))
    _filings(_filing("1234", "old", _OUTSIDE, "2.01"))

    assert events.corporate_events(["AAA"], today=TODAY) == {}


def test_the_window_edge_is_inside(rawstore_tmp_db):
    """Mutation guard for M4: the rule is ``>= today - the window``, not strictly inside it."""
    _master(("AAA", "1234"))
    _filings(_filing("1234", "edge", _AT_THE_EDGE, "4.02"))

    assert events.corporate_events(["AAA"], today=TODAY) == {
        "AAA": {"items": ["4.02"], "last_filing_date": _AT_THE_EDGE}}


def test_every_item_of_the_set_counts_and_the_newest_filing_dates_it(rawstore_tmp_db):
    _master(("AAA", "1234"))
    _filings(_filing("1234", "a1", _INSIDE, "5.01"),
             _filing("1234", "a2", (TODAY - timedelta(days=5)).isoformat(), "4.02,2.01"),
             _filing("1234", "a3", (TODAY - timedelta(days=10)).isoformat(), "9.01"))

    found = events.corporate_events(["AAA"], today=TODAY)

    assert found["AAA"]["items"] == ["2.01", "4.02", "5.01"]
    assert found["AAA"]["last_filing_date"] == (TODAY - timedelta(days=5)).isoformat()


def test_a_10k_with_a_corporate_item_is_not_an_8k(rawstore_tmp_db):
    _master(("AAA", "1234"))
    _filings(_filing("1234", "a1", _INSIDE, "2.01", form="10-K"))

    assert events.corporate_events(["AAA"], today=TODAY) == {}


def test_an_empty_or_absent_store_is_no_event(rawstore_tmp_db):
    assert events.corporate_events(["AAA"], today=TODAY) == {}
    assert events.corporate_events([], today=TODAY) == {}


# ── the card ─────────────────────────────────────────────────────────────────

def _row(ticker, metrics, **kw):
    row = {"ticker": ticker, "name": ticker, "price": 50.0, "market_cap": 1e9, "sector": "Technology",
           "industry": "Software", "themes": [], "earnings_date": None, "period": "FY2024 vs FY2023",
           "metrics": metrics}
    row.update(kw)
    return row


_T3 = dict(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=45.0, ttm_revenue=2e9,
           roic=0.20, gross_margin=0.40, net_debt_ebitda=1.0,
           dist_52w_pct=0.95, rs_6m=0.1, above_200d=True)
_RO40 = dict(rev_growth_ttm=0.30, fcf_margin=0.20, ro40=50.0, ttm_revenue=2e9)


def _rows():
    return [_row("AAA", _m(**_RO40)), _row("BBB", _m(**_RO40)), _row("CCC", _m(**_T3))]


def _build(monkeypatch, rows=None):
    monkeypatch.setattr(pipeline, "_today", lambda: TODAY)
    return build_payload(copy.deepcopy(rows or _rows()), top_n=10)


def test_a_ticker_with_no_cik_is_unknown_not_clean(monkeypatch, rawstore_tmp_db):
    """The unknown-is-not-false rule: AAA has no CIK, so nothing is known about it and nothing is claimed."""
    _master(("BBB", "0002345"))
    _filings(_filing("2345", "a1", _INSIDE, "2.01"))

    p = _build(monkeypatch)
    cards = {c["ticker"]: c for c in p["candidates"]}

    assert cards["AAA"]["flags"] == [] and "corporate_event" not in cards["AAA"]
    assert cards["BBB"]["flags"] == ["corporate_event"]
    assert cards["BBB"]["corporate_event"] == {"items": ["2.01"], "last_filing_date": _INSIDE}
    assert cards["CCC"]["flags"] == [] and "corporate_event" not in cards["CCC"]


def test_the_flag_changes_nothing_that_is_scored(monkeypatch, rawstore_tmp_db):
    """Same rows, once with qualifying filings in the store and once without: every scored number is identical."""
    _master(("AAA", "1234"), ("BBB", "2345"), ("CCC", "3456"))
    version = freeze.model_version()
    plain = _build(monkeypatch)

    _filings(_filing("1234", "a1", _INSIDE, "2.01,9.01"),
             _filing("2345", "b1", _INSIDE, "4.02"))
    flagged = _build(monkeypatch)

    assert any(c.get("corporate_event") for c in flagged["candidates"]), "the fixture must flag something"
    assert [c["ticker"] for c in plain["candidates"]] == [c["ticker"] for c in flagged["candidates"]]
    for a, b in zip(plain["candidates"], flagged["candidates"]):
        assert {k: v for k, v in a.items() if k not in ("flags", "corporate_event")} == \
               {k: v for k, v in b.items() if k not in ("flags", "corporate_event")}, a["ticker"]
        assert a["bucket"] == b["bucket"] and a["rules_passed"] == b["rules_passed"]
        assert a["passed_count"] == b["passed_count"] and a["ro40"] == b["ro40"]
    for c in (plain["candidates"][0], flagged["candidates"][0]):
        assert "corporate_event" not in c["bucket"] and "corporate_event" not in c["rules_passed"]
    assert freeze.picks(plain["candidates"]) == freeze.picks(flagged["candidates"])
    assert freeze.model_version() == version


def test_the_selection_version_and_the_freeze_hash_are_untouched():
    assert config.SELECTION_VERSION == "6"
    assert "CORPORATE_EVENT_ITEMS" not in freeze._HASHED
    assert "CORPORATE_EVENT_WINDOW_DAYS" not in freeze._HASHED


def test_a_hostile_item_string_is_escaped_on_the_card(monkeypatch, tmp_path, rawstore_tmp_db):
    """The card is the only sink: a value shaped like a store item must never reach markup raw."""
    evil = "<img src=x onerror=alert(1)>"
    p = _build(monkeypatch)
    c = p["candidates"][0]
    c["flags"] = ["corporate_event"]
    c["corporate_event"] = {"items": ["2.01" + evil], "last_filing_date": evil}

    out = run_board_js(render_fundamental_dashboard(p),
                       "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)

    assert "<img" not in out and "&lt;img" in out
    assert "2.01" in out
