"""``uw/earnings``'s ``fetch()`` driven at the HTTP boundary (#519 findings 1, 2, 3, 4 and 6).

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


# ── Finding 6: a provider-side stop mid-run is a failed run, not a successful one ───────────────────────────────

def _outage_after(http_mock, monkeypatch, good):
    """``good`` companies answer, then UW goes down (503 after the retries) for every later one."""
    tickers = [*good, *[f"Z{n:02d}" for n in range(uw_earnings.FAIL_STREAK + 3)]]
    _companies(monkeypatch, tickers)
    for t in good:
        http_mock.add(f"/stock/{t}/earnings", ANSWER)
    http_mock.add("/earnings", {"error": "down"}, status=503)


def test_an_outage_after_some_answers_fails_the_run(http_mock, monkeypatch):
    """Two answers, then five provider-side failures: the run used to end ``ok`` with three companies never asked,
    the truth only in a note. It fails now, naming how many were not asked."""
    _outage_after(http_mock, monkeypatch, ["AAA", "BBB"])
    with pytest.raises(base.SourceError, match="3 not asked"):
        _fetch()


def test_an_outage_mid_run_keeps_what_was_answered(http_mock, monkeypatch):
    """The answers before the stop stay as raw files (the resume marker) and reach their Parquet partition, so a
    next-day run, which does not ask them again within the week, does not leave them out of the build."""
    _outage_after(http_mock, monkeypatch, ["AAA", "BBB"])
    with pytest.raises(base.SourceError):
        _fetch()
    assert sorted(p.name for p in uw_earnings.day_dir(AS_OF).glob("*.json.gz")) == ["AAA.json.gz", "BBB.json.gz"]
    part = uw_earnings.common.parquet_dir(uw_earnings.NAME) / f"date={AS_OF}" / "part-0.parquet"
    import duckdb
    assert duckdb.sql(f"SELECT DISTINCT ticker FROM '{part}' ORDER BY 1").fetchall() == [("AAA",), ("BBB",)]


def test_an_outage_on_a_resumed_day_fails_the_run_too(http_mock, monkeypatch):
    """The day's earlier answers must not turn the stop into an ``ok`` run (``out`` is unioned with them)."""
    _companies(monkeypatch, ["AAA"])
    http_mock.add("/stock/AAA/earnings", ANSWER)
    _fetch()
    http_mock.requests.clear()
    _outage_after(http_mock, monkeypatch, [])
    with pytest.raises(base.SourceError, match="3 not asked"):
        _fetch()


def test_failures_at_the_very_end_are_not_a_stop(http_mock, monkeypatch):
    """When the last companies of the queue fail, everyone was asked: a note, not a failed run (finding 6 is
    about the companies the stop left unasked)."""
    _companies(monkeypatch, ["AAA", *[f"Z{n}" for n in range(uw_earnings.FAIL_STREAK)]])
    http_mock.add("/stock/AAA/earnings", ANSWER)
    http_mock.add("/earnings", {"error": "down"}, status=503)
    ctx, out = _fetch()
    assert [p.name for p in out] == ["AAA.json.gz"]


# ── Finding 2: a final failure is one failure, whoever saw it ───────────────────────────────────────────────────

def _failures_today() -> int:
    day = ops._today()
    return sum(int(r["failures"]) for r in ops.calls_by_day(day, day) if r["provider"] == "uw")


def test_a_provider_side_failure_is_counted_once(http_mock, monkeypatch):
    """A 503 after the retries is recorded by the client (``health()``) and added by ``RunContext.close``; the
    dataset used to add its own as well, so the Data screen showed 2 for one failed company."""
    _companies(monkeypatch, ["DOWN", "MSFT"])
    http_mock.add("/stock/DOWN/earnings", {"error": "down"}, status=503)
    http_mock.add("/stock/MSFT/earnings", ANSWER)
    ctx, _ = _fetch()
    assert ctx.calls["uw"][1] == 1 and _failures_today() == 1


def test_a_rejected_request_is_counted_once(http_mock, monkeypatch):
    """A 422 never reaches ``health()`` (not an outage), so ``RunContext`` counts it where it sees the answer."""
    _companies(monkeypatch, ["BAD", "MSFT"])
    http_mock.add("/stock/BAD/earnings", {"error": "unknown ticker"}, status=422)
    http_mock.add("/stock/MSFT/earnings", ANSWER)
    ctx, _ = _fetch()
    assert ctx.calls["uw"][1] == 1 and _failures_today() == 1


