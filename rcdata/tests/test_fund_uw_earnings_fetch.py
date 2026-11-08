"""``uw/earnings``'s ``fetch()`` driven at the HTTP boundary (#519 findings 1, 3 and 4).

The guard tests of #511/#513 read the dataset's source text; these drive the real ``fetch()`` over a real
:class:`UWClient` whose transport answers like UW, and read the result where it lands: the raw files, the calls the
run counted, and ``provider_calls_daily``.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import httpx
import pytest

from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.fundamentals import uw_earnings

AS_OF = date(2026, 11, 6)
ANSWER = [{"report_type": "quarterly", "fiscal_date_ending": "2025-09-30", "report_date": "2025-10-29",
           "reported_eps": "3.72", "estimated_eps": "3.66", "surprise_percentage": "1.64"}]


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(unusual_whales, "_sleep", lambda _s: None)       # the client's backoff, not the test's time


def _companies(monkeypatch, tickers):
    monkeypatch.setattr(uw_earnings, "companies", lambda: [(str(1000 + n), t) for n, t in enumerate(tickers)])


def _fetch(as_of=AS_OF):
    ctx = base.RunContext(run_id=f"t-{as_of}", dataset=uw_earnings.NAME)
    try:
        return ctx, uw_earnings.DATASET.fetch(ctx, as_of)
    finally:
        ctx.close()
        ctx.flush_counts()


def _asked(http_mock) -> list[str]:
    return [r.url.path.split("/")[3] for r in http_mock.calls("/earnings")]


# ── Finding 1: a request UW rejects must not pin a company at the head of every run ─────────────────────────────

def test_a_rejected_request_does_not_stop_the_run_or_starve_the_rest(http_mock, monkeypatch):
    """Five companies UW answers 422 sort first (no raw file: ``date.min``). They used to fill ``FAIL_STREAK``, so
    the run stopped before ZZZ, every week, for good. A 4xx is the request's own fault, not an outage."""
    bad = [f"AA{n}" for n in range(uw_earnings.FAIL_STREAK)]
    _companies(monkeypatch, [*bad, "ZZZ"])
    for t in bad:
        http_mock.add(f"/stock/{t}/earnings", {"error": "unknown ticker"}, status=422)
    http_mock.add("/stock/ZZZ/earnings", ANSWER)
    ctx, out = _fetch()
    assert "ZZZ" in _asked(http_mock)
    assert (uw_earnings.day_dir(AS_OF) / "ZZZ.json.gz") in out


def test_a_rejected_company_ages_like_any_other(http_mock, monkeypatch):
    """The rejected answer leaves a raw file with no rows, so the company is not due again for a week and stops
    sorting first; it serves no row."""
    _companies(monkeypatch, ["BAD", "ZZZ"])
    http_mock.add("/stock/BAD/earnings", {"error": "unknown ticker"}, status=422)
    http_mock.add("/stock/ZZZ/earnings", ANSWER)
    _fetch()
    tomb = uw_earnings.day_dir(AS_OF) / "BAD.json.gz"
    assert tomb.exists()
    doc = base.read_raw_json(tomb)
    assert uw_earnings.rows_of(doc.get("body"), doc["cik"], doc["ticker"])[0] == []
    http_mock.requests.clear()
    _fetch(AS_OF + timedelta(days=1))
    assert _asked(http_mock) == []                       # neither is due within the week
    http_mock.requests.clear()
    _fetch(AS_OF + timedelta(days=uw_earnings.WEEK_DAYS))
    assert sorted(_asked(http_mock)) == ["BAD", "ZZZ"]   # both age out together


def test_an_outage_still_stops_the_run(http_mock, monkeypatch):
    """A provider-side failure (5xx after the retries) still counts toward ``FAIL_STREAK``: the stop is kept for
    what it was for, and no raw file is written for a company UW never answered."""
    tickers = [f"C{n:02d}" for n in range(uw_earnings.FAIL_STREAK + 3)]
    _companies(monkeypatch, tickers)
    http_mock.add("/earnings", {"error": "down"}, status=503)
    with pytest.raises(base.SourceError):
        _fetch()
    assert len(set(_asked(http_mock))) == uw_earnings.FAIL_STREAK
    assert not uw_earnings.day_dir(AS_OF).exists() or not list(uw_earnings.day_dir(AS_OF).glob("*.json.gz"))


# ── Finding 3: every request sent is counted, a transport error too ──────────────────────────────────────────────

def test_one_answer_is_one_call(http_mock, monkeypatch):
    _companies(monkeypatch, ["MSFT"])
    http_mock.add("/stock/MSFT/earnings", ANSWER)
    ctx, _ = _fetch()
    assert ctx.calls["uw"][0] == 1 and ops.calls_today("uw") == 1


