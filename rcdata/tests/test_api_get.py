"""``rcdata.api.get`` over a built store: the envelope's freshness, coverage, provenance and status.

The spine's needs are read over the built spine of ``test_spine_security``, the prices store's over the fixture week
of ``test_prices_serving``. Every test that asserts a state pins the reference session (``_latest_session``), so the
answer does not turn stale as the calendar moves past the fixtures.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import api, ops, read

from .test_prices_serving import AAPL, FLG, built as prices_built, env  # noqa: F401 - the built prices store
from .test_spine_security import built as spine_built, run_all  # noqa: F401 - the built spine of the 10 names

SPINE_AS_OF = date(2026, 10, 27)   # the as-of of the spine fixture's SEC file


@pytest.fixture
def at_session(monkeypatch):
    """Pin the session the lag is counted against."""
    def pin(day: date):
        monkeypatch.setattr(api.serve, "_latest_session", lambda: day)
    return pin


# ── The spine's needs ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("spine_built")
def test_fresh_rows_are_the_read_rows_with_provenance_and_status(at_session):
    at_session(SPINE_AS_OF)
    ans = api.get("identity.universe")
    assert ans.rows == read.universe()
    assert ans.state == "fresh" and ans.ok
    assert ans.freshness == {"as_of_session": SPINE_AS_OF, "lag_sessions": 0, "max_lag": 5}
    assert ans.status == ops.latest_check("spine/security")["result"]
    msft = next(p for p in ans.provenance if p["key"] == "MSFT")
    assert msft["source"] == "sec/company_tickers_exchange" and msft["dataset"] == "spine/security"
    assert msft["source_as_of"] == SPINE_AS_OF and msft["run_id"] and msft["fetched_at"]


@pytest.mark.usefixtures("spine_built")
def test_stale_once_the_newest_day_is_past_the_needs_max_lag(at_session):
    inside = SPINE_AS_OF
    for _ in range(5):
        inside = calendar.next_trading_day(inside)
    at_session(inside)
    assert api.get("identity.universe").state == "fresh"          # exactly max_lag sessions behind
    at_session(calendar.next_trading_day(inside))
    ans = api.get("identity.universe")
    assert ans.state == "stale" and ans.freshness["lag_sessions"] == 6


@pytest.mark.usefixtures("spine_built")
def test_coverage_names_what_was_asked_and_what_the_store_does_not_hold(at_session):
    at_session(SPINE_AS_OF)
    ans = api.get("identity.security", tickers=["msft", "WFC-PC", "NOPE"])
    assert ans.coverage == {"asked": 3, "served": 2, "missing": ["NOPE"], "from_fallback": 0}
    assert ans.state == "fresh"


@pytest.mark.usefixtures("spine_built")
def test_read_log_counts_the_need_name_beside_the_table_name(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(ops.read_log, "bump", seen.append)
    api.get("identity.universe")
    assert seen[0] == "need:identity.universe" and "security" in seen


@pytest.mark.usefixtures("spine_built")
def test_fill_never_queues_nothing_and_calls_no_provider():
    api.get("identity.universe")
    api.get("identity.security", tickers=["NOPE"])
    assert ops.queue_view()["active"] == []


# ── The prices store's needs ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.usefixtures("prices_built")
def test_prices_bars_goes_stale_after_one_session(at_session):
    newest = max(r["session_date"] for r in read.bars([AAPL]))
    at_session(calendar.next_trading_day(calendar.next_trading_day(newest)))
    ans = api.get("prices.bars", security_ids=[AAPL, FLG])
    assert ans.state == "stale" and ans.freshness["lag_sessions"] == 2 and ans.freshness["max_lag"] == 1
    assert ans.freshness["as_of_session"] == newest and ans.rows == read.bars([AAPL, FLG])
    assert ans.coverage["served"] == 2 and ans.coverage["missing"] == []


@pytest.mark.usefixtures("prices_built")
def test_a_security_the_store_does_not_hold_is_named_missing(at_session):
    at_session(max(r["session_date"] for r in read.bars([AAPL])))
    ans = api.get("prices.market_cap", security_ids=[AAPL, "sec:nope"])
    assert ans.coverage["asked"] == 2 and ans.coverage["missing"] == ["sec:nope"]
    assert ans.state == "fresh" and ans.fill is None


@pytest.mark.usefixtures("prices_built")
def test_the_reader_gets_the_params_unchanged(at_session):
    at_session(max(r["session_date"] for r in read.bars([AAPL])))
    ans = api.get("prices.bars", security_ids=[AAPL], basis="split")
    assert {r["basis"] for r in ans.rows} == {"split"}


# ── Every catalogued need answers over a built store (a catalog entry that names the wrong column is a bug) ─────

@pytest.mark.usefixtures("spine_built")
def test_the_spine_needs_all_answer(at_session):
    at_session(SPINE_AS_OF)
    for name in ("identity.universe", "identity.security", "identity.membership"):
        ans = api.get(name)
        assert ans.rows, f"{name}: no rows over the built spine"
        assert ans.freshness["as_of_session"] is not None, f"{name}: day_column names no day"
        assert all(p["key"] for p in ans.provenance), f"{name}: key_column names no key"


@pytest.mark.usefixtures("prices_built")
def test_the_prices_needs_all_answer(at_session):
    at_session(max(r["session_date"] for r in read.bars([AAPL])))
    for name in ("prices.bars", "prices.market_cap", "prices.shares"):
        ans = api.get(name, security_ids=[AAPL, FLG])
        assert ans.rows, f"{name}: no rows over the built prices store"
        assert ans.freshness["as_of_session"] is not None, f"{name}: day_column names no day"
        assert all(p["key"] for p in ans.provenance), f"{name}: key_column names no key"
