"""``uw/spot_expiry_strike`` paging: a session whose answer fills UW's page of 500 is asked for its next pages.

UW pages this endpoint with ``page`` (0-based) and ``limit``; the live check of 28 Oct 2026 found the last page is
the one shorter than the limit (an empty one after a page of exactly 500), pages never overlap and the order is
stable. The fakes below answer by the request's ``page`` at the HTTP boundary.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import caps, ops, read
from RevenueChaser.rcdata.build import base, uw_spot_expiry_strike as uw

from . import uw_fixture

SESSION = date(2026, 10, 26)
LIMIT = uw.PAGE_LIMIT


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc))


def _rows(n: int, first: int = 0, session: date = SESSION) -> list[dict]:
    """``n`` rows of distinct strikes, numbered from ``first``; the first half at 0 DTE, the rest at 1 DTE."""
    nxt = calendar.next_trading_day(session)
    out = []
    for i in range(first, first + n):
        row = {"expiry": (session if i % 2 == 0 else nxt).isoformat(), "strike": str(1000 + 5 * i),
               "time": f"{session}T20:15:00Z"}
        row.update({f: str(i + 0.5) for f in uw.NUMERIC_FIELDS})
        out.append(row)
    return out


def _paged(http_mock, pages: list, fail: dict | None = None) -> None:
    """Answer page ``n`` with ``{"data": pages[n]}``, or ``pages[n]`` itself when it is a dict (a page past the end is
    empty); ``fail`` maps a page to an HTTP status."""
    def answer(request):
        q = parse_qs(request.url.query.decode())
        page = int(q.get("page", ["0"])[0])
        if fail and page in fail:
            return fail[page], b'{"error": "bad"}', {"content-type": "application/json"}
        rows = pages[page] if page < len(pages) else []
        body = rows if isinstance(rows, dict) else {"data": rows}      # a dict page is sent as it is
        return 200, json.dumps(body), {"content-type": "application/json"}
    http_mock.add("/stock/SPX/spot-exposures/expiry-strike", answer)


def _pages_asked(http_mock) -> list:
    return [parse_qs(r.url.query.decode()).get("page", [None])[0] for r in http_mock.calls("expiry-strike")]


def _daily():
    return base.run_request({"dataset": uw.NAME, "params": {"as_of": SESSION.isoformat()}})


def _issues() -> list[tuple]:
    with ops.connect() as con:
        return [tuple(r) for r in con.execute("SELECT kind, detail FROM build_issues")]


def _stored() -> int:
    return len(read.spx_expiry_strike(SESSION))


def test_a_one_page_day_is_one_call_and_its_raw_file_is_unchanged(http_mock):
    """The small-day path stays byte-identical: one call without ``page``, and a raw file holding UW's body as sent
    with the same request and provenance keys as before paging."""
    body = uw_fixture.body()
    _paged(http_mock, [body["data"]])
    row = _daily()
    assert row["result"] == "changed" and row["rows"] == 391 and row["calls"] == 1
    assert _pages_asked(http_mock) == [None]
    doc = base.read_raw_json(uw.raw_path(SESSION))
    assert sorted(doc) == ["body", "fetched_at", "observed", "request", "run_id", "session_date"]
    assert doc["body"] == body
    assert doc["request"] == {"endpoint": uw.ENDPOINT, "params": {
        "expirations[]": ["2026-10-26", "2026-10-27"], "date": "2026-10-26", "limit": LIMIT}}


def test_a_two_page_day_is_stored_whole(http_mock):
    _paged(http_mock, [_rows(LIMIT), _rows(75, first=LIMIT)])
    row = _daily()
    assert row["result"] == "changed" and row["rows"] == LIMIT + 75 and row["calls"] == 2
    assert _pages_asked(http_mock) == [None, "1"]
    q = parse_qs(http_mock.calls("expiry-strike")[1].url.query.decode())
    assert q["limit"] == [str(LIMIT)] and q["date"] == ["2026-10-26"]
    assert q["expirations[]"] == ["2026-10-26", "2026-10-27"]
    assert _stored() == LIMIT + 75
    assert not [i for i in _issues() if i[0] == "truncated"]


def test_a_three_page_day_is_stored_whole_and_its_raw_file_says_how_it_was_asked(http_mock):
    _paged(http_mock, [_rows(LIMIT), _rows(LIMIT, first=LIMIT), _rows(3, first=2 * LIMIT)])
    row = _daily()
    assert row["result"] == "changed" and row["rows"] == 2 * LIMIT + 3 and row["calls"] == 3
    assert _pages_asked(http_mock) == [None, "1", "2"]
    doc = base.read_raw_json(uw.raw_path(SESSION))
    assert doc["request"]["pages"] == [LIMIT, LIMIT, 3]
    assert len(doc["body"]["data"]) == 2 * LIMIT + 3


def test_a_last_page_of_exactly_the_limit_is_followed_by_an_empty_one(http_mock):
    """A day of exactly 500 rows, which before paging was refused as "fills the page limit"."""
    _paged(http_mock, [_rows(LIMIT)])
    row = _daily()
    assert row["result"] == "changed" and row["rows"] == LIMIT and row["calls"] == 2
    assert _pages_asked(http_mock) == [None, "1"]
    assert base.read_raw_json(uw.raw_path(SESSION))["request"]["pages"] == [LIMIT, 0]


def test_a_failed_second_page_stores_nothing_for_the_day(http_mock):
    _paged(http_mock, [_rows(LIMIT), _rows(75, first=LIMIT)], fail={1: 400})
    row = _daily()
    assert row["result"] == "failed" and "page 1" in row["note"]
    assert not uw.raw_path(SESSION).exists() and not uw.partition_path(SESSION).exists()
    assert _pages_asked(http_mock) == [None, "1"]


@pytest.mark.parametrize("page1", [{}, {"message": "try later"}, {"data": None}])
def test_a_later_page_without_a_list_of_rows_fails_the_day(http_mock, page1):
    """Review should-fix 1: a 200 on page 1 that carries no ``data`` list is not an end of the day; read as an empty
    page it would store page 0 alone as a whole day, and the raw marker would keep it so."""
    _paged(http_mock, [_rows(LIMIT), page1])
    row = _daily()
    assert row["result"] == "failed" and "page 1" in row["note"]
    assert not uw.raw_path(SESSION).exists() and not uw.partition_path(SESSION).exists()


def test_a_later_page_stamped_wholly_on_another_day_fails_the_day(http_mock):
    """Review nit 4: the any-row-on-this-session check of page 0 holds for every page."""
    other = [dict(r, time="2026-10-23T20:15:00Z") for r in _rows(75, first=LIMIT)]
    _paged(http_mock, [_rows(LIMIT), other])
    row = _daily()
    assert row["result"] == "failed" and "page 1" in row["note"] and "not on 2026-10-26" in row["note"]
    assert not uw.raw_path(SESSION).exists()


def test_a_day_that_reaches_the_page_ceiling_fails_with_a_build_issue(http_mock):
    _paged(http_mock, [_rows(LIMIT, first=k * LIMIT) for k in range(uw.MAX_PAGES + 5)])
    row = _daily()
    assert row["result"] == "failed" and "page ceiling" in row["note"]
    assert len(http_mock.calls("expiry-strike")) == uw.MAX_PAGES
    assert not uw.raw_path(SESSION).exists()
    kinds = [k for k, _ in _issues()]
    assert kinds == ["truncated"]


def test_a_row_repeated_across_pages_fails_the_day(http_mock):
    """Review should-fix 2: with offset paging a repeat means the ordered set moved between calls, and the same move
    the other way skips a row unseen; so the day may be short and is not kept. The next run asks again."""
    page0 = _rows(LIMIT)
    page1 = [dict(r) for r in page0[:5]] + _rows(10, first=LIMIT)
    _paged(http_mock, [page0, page1])
    row = _daily()
    assert row["result"] == "failed" and "repeated across pages" in row["note"]
    assert not uw.raw_path(SESSION).exists() and not uw.partition_path(SESSION).exists()
    dup = [d for k, d in _issues() if k == "duplicate_key"]
    assert len(dup) == 1 and "5 rows" in dup[0] and "across pages" in dup[0]


def test_a_row_repeated_within_one_page_of_a_paged_day_goes_to_the_per_key_issue(http_mock):
    """Review nit 5: a repeat inside one page is what a one-page day already reports through ``to_parquet`` (the
    second row for a key dropped); the paged day does the same and is kept."""
    page1 = _rows(10, first=LIMIT)
    page1.append(dict(page1[0]))
    _paged(http_mock, [_rows(LIMIT), page1])
    row = _daily()
    assert row["result"] == "changed" and row["rows"] == LIMIT + 10
    assert len(base.read_raw_json(uw.raw_path(SESSION))["body"]["data"]) == LIMIT + 11
    dup = [d for k, d in _issues() if k == "duplicate_key"]
    assert dup == ["second row for the key dropped"]


def test_every_page_counts_against_the_day_cap(http_mock, monkeypatch):
    monkeypatch.setitem(caps.DAY_CAPS, "uw", 1)
    _paged(http_mock, [_rows(LIMIT), _rows(75, first=LIMIT)])
    row = _daily()
    assert row["result"] != "changed" and "day cap" in row["note"]
    assert len(http_mock.calls("expiry-strike")) == 1
    assert not uw.raw_path(SESSION).exists()


def test_the_backfill_pages_through_the_same_function_and_resumes(http_mock):
    """The hourly run and the backfill share ``_fetch_session``; a paged day's raw file is the resume marker."""
    def answer(request):
        q = parse_qs(request.url.query.decode())
        day = date.fromisoformat(q["date"][0])
        page = int(q.get("page", ["0"])[0])
        pages = [_rows(LIMIT, session=day), _rows(20, first=LIMIT, session=day)]
        rows = pages[page] if page < len(pages) else []
        return 200, json.dumps({"data": rows}), {"content-type": "application/json"}
    http_mock.add("expiry-strike", answer)
    req = {"dataset": uw.NAME, "params": {"mode": "backfill", "from": "2026-10-22", "to": "2026-10-23"}}
    row = base.run_request(req)
    assert row["calls"] == 4
    for day in (date(2026, 10, 22), date(2026, 10, 23)):
        assert uw.partition_path(day).exists()
        assert len(read.spx_expiry_strike(day)) == LIMIT + 20
    assert base.run_request(req)["calls"] == 0
