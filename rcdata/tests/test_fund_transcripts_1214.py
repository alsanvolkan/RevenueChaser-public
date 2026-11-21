"""``earnings_transcript`` (#1214): Alpha Vantage's earnings-call transcripts, one request per fiscal quarter.

The provider's key travels in the query string only; these tests also pin that it is never stored or noted.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.fundamentals import transcripts

from . import fund_fixture as fx

KEY = "av-test-key-not-a-real-one"


def call(turns) -> str:
    import json
    return json.dumps({"symbol": "MSFT", "quarter": "2025Q1",
                       "transcript": [{"speaker": s, "title": t, "content": c, "sentiment": 0.0}
                                      for s, t, c in turns]})


MEASURED = call([("Brett Iversen", "Vice President of Investor Relations", "Good afternoon, and welcome."),
                 ("Satya Nadella", "CEO", "We are off to a solid start to our fiscal year."),
                 ("Amy Hood", "CFO", "Revenue was $65.6 billion, up 16%."),
                 ("Operator", "Operator", "We will now open the line for questions."),
                 ("Keith Weiss", "Analyst", "Can you unpack the guidance assumption?"),
                 ("Satya Nadella", "CEO", "It assumes no change in pricing.")])

HISTORY = [{"cik": "789019", "period_end": date(2024, 9, 30), "report_date": date(2024, 10, 30),
            "fiscal_year": 2025, "fiscal_quarter": 1},
           {"cik": "789019", "period_end": date(2024, 6, 30), "report_date": date(2024, 7, 30),
            "fiscal_year": 2024, "fiscal_quarter": 4},
           {"cik": "46619", "period_end": date(2024, 10, 31), "report_date": date(2024, 12, 18),
            "fiscal_year": 2024, "fiscal_quarter": 4}]


@pytest.fixture
def av(monkeypatch, http_mock):
    monkeypatch.setenv("RC_ALPHAVANTAGE_BASE", "https://av.test")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", KEY)
    fx.spine()
    # The fiscal label comes from another dataset's served table; these tests are about the fetch, so the label
    # source is given directly rather than by building the whole fundamentals file for it.
    from RevenueChaser.rcdata import read
    monkeypatch.setattr(read, "earnings_report_history", lambda **kw: list(HISTORY))
    return http_mock


def _raw_bytes() -> bytes:
    return b"".join(p.read_bytes() for p in base.raw_root().rglob("*") if p.is_file())


# ── The label ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_quarter_asked_for_is_the_companys_fiscal_quarter_not_a_calendar_one():
    """Measured 21 Nov 2026: ``MSFT`` ``2025Q1`` answers the call reporting $65.6bn and "a solid start to our
    fiscal year" -- Microsoft's fiscal Q1, which ended in September 2024. So the label is the fiscal pair, and a
    quarter derived from the period end's calendar month would ask for the wrong call."""
    assert transcripts.quarter_label(2025, 1) == "2025Q1"
    tickers = {"789019": ["MSFT"], "46619": ["HEI"]}
    assert transcripts.wanted(HISTORY, tickers) == [("HEI", "2024Q4"), ("MSFT", "2025Q1")]


def test_a_company_with_no_fiscal_label_is_never_asked_about():
    """A UW-sourced quarter carries no ``fyearq``/``fqtr``, and the label cannot be guessed: that company is left
    out and R8 abstains for it, rather than a wrong quarter being fetched under its name."""
    rows = [{"cik": "789019", "period_end": date(2024, 9, 30), "fiscal_year": None, "fiscal_quarter": None}]
    assert transcripts.wanted(rows, {"789019": ["MSFT"]}) == []
    assert transcripts.quarter_label(None, None) is None
    assert transcripts.quarter_label(2025, 7) is None
    assert transcripts.quarter_label("", 1) is None


def test_a_company_we_hold_no_ticker_for_is_left_out():
    assert transcripts.wanted(HISTORY, {}) == []


# ── The answer ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_turns_are_stored_in_the_order_spoken_with_the_providers_own_sentiment(av):
    av.add("av.test/query", MEASURED)
    check = fx.run(transcripts.NAME, "2026-10-28")
    assert check["result"] == "changed", check["note"]
    rows = fx.q("SELECT ticker, quarter, seq, title, content, provider_sentiment, source FROM earnings_transcript "
                "WHERE ticker = 'MSFT' ORDER BY seq")
    assert [(r[2], r[3]) for r in rows] == [(0, "Vice President of Investor Relations"), (1, "CEO"), (2, "CFO"),
                                            (3, "Operator"), (4, "Analyst"), (5, "CEO")]
    assert rows[1][4] == "We are off to a solid start to our fiscal year."
    assert rows[0][:2] == ("MSFT", "2025Q1") and rows[0][5] == 0.0 and rows[0][6] == transcripts.NAME


def test_the_key_is_in_the_request_only_and_never_in_a_raw_file_or_a_note(av):
    av.add("av.test/query", MEASURED)
    check = fx.run(transcripts.NAME, "2026-10-28")
    assert any(r.url.params.get("function") == transcripts.FUNCTION for r in av.requests)
    assert KEY.encode() not in _raw_bytes() and KEY not in (check["note"] or "")


def test_no_key_asks_nothing_and_says_so(av, monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY")
    check = fx.run(transcripts.NAME, "2026-10-28")
    assert "no ALPHAVANTAGE_API_KEY" in check["note"]
    assert not av.requests
    assert "missing_key" in [i["kind"] for i in ops.open_issues(transcripts.NAME)[1]]


@pytest.mark.parametrize("body", ['{"Information": "premium endpoint"}',
                                  '{"Note": "Thank you for using Alpha Vantage!"}',
                                  '{"Error Message": "Invalid API call"}'])
def test_a_refusal_is_not_an_empty_call_so_nothing_is_kept_and_the_name_is_asked_again(av, body):
    """Measured 21 Nov 2026: ``NVDA`` ``2025Q4`` answered ``{"Information": ...}`` with no ``transcript``. Keeping
    that as "this company has no call" would write a false absence and never retry, because a raw file on disk is
    this dataset's resume marker."""
    av.add("av.test/query", body)
    fx.run(transcripts.NAME, "2026-10-28")
    assert not list(base.raw_root().rglob("*.json.gz"))
    assert not transcripts.held("MSFT", "2025Q1")


def test_an_answer_with_no_turns_keeps_nothing_either(av):
    av.add("av.test/query", '{"symbol": "MSFT", "quarter": "2025Q1", "transcript": []}')
    fx.run(transcripts.NAME, "2026-10-28")
    assert not list(base.raw_root().rglob("*.json.gz"))


# ── The budget ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_quarter_already_on_disk_is_never_fetched_again(av):
    """A past quarter's call does not change, so the raw file is the resume marker: the second run spends the
    day's budget on the names it has not got, not on the ones it has."""
    av.add("av.test/query", MEASURED)
    fx.run(transcripts.NAME, "2026-10-28")
    first = len(av.requests)
    assert transcripts.held("MSFT", "2025Q1")
    fx.run(transcripts.NAME, "2026-10-29")
    assert len(av.requests) == first          # nothing left that is missing


def test_the_run_stops_at_the_day_cap_and_says_what_is_left_for_the_next_one(av, monkeypatch):
    from RevenueChaser.rcdata import caps
    monkeypatch.setitem(caps.DAY_CAPS, "alphavantage", 1)
    av.add("av.test/query", MEASURED)
    check = fx.run(transcripts.NAME, "2026-10-28")
    assert "left for the next run" in (check["note"] or ""), check["note"]
    assert len(av.requests) == 1              # one of the two companies, then the cap
