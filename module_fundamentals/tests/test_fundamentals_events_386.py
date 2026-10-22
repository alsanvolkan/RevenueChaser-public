"""#386 F5: the corporate-event flag on the Fundamentals cards.

A synthetic store (``rawstore_tmp_db``: its own temp ``raw.duckdb`` and ``edgar.duckdb``) and synthetic scoring rows;
no real store, no network. The flag is display only, so the scoring test is the one that matters most.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

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


@pytest.mark.parametrize("item", config.CORPORATE_EVENT_ITEMS)
def test_every_configured_item_raises_the_flag(item, rawstore_tmp_db):
    """E1: the reader follows the configuration, not a hard-coded subset — every item of the set counts.

    Parametrised over ``CORPORATE_EVENT_ITEMS`` (E1): a dropped or forgotten code fails one case by name.
    """
    _master(("AAA", "1234"))
    _filings(_filing("1234", "a1", _INSIDE, f"{item},9.01"))

    assert events.corporate_events(["AAA"], today=TODAY) == {
        "AAA": {"items": [item], "last_filing_date": _INSIDE}}


@pytest.mark.parametrize("item", ["2.02", "9.01"])
def test_an_item_outside_the_set_does_not_raise_the_flag(item, rawstore_tmp_db):
    """E1: results of operations (2.02) and exhibits (9.01) are routine; they are not corporate events."""
    _master(("AAA", "1234"))
    _filings(_filing("1234", "a1", _INSIDE, item))

    assert events.corporate_events(["AAA"], today=TODAY) == {}


def test_the_configured_item_set_is_the_eight_items():
    """E2: the brief's eight items, in the brief's order."""
    assert config.CORPORATE_EVENT_ITEMS == ["1.02", "2.01", "2.06", "3.01",
                                           "4.01", "4.02", "5.01", "5.03"]


def test_a_10k_with_a_corporate_item_is_not_an_8k(rawstore_tmp_db):
    _master(("AAA", "1234"))
    _filings(_filing("1234", "a1", _INSIDE, "2.01", form="10-K"))

    assert events.corporate_events(["AAA"], today=TODAY) == {}


def test_an_empty_or_absent_store_is_no_event(rawstore_tmp_db):
    assert events.corporate_events(["AAA"], today=TODAY) == {}
    assert events.corporate_events([], today=TODAY) == {}


def test_a_non_digit_cik_is_no_cik_and_never_an_exception(rawstore_tmp_db):
    """E6: the shared normaliser raises on a non-digit CIK; the reader answers "unknown", it does not raise."""
    _master(("AAA", "45-12"), ("BBB", "0001234"))
    _filings(_filing("1234", "a1", _INSIDE, "2.01"))

    assert events.corporate_events(["AAA", "BBB"], today=TODAY) == {
        "BBB": {"items": ["2.01"], "last_filing_date": _INSIDE}}
    assert events._norm_cik("45-12") is None and events._norm_cik("") is None
    assert events._norm_cik("0001234") == "1234"


# ── the bounded reads ────────────────────────────────────────────────────────

_BIG = events.CHUNK + 400          # candidate tickers in the chunking test: two slices whatever ``CHUNK`` becomes
_MANY = 4_500                      # candidates in the session test: the field size the review priced (24 statements)


def _big_store(n: int = _BIG) -> list[str]:
    """``n`` candidates, each with its own CIK; the first two have filings (one flagged, one routine)."""
    _master(*((f"T{i:04d}", str(100_000 + i)) for i in range(n)))
    _filings(_filing("100000", "a1", _INSIDE, "2.01"), _filing("100001", "a2", _INSIDE, "9.01"))
    return [f"T{i:04d}" for i in range(n)]


def test_the_store_reads_are_chunked_and_the_answer_is_the_unchunked_one(monkeypatch, rawstore_tmp_db):
    """E3: production passes no top_n, so the candidate list is unbounded: both reads walk it in slices.

    The spy sees every store call of the chunked read; the same input read in one slice must answer the same dict.
    """
    tickers = _big_store()
    calls = []
    real = rawstore.query

    def spy(sql, params=None, **kwargs):
        calls.append((sql, list(params or [])))
        return real(sql, params, **kwargs)

    monkeypatch.setattr(rawstore, "query", spy)
    stats: dict = {}
    chunked = events.corporate_events(tickers, today=TODAY, stats=stats)     # stats: the DISTINCT-cik read runs too
    master = [c for c in calls if "security_master" in c[0]]
    filings = [c for c in calls if "8-K" in c[0]]
    absent = [c for c in calls if "DISTINCT cik" in c[0]]

    assert chunked == {"T0000": {"items": ["2.01"], "last_filing_date": _INSIDE}}
    assert len(master) > 1 and len(filings) > 1, "the reads must be sliced, not one statement for every candidate"
    for sql, params in master:
        assert "ticker IN" in sql, sql            # the whole security_master is never read
        assert len(params) <= events.CHUNK
    for _sql, params in filings:
        assert len(params) - 1 <= events.CHUNK    # the window date plus at most one slice of CIKs
    assert len(absent) > 1, "the DISTINCT-cik read must run and be sliced too"
    for _sql, params in absent:
        assert len(params) <= events.CHUNK

    monkeypatch.setattr(events, "CHUNK", 10 * _BIG)       # one slice per table: the slicing changes no answer
    assert events.corporate_events(tickers, today=TODAY) == chunked


def test_the_store_reads_share_one_session(monkeypatch, rawstore_tmp_db):
    """E7: every store read of one call runs inside one ``rawstore.session()``: one connect per store file.

    A job's worker and the scheduled CLI Fetch have no outer request session, so the reader's own block is what
    keeps 4,500 candidates from opening the 500 MB store once per statement (the reviewer's field measurement: 24
    connects, 9.5 s). ``security_master`` lives in ``raw``, ``sec_submissions`` in ``edgar``: at most two connects,
    whatever the candidate count, and the answer is the same one statement per table gives.
    """
    tickers = _big_store(_MANY)
    with pytest.MonkeyPatch.context() as mp:          # the answer of one statement per table
        mp.setattr(events, "CHUNK", 10 * _MANY)
        reference = events.corporate_events(tickers, today=TODAY)
        ref_stats: dict = {}
        events.corporate_events(tickers, today=TODAY, stats=ref_stats)

    opened = []
    real = rawstore._connect_with_retry

    def counting(*args, **kwargs):
        opened.append(str(kwargs.get("path") or (args[1] if len(args) > 1 else rawstore.DB_PATH)))
        return real(*args, **kwargs)

    monkeypatch.setattr(rawstore, "_connect_with_retry", counting)

    stats: dict = {}
    assert events.corporate_events(tickers, today=TODAY, stats=stats) == reference
    assert stats == ref_stats                          # the stats read rides the same session, and agrees
    assert stats["no_submissions"] == _MANY - 2        # every filer but the two with a filing row
    assert reference == {"T0000": {"items": ["2.01"], "last_filing_date": _INSIDE}}
    assert len(opened) <= 2, opened                    # one handle per store file, never one per statement
    assert len(set(opened)) == len(opened), opened     # and never the same file opened twice


def test_a_mixed_input_answers_exactly_what_the_unbounded_read_answered(rawstore_tmp_db):
    """E4: the expected dict is written by hand from the old (unbounded, every-ticker) implementation's rules."""
    _master(("AAA", "0001234"), ("BBB", "2345"), ("CCC", "3456"), ("DDD", "4567"),
            ("FFF", "00001234"), ("GGG", "5678"))
    _filings(
        _filing("1234", "a1", _INSIDE, "2.01,9.01"),
        _filing("1234", "a2", (TODAY - timedelta(days=5)).isoformat(), "4.02"),
        _filing("1234", "a3", _OUTSIDE, "5.01"),                       # outside the window: ignored
        _filing("2345", "b1", _INSIDE, "9.01"),                        # routine only
        _filing("3456", "c1", _AT_THE_EDGE, "5.03,1.02"),              # the window edge is inside
        _filing("3456", "c2", (TODAY - timedelta(days=1)).isoformat(), "3.01"),
        _filing("4567", "d1", _INSIDE, "2.01", form="10-Q"),           # not an 8-K
    )

    assert events.corporate_events(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"], today=TODAY) == {
        # AAA and FFF are the same filer: both carry its items, sorted, and its newest qualifying date.
        "AAA": {"items": ["2.01", "4.02"], "last_filing_date": (TODAY - timedelta(days=5)).isoformat()},
        "FFF": {"items": ["2.01", "4.02"], "last_filing_date": (TODAY - timedelta(days=5)).isoformat()},
        "CCC": {"items": ["1.02", "3.01", "5.03"], "last_filing_date": (TODAY - timedelta(days=1)).isoformat()},
        # BBB: routine items only; DDD: a 10-Q is not an 8-K; EEE: no CIK; GGG: no filings at all.
    }


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

    assert "corporate_event" not in cards["AAA"]                  # unknown, never "no event"
    assert cards["BBB"]["corporate_event"] == {"items": ["2.01"], "last_filing_date": _INSIDE}
    assert "corporate_event" not in cards["CCC"]
    for c in cards.values():
        assert "flags" not in c                                   # the unread list is gone: a card says an event or nothing


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
        assert {k: v for k, v in a.items() if k != "corporate_event"} == \
               {k: v for k, v in b.items() if k != "corporate_event"}, a["ticker"]
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