def test_every_uw_dataset_counts_a_rejected_request_the_same_way(http_mock):
    """Not only ``uw/earnings``: any dataset on ``ctx.uw_client()`` (``uw/financials`` counted none of its own) gets
    one failure for a request UW rejects, and none for a 401/403/429/5xx, which ``close()`` takes from
    ``health()``."""
    http_mock.add("/stock/BAD/financials", {"error": "unknown ticker"}, status=404)
    http_mock.add("/stock/KEY/financials", {"error": "no"}, status=401)
    ctx = base.RunContext(run_id="t", dataset="uw/financials")
    try:
        ctx.uw_client().get_result("/stock/BAD/financials")
        ctx.uw_client().get_result("/stock/KEY/financials")
    finally:
        ctx.close()
        ctx.flush_counts()
    assert ctx.calls["uw"][:2] == [2, 2] and _failures_today() == 2


# ── Finding 7: one walk of the raw root, not one per company ────────────────────────────────────────────────────

def test_the_newest_answer_per_company_is_read_in_one_walk(http_mock, monkeypatch):
    """``{ticker: newest day}`` from one pass over the day directories; a directory that is not a day is skipped
    instead of raising, and a company with no answer is simply absent."""
    root = uw_earnings.common.raw_dir(uw_earnings.NAME)
    for day, ticker in [("2026-10-23", "AAA"), ("2026-10-30", "AAA"), ("2026-10-30", "BBB")]:
        (root / day).mkdir(parents=True, exist_ok=True)
        (root / day / f"{ticker}.json.gz").write_bytes(b"")
    (root / "tmp").mkdir()
    (root / "tmp" / "CCC.json.gz").write_bytes(b"")
    assert uw_earnings.newest_by_ticker() == {"AAA": date(2026, 10, 30), "BBB": date(2026, 10, 30)}
    calls = []
    real = uw_earnings.newest_by_ticker
    monkeypatch.setattr(uw_earnings, "newest_by_ticker", lambda: calls.append(1) or real())
    _companies(monkeypatch, ["AAA", "BBB", "DDD"])
    http_mock.add("/earnings", ANSWER)
    _fetch(date(2026, 11, 2))
    assert len(calls) == 1 and _asked(http_mock) == ["DDD"]     # AAA and BBB answered within the week


# ── Finding 8: politeness pacing off UW's own per-minute headers ────────────────────────────────────────────────

def _minute_headers(left, reset_ms="1500"):
    return {"x-uw-req-per-minute-remaining": str(left), "x-uw-req-per-minute-reset": reset_ms}


def test_an_exhausted_minute_window_waits_for_its_reset(http_mock, monkeypatch):
    """When UW says the minute's requests are (nearly) spent, the run waits the reset UW names before the next
    request, instead of running into the 429 ladder. Politeness, not a published limit (``caps.py``)."""
    slept = []
    monkeypatch.setattr(base, "_uw_pause", slept.append)
    _companies(monkeypatch, ["AAA", "BBB"])
    http_mock.add("/stock/AAA/earnings", lambda request: (200, b"[]", _minute_headers(0)))
    http_mock.add("/stock/BBB/earnings", lambda request: (200, b"[]", _minute_headers(1_000_000)))
    _fetch()
    assert slept == [1.5]


def test_a_wide_window_or_no_header_never_waits(http_mock, monkeypatch):
    slept = []
    monkeypatch.setattr(base, "_uw_pause", slept.append)
    _companies(monkeypatch, ["AAA", "BBB"])
    http_mock.add("/stock/AAA/earnings", lambda request: (200, b"[]", _minute_headers(1_000_000, "60000")))
    http_mock.add("/stock/BBB/earnings", ANSWER)
    _fetch()
    assert slept == []


def test_the_wait_is_capped(http_mock, monkeypatch):
    slept = []
    monkeypatch.setattr(base, "_uw_pause", slept.append)
    _companies(monkeypatch, ["AAA"])
    http_mock.add("/stock/AAA/earnings", lambda request: (200, b"[]", _minute_headers(0, "3600000")))
    _fetch()
    assert slept == [base.UW_POLITE_MAX_S]


def test_a_429_is_not_paused_on_top_of_its_retry_after(http_mock, monkeypatch):
    """The client's Retry-After backoff already waits before the retry; the politeness wait must not stack on it."""
    slept = []
    monkeypatch.setattr(base, "_uw_pause", slept.append)
    _companies(monkeypatch, ["AAA"])
    answers = iter([(429, b'{"error":"slow down"}', _minute_headers(0)), (200, b"[]", _minute_headers(1_000_000))])
    http_mock.add("/stock/AAA/earnings", lambda request: next(answers))
    _fetch()
    assert slept == []
