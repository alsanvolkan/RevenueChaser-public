"""``rcdata/caps.py`` through the builder: a provider at its day cap is refused before the first call."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import caps, ops
from RevenueChaser.rcdata.build import base, uw_spot_expiry_strike as uw


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc))


def test_a_dataset_whose_provider_is_at_its_cap_is_skipped_before_any_call(http_mock):
    ops.count_calls("uw", caps.DAY_CAPS["uw"])
    rid = ops.enqueue(uw.NAME, {"as_of": "2026-10-26"}, by="test")
    row = base.run_request(ops.next_request())
    assert http_mock.requests == []
    assert row["result"] == "skipped" and row["calls"] == 0
    assert row["note"] == f"day cap reached: uw {caps.DAY_CAPS['uw']}/{caps.DAY_CAPS['uw']}"
    with ops.connect() as con:
        assert con.execute("SELECT status FROM build_requests WHERE id = ?", (rid,)).fetchone()[0] == "skipped"
        assert con.execute("SELECT status FROM job_runs2").fetchone()[0] == "skipped"


def test_one_call_under_the_cap_still_runs(http_mock):
    from . import uw_fixture
    ops.count_calls("uw", caps.DAY_CAPS["uw"] - 1)
    http_mock.add("expiry-strike", uw_fixture.body())
    row = base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-26"}})
    assert row["result"] == "changed" and len(http_mock.requests) == 1


def test_a_provider_without_a_cap_is_never_refused():
    ops.count_calls("cboe", 10**9)
    assert caps.at_cap("cboe") == (False, 10**9, None)