def test_a_429_then_200_is_two_calls_and_one_429(http_mock, monkeypatch):
    _companies(monkeypatch, ["MSFT"])
    answers = iter([(429, b'{"error":"slow down"}', {}), (200, b"[]", {})])
    http_mock.add("/stock/MSFT/earnings", lambda request: next(answers))
    ctx, _ = _fetch()
    assert ctx.calls["uw"][0] == 2 and ctx.calls["uw"][2] == 1
    assert ops.calls_today("uw") == 2


def test_a_read_timeout_is_counted_for_every_attempt_sent(http_mock, monkeypatch):
    """A ``ReadTimeout`` means the request was sent and UW counted it; the client retries, so one slow company
    costs ``MAX_ATTEMPTS`` quota units. The day cap must see them, as the keyless path does (counted on send)."""
    _companies(monkeypatch, ["SLOW", "MSFT"])

    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    http_mock.add("/stock/SLOW/earnings", slow)
    http_mock.add("/stock/MSFT/earnings", ANSWER)
    ctx, _ = _fetch()
    sent = len(http_mock.calls("/earnings"))
    assert sent == unusual_whales.MAX_ATTEMPTS + 1
    assert ctx.calls["uw"][0] == sent and ops.calls_today("uw") == sent


# ── Finding 4: a failed flush must not drop the calls it was writing ─────────────────────────────────────────────

def test_a_failed_flush_keeps_its_calls_for_the_next_one(http_mock, monkeypatch):
    """Every ``COUNT_FLUSH_EVERY``th call writes ``provider_calls_daily`` from inside the client's observer, which
    swallows what it raises. A locked ``ops.sqlite`` used to lose those calls from the pending book and the table
    both, so the day cap undercounted by 25 per failed flush."""
    n = base.COUNT_FLUSH_EVERY + 3
    _companies(monkeypatch, [f"T{i:03d}" for i in range(n)])
    http_mock.add("/earnings", ANSWER)
    real, failed = ops.count_calls, []

    def locked_once(*a, **k):
        if not failed:
            failed.append(a)
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(ops, "count_calls", locked_once)
    ctx, _ = _fetch()
    assert failed, "the first flush was meant to fail"
    assert ctx.calls["uw"][0] == n
    assert ops.calls_today("uw") == n


def test_a_failed_flush_raises_to_a_direct_caller():
    """Outside the observer the store error is not swallowed: ``_run``'s own handling decides, as before."""
    ctx = base.RunContext(run_id="t", dataset=uw_earnings.NAME)
    ctx.count("uw", calls=3)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    real = ops.count_calls
    ops.count_calls = locked
    try:
        with pytest.raises(sqlite3.OperationalError):
            ctx.flush_counts()
    finally:
        ops.count_calls = real
    ctx.flush_counts()
    assert ops.calls_today("uw") == 3


# ── Review of #541: a 4xx on every request is not each request's own fault ──────────────────────────────────────

def test_a_4xx_on_every_request_stops_the_run_and_hides_nobody(http_mock, monkeypatch):
    """UW drops the path (404 for everyone): the run stops after ``REJECT_STREAK`` rejected requests in a row with
    no answer between, fails loudly, and writes no empty answer, so nobody is hidden for a week."""
    _companies(monkeypatch, [f"T{i:03d}" for i in range(uw_earnings.REJECT_STREAK + 10)])
    http_mock.add("/earnings", {"error": "not found"}, status=404)
    with pytest.raises(base.SourceError):
        _fetch()
    assert len(http_mock.calls("/earnings")) == uw_earnings.REJECT_STREAK
    assert not list(uw_earnings.day_dir(AS_OF).glob("*.json.gz"))


def test_the_same_stop_holds_on_a_resumed_day(http_mock, monkeypatch):
    """Earlier answers of the day must not turn a mid-run loss of the path into an ``ok`` run."""
    tickers = [f"T{i:03d}" for i in range(uw_earnings.REJECT_STREAK + 10)]
    _companies(monkeypatch, tickers)
    http_mock.add("/stock/T000/earnings", ANSWER)
    http_mock.add("/earnings", {"error": "not found"}, status=404)
    with pytest.raises(base.SourceError):
        _fetch()
    assert [p.name for p in uw_earnings.day_dir(AS_OF).glob("*.json.gz")] == ["T000.json.gz"]


def test_a_rejected_request_waits_for_an_answer_before_its_empty_file(http_mock, monkeypatch):
    """An empty answer is written only once a later request shows the path still answers; one at the end of the
    queue is asked again next run instead."""
    _companies(monkeypatch, ["AAA", "ZZZ"])
    http_mock.add("/stock/AAA/earnings", ANSWER)
    http_mock.add("/stock/ZZZ/earnings", {"error": "unknown ticker"}, status=422)
    _fetch()
    assert [p.name for p in uw_earnings.day_dir(AS_OF).glob("*.json.gz")] == ["AAA.json.gz"]
