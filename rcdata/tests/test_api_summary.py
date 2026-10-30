"""``rcdata.api.summary``: a need's state without naming a single ticker (the Settings / Data screen's ask).

The screen does not want rows for a ticker set; it wants "how much does the store hold for this need, how new is it,
what did the last check say". The by-key readers cannot answer that, so the one query lives in ``rcdata.read``
(``table_summary``) and this wraps it in the same envelope ``get`` uses, so a card reads one shape either way.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import api, ops

from .test_spine_security import built as spine_built, run_all  # noqa: F401 - the built spine

SPINE_AS_OF = date(2026, 10, 27)


@pytest.fixture
def at_session(monkeypatch):
    def pin(day: date):
        monkeypatch.setattr(api.serve, "reference_session", lambda: day)
    return pin


def test_an_unknown_need_is_an_error():
    with pytest.raises(api.UnknownNeed):
        api.summary("prices.nope")


def test_nothing_built_yet_is_missing_with_no_rows():
    s = api.summary("identity.universe")
    assert s["state"] == "missing" and s["row_count"] == 0 and s["sample_rows"] == []
    assert s["freshness"]["as_of_session"] is None and s["fill"] is None


@pytest.mark.usefixtures("spine_built")
def test_a_built_need_reports_rows_freshness_coverage_and_status(at_session):
    at_session(SPINE_AS_OF)
    s = api.summary("identity.universe")
    assert s["need"] == "identity.universe" and s["state"] == "fresh"
    assert s["row_count"] > 0 and s["coverage"]["served"] == s["key_count"]
    assert s["freshness"] == {"as_of_session": SPINE_AS_OF, "lag_sessions": 0, "max_lag": 5, "unit": "sessions"}
    assert s["status"] == ops.latest_check("spine/security")["result"]
    assert s["coverage"]["asked"] is None, "the screen asked for nothing, so nothing was asked for"


@pytest.mark.usefixtures("spine_built")
def test_the_sample_carries_its_provenance(at_session):
    at_session(SPINE_AS_OF)
    s = api.summary("identity.universe", sample=5)
    assert 0 < len(s["sample_rows"]) <= 5
    assert len(s["provenance"]) == len(s["sample_rows"])
    assert s["provenance"][0]["dataset"] == "spine/security" and s["provenance"][0]["key"]


@pytest.mark.usefixtures("spine_built")
def test_a_summary_goes_stale_by_the_needs_own_window(at_session):
    from RevenueChaser.core import calendar
    day = SPINE_AS_OF
    for _ in range(6):
        day = calendar.next_trading_day(day)
    at_session(day)
    assert api.summary("identity.universe")["state"] == "stale"


@pytest.mark.usefixtures("spine_built")
def test_a_summary_never_fills_and_queues_nothing():
    api.summary("identity.universe")
    assert ops.queue_view()["active"] == []


@pytest.mark.usefixtures("spine_built")
def test_the_read_log_counts_the_need_name(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(ops.read_log, "bump", seen.append)
    api.summary("identity.universe")
    assert seen[0] == "need:identity.universe"
