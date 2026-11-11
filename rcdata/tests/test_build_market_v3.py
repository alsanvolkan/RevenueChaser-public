"""Market 3 (#643): ``uw/economic_calendar`` through the builder, UW mocked at the HTTP boundary; an hourly dataset of
the ``market`` kind. Market 4 dropped the IBKR headlines (``ibkr/news``, ``news_headline``) for ``boards/news.duckdb``."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops, read, serving
from RevenueChaser.rcdata.build import DATASETS, base, due, uw_economic_calendar as econ

pytestmark = pytest.mark.usefixtures("kinds_full")

UW_HEADERS = {"x-uw-daily-req-count": "355", "x-uw-req-per-minute-remaining": "1000000",
              "x-uw-req-per-minute-reset": "60000", "x-uw-token-req-limit": "100000000",
              "content-type": "application/json"}
NOW = datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc)
CPI = {"event": "CPI", "type": "report", "time": "2026-11-12T13:30:00Z", "forecast": "0.3%", "prev": "0.4%",
       "reported_period": "October"}
FOMC = {"event": "FOMC Minutes", "type": "fomc", "time": "2026-11-18 19:00:00+00:00", "forecast": None,
        "prev": None, "reported_period": None}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(calendar, "_now", lambda: NOW)


def _run(name: str) -> dict:
    return base.run_request({"dataset": name, "params": {}})


def test_the_economic_calendar_is_an_hourly_market_dataset():
    assert econ.TABLE != "economic_calendar", "the old raw table's name, retired by needs/screen.py"
    assert DATASETS[econ.NAME].cadence == "hourly" and DATASETS[econ.NAME].kind == "market"


# ── uw/economic_calendar ──────────────────────────────────────────────────────────────────────────────────────

def test_the_calendar_lands_typed_and_reads_back(http_mock):
    http_mock.add("/market/economic-calendar", {"data": [CPI, FOMC, {"event": "no time", "type": "report"}]},
                  headers=UW_HEADERS)
    row = _run(econ.NAME)
    assert row["result"] == "changed" and row["rows"] == 2 and row["calls"] == 1, row
    got = read.economic_calendar()
    assert [(r["event"], r["type"], r["forecast"]) for r in got] == [("CPI", "report", "0.3%"),
                                                                     ("FOMC Minutes", "fomc", None)]
    assert got[0]["snapshot_date"] == date(2026, 11, 9) and got[0]["event_ts"] == datetime(
        2026, 11, 12, 13, 30, tzinfo=timezone.utc).timestamp()
    assert any(i["kind"] == "unkeyed" for i in ops.rows("build_issues"))


def test_a_later_fetch_the_same_day_replaces_the_day_so_a_revision_is_kept(http_mock):
    http_mock.add("/market/economic-calendar", {"data": [CPI, FOMC]}, headers=UW_HEADERS)
    _run(econ.NAME)
    http_mock.routes.clear()
    # The forecast is revised and the minutes move a day: same row count, so the swap's shrink guard is not met.
    moved = {**FOMC, "time": "2026-11-19 19:00:00+00:00"}
    http_mock.add("/market/economic-calendar", {"data": [{**CPI, "forecast": "0.2%"}, moved]}, headers=UW_HEADERS)
    row = _run(econ.NAME)
    assert row["result"] == "changed", row
    got = read.economic_calendar()
    assert [(r["event"], r["forecast"]) for r in got] == [("CPI", "0.2%"), ("FOMC Minutes", None)]
    assert got[1]["event_time"] == "2026-11-19 19:00:00+00:00", "the moved event is not kept twice"
    assert len(list(econ.day_dir(date(2026, 11, 9)).glob("*.json.gz"))) >= 1


def test_an_empty_or_failed_answer_is_a_failed_check_and_keeps_what_is_served(http_mock):
    http_mock.add("/market/economic-calendar", {"data": []}, headers=UW_HEADERS)
    assert _run(econ.NAME)["result"] == "failed"
    assert read.economic_calendar() == []


def test_no_market_file_reads_as_no_rows():
    assert read.economic_calendar() == []


# ── market 4: the headlines left for the News board's own file ───────────────────────────────────────────────

def test_market_4_builds_no_headlines_and_retires_the_live_table():
    """avo's option A (10 Nov 2026): ``boards/news.duckdb`` is the only home of the headlines, so the builder's
    ``ibkr/news`` is gone and a live market 3 file's ``news_headline`` may leave the build."""
    from RevenueChaser.rcdata import scaffold
    assert serving.SERVING_SCHEMA_VERSION["market"] == 4
    assert "ibkr/news" not in DATASETS and not hasattr(read, "news_headlines")
    assert "news_headline" in serving.RETIRED_TABLES["market"]
    assert "news_headline" not in scaffold.SCHEMA_OF_KIND["market"], "a retired table is still built"


def test_a_live_market_3_file_with_headlines_swaps_to_a_build_without_them(http_mock):
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE news_headline AS SELECT range AS i FROM range(5)")
        b.swap({"news_headline": 5})
    http_mock.add("/market/economic-calendar", {"data": [CPI]}, headers=UW_HEADERS)
    assert _run(econ.NAME)["result"] == "changed"
    with serving.open_read("market") as con:
        names = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "news_headline" not in names and econ.TABLE in names


def test_an_hourly_dataset_is_due_again_an_hour_after_its_run(http_mock, monkeypatch):
    http_mock.add("/market/economic-calendar", {"data": [CPI]}, headers=UW_HEADERS)
    _run(econ.NAME)
    assert due.is_due(DATASETS[econ.NAME])[0] is False
    started = ops.run_started_at(ops.latest_check(econ.NAME)["run_id"])
    monkeypatch.setattr(calendar, "_now", lambda: datetime.fromtimestamp(started + 3600, timezone.utc))
    assert due.is_due(DATASETS[econ.NAME])[0] is True


def test_settings_reads_the_hourly_cadence_and_counts_it_like_a_daily_one():
    from RevenueChaser.module_settings import newdata
    assert newdata._cadence(econ.NAME, None) == "hourly"
    check = {"result": "changed", "source_as_of": "2026-11-09", "checked_on": "2026-11-09", "coverage": None}
    assert newdata.status_of(econ.NAME, check, check, "hourly", NOW)[0] == "fresh"


def test_a_timeout_gets_a_second_ladder_after_a_pause_and_the_fetch_lands(http_mock, monkeypatch):
    import httpx
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    pauses, sent = [], []

    def answer(request):
        sent.append(request)
        if len(sent) <= unusual_whales.MAX_ATTEMPTS:            # the client's whole first ladder times out
            raise httpx.ConnectTimeout("timed out", request=request)
        return 200, {"data": [CPI]}, UW_HEADERS

    http_mock.add("/market/economic-calendar", answer)
    row = base.run_request({"dataset": econ.NAME, "params": {}}, sleep=pauses.append)
    assert row["result"] == "changed", row
    assert econ.TRANSPORT_RETRY_PAUSE_S in pauses
    assert len(sent) == unusual_whales.MAX_ATTEMPTS + 1


def test_an_http_error_is_not_retried_after_a_pause(http_mock, monkeypatch):
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)
    pauses = []
    http_mock.add("/market/economic-calendar", {"error": "down"}, status=500, headers=UW_HEADERS)
    assert base.run_request({"dataset": econ.NAME, "params": {}}, sleep=pauses.append)["result"] == "failed"
    assert econ.TRANSPORT_RETRY_PAUSE_S not in pauses