def test_no_submissions_counts_only_the_candidates_the_store_cannot_answer_for(monkeypatch, rawstore_tmp_db):
    """E5: the flag's blind spot, counted. A candidate whose CIK has no filing row at all is not "no event".

    Two blind spots, counted apart: ``no_submissions`` (a CIK, no row) and ``no_cik`` (no usable CIK at all — a
    candidate with no ``security_master`` row like EEE, or a value that is no CIK like DDD, is counted nowhere else).
    """
    _master(("AAA", "0001234"), ("BBB", "0002345"), ("CCC", "0003456"), ("DDD", "not-a-cik"))
    _filings(_filing("1234", "a1", _INSIDE, "2.01"),                       # AAA: an 8-K
             _filing("2345", "b1", _INSIDE, "9.01", form="10-K"))          # BBB: a filing of another form
    # CCC's CIK has no row at all; DDD has a CIK-shaped value that is no CIK; EEE is not in security_master.
    monkeypatch.setattr(pipeline, "_today", lambda: TODAY)
    stats = {"regime": "RISK-ON"}
    p = build_payload([_row(t, _m(**_RO40)) for t in ("AAA", "BBB", "CCC", "DDD", "EEE")],
                      top_n=10, run_stats=stats)

    assert p["data_quality"]["no_submissions"] == 1
    assert stats["no_submissions"] == 1
    assert p["data_quality"]["no_cik"] == 2                # DDD and EEE: no usable CIK in security_master
    assert stats["no_cik"] == 2
    cards = {c["ticker"]: c for c in p["candidates"]}
    assert cards["AAA"]["corporate_event"] == {"items": ["2.01"], "last_filing_date": _INSIDE}
    assert all("flags" not in c for c in cards.values())


def test_a_hostile_item_string_is_escaped_on_the_card(monkeypatch, tmp_path, rawstore_tmp_db):
    """The card is the only sink: a value shaped like a store item must never reach markup raw."""
    evil = "<img src=x onerror=alert(1)>"
    p = _build(monkeypatch)
    c = p["candidates"][0]
    c["corporate_event"] = {"items": ["2.01" + evil], "last_filing_date": evil}

    out = run_board_js(render_fundamental_dashboard(p),
                       "console.log(JSON.stringify(_els['grid'].innerHTML));", tmp_path)

    assert "<img" not in out and "&lt;img" in out
    assert "2.01" in out
