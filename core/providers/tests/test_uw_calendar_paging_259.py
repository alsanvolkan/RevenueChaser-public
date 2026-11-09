"""#259: the UW earnings calendar (``/earnings/{session}``) reads every page, not only the first.

UW's spec pages from 0 (``docs/openapi.yaml``, schema ``Page``). A full page (as many rows as the limit asked
for) means there may be more, so the client asks for the next page until a short one comes back.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core.providers import unusual_whales as uwmod
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient

D = date(2026, 10, 9)
LIMIT = uwmod.CALENDAR_PAGE_LIMIT


def _rows(start, n):
    return [{"symbol": f"T{i}"} for i in range(start, start + n)]


def _client(pages, fail_page=None):
    """A client whose calendar answers ``pages[page]`` (page 0 when no ``page`` is asked); records each request."""
    c = UWClient(key="k", sleep=lambda s: None)
    calls = []

    def fake(ep, params=None):
        params = dict(params or {})
        calls.append((ep, params))
        page = params.get("page", 0)
        if page == fail_page:
            return ProviderResult("failed", {}, {"endpoint": ep, "status": 503}, ep)
        return ProviderResult("ok", {"data": pages[page] if page < len(pages) else []}, None, ep)

    c.get_result = fake  # type: ignore[method-assign]
    return c, calls


def test_a_short_first_page_is_one_request_as_before():
    c, calls = _client([_rows(0, 3)])
    res = c.calendar_day_raw_result(D, "premarket")
    assert res.status == "ok" and len(res.data) == 3
    assert calls == [("/earnings/premarket", {"date": "2026-10-09", "limit": LIMIT})]


def test_a_full_page_reads_the_next_page_until_a_short_one():
    c, calls = _client([_rows(0, LIMIT), _rows(LIMIT, LIMIT), _rows(2 * LIMIT, 7)])
    res = c.calendar_day_raw_result(D, "afterhours")
    assert res.status == "ok"
    assert [r["symbol"] for r in res.data] == [f"T{i}" for i in range(2 * LIMIT + 7)]
    assert [p.get("page") for _, p in calls] == [None, 1, 2]


def test_calendar_day_reads_every_page_of_both_sessions():
    c, _ = _client([_rows(0, LIMIT), _rows(LIMIT, 2)])
    res = c.calendar_day_result(D)
    assert res.status == "ok" and len(res.data) == 2 * (LIMIT + 2)


def test_a_failed_later_page_is_partial_and_keeps_the_earlier_rows():
    c, _ = _client([_rows(0, LIMIT), _rows(LIMIT, 5)], fail_page=1)
    res = c.calendar_day_raw_result(D, "premarket")
    assert res.status == "partial" and len(res.data) == LIMIT
    assert res.error["page"] == 1 and res.error["status"] == 503


def test_a_page_that_repeats_the_last_one_stops_the_read():
    """If UW ever ignores ``page``, the same full page must not be read (and counted) again and again."""
    same = _rows(0, LIMIT)
    c, calls = _client([same, same, same])
    res = c.calendar_day_raw_result(D, "premarket")
    assert len(res.data) == LIMIT and len(calls) == 2


def test_the_read_stops_at_the_page_cap():
    pages = [_rows(i * LIMIT, LIMIT) for i in range(uwmod.CALENDAR_MAX_PAGES + 2)]
    c, calls = _client(pages)
    res = c.calendar_day_raw_result(D, "premarket")
    assert len(calls) == uwmod.CALENDAR_MAX_PAGES
    assert len(res.data) == LIMIT * uwmod.CALENDAR_MAX_PAGES


def test_the_run_note_flags_a_session_only_when_the_page_cap_was_hit():
    from RevenueChaser.module_earnings_radar import history
    def events(n):
        return [{"source": "UW", "earnings_date": D, "earnings_time": "BMO"}] * n
    assert history._input_problems({}, False, events(LIMIT + 1)) == []       # paged past one page: whole
    cap = LIMIT * uwmod.CALENDAR_MAX_PAGES
    assert any("may be missing" in p for p in history._input_problems({}, False, events(cap)))
