"""G9 (build plan PR 19, D1): the Earnings Fetch queues one build of each options dataset the board reads.

``uw/option_list`` (the list names' daily rows) and ``uw/option_screener`` (the screener set and the earnings set);
nothing before a dataset's first run, one request per dataset however often the board is pressed, and the board says
what happened in one line. The rest of the Fetch (the calendar, the earnings rows) is unchanged. Offline, temp stores.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.app.tests.test_earnings_history_runner_278 import _UW, _card
from RevenueChaser.app.tests.test_earnings_history_runner_278 import run  # noqa: F401 - the fixture
from RevenueChaser.rcdata import ops

DATASETS = ["uw/option_list", "uw/option_screener"]


@pytest.fixture
def notices(run, monkeypatch):  # noqa: F811
    seen: list = []

    def fake_export(cards, events, nrc, filename=None, filter_stats=None, provider_health=None, notice=None,
                    run_at=None):
        seen.append(notice)
        runners.EARNINGS_LATEST.write_text("<html>board</html>", encoding="utf-8")

    monkeypatch.setattr("RevenueChaser.module_earnings_radar.report.export_html", fake_export)
    run["cards"] = [_card("AAA")]
    return seen


def _fetch():
    return runners.run_earnings_range(_UW(), date(2026, 10, 8), date(2026, 10, 8))


def _queued():
    return sorted((r["dataset"], r["requested_by"]) for r in ops.queue_view()["active"])


def test_nothing_is_queued_before_the_datasets_first_run(notices):
    _fetch()
    assert _queued() == []
    for name in DATASETS:
        assert f"{name} not queued: it has not had its first run" in notices[-1]


def test_a_fetch_queues_each_options_dataset_once(notices, monkeypatch):
    monkeypatch.setattr(ops, "ever_built", lambda name: name in DATASETS)
    _fetch()
    assert "Builder request: uw/option_list queued, uw/option_screener queued." in notices[-1]
    _fetch()
    assert _queued() == [("uw/option_list", "earnings"), ("uw/option_screener", "earnings")]
    assert "uw/option_list already queued, uw/option_screener already queued." in notices[-1]


def test_an_empty_calendar_still_queues_and_the_empty_board_says_so(run, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ops, "ever_built", lambda name: name in DATASETS)
    run["events"] = []
    with pytest.raises(JobMessage):
        _fetch()
    assert _queued() == [("uw/option_list", "earnings"), ("uw/option_screener", "earnings")]
    assert "Builder request: uw/option_list queued" in runners.EARNINGS_LATEST.read_text("utf-8")
