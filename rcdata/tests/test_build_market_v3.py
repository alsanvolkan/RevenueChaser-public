"""Market 3 (#643): ``uw/economic_calendar`` and ``ibkr/news`` through the builder, UW mocked at the HTTP boundary
and the IBKR Gateway faked at its socket. Both are hourly datasets of the ``market`` kind."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import ibkr_tws as ibkr
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops, read, serving
from RevenueChaser.rcdata.build import DATASETS, base, due, ibkr_news, uw_economic_calendar as econ

from .ibkr_fixture import FakeGateway, frame

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


def test_market_is_version_3_with_both_new_tables():
    assert serving.SERVING_SCHEMA_VERSION["market"] == 3
    assert econ.TABLE != "economic_calendar", "the old raw table's name, retired by needs/screen.py"
    assert {econ.NAME, ibkr_news.NAME} <= set(DATASETS)
    assert DATASETS[econ.NAME].cadence == DATASETS[ibkr_news.NAME].cadence == "hourly"


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
    assert read.economic_calendar() == [] and read.news_headlines() == []


# ── ibkr/news ─────────────────────────────────────────────────────────────────────────────────────────────────

class NewsGateway(FakeGateway):
    """reqContractDetails and reqHistoricalNews only (the News board's own fake, module_news/tests)."""

    def __init__(self):
        super().__init__(last_bar=date(2026, 11, 6))
        self.con_ids = {"AAPL": 265598, "NVDA": 4815747}
        self.news: dict[int, list[tuple]] = {}

    def _answer(self, fields):
        msg = fields[0]
        if msg == "9":
            req_id, symbol = fields[2], fields[4]
            if symbol not in self.con_ids:
                self._out += frame(4, 2, req_id, 200, "No security definition has been found", "")
                return
            self._out += frame(10, req_id, symbol, "STK", "", 0, "", "SMART", "USD", symbol, "NMS", symbol,
                               self.con_ids[symbol], 0.01, "", "ACTIVETIM", "SMART,NASDAQ")
            self._out += frame(52, 1, req_id)
        elif msg == "86":
            req_id, con_id = fields[1], int(fields[2])
            for row in self.news.get(con_id, []):
                self._out += frame(86, req_id, *row)
            self._out += frame(87, req_id, 0)
        else:
            super()._answer(fields)


@pytest.fixture
def gw(monkeypatch):
    g = NewsGateway()
    monkeypatch.setattr(ibkr, "CONNECT", g.connect)
    g.news[265598] = [("2026-11-09 18:20:00.0", "DJ-N", "DJ-N$1", "{A:800015:L:en:K:n/a:C:0.97}!Apple Expands AI Deal"),
                      ("2026-11-08 23:00:00.0", "DJ-RT", "DJ-RT$9", "Tech Stocks Rally as Yields Slip")]
    g.news[4815747] = [("2026-11-09 18:25:00.0", "DJ-N", "DJ-N$3", "Nvidia, Apple Lead Gains"),
                       ("2026-11-08 23:00:00.0", "DJ-N", "DJ-N$dup", "Tech Stocks Rally as Yields Slip")]
    return g


def test_headlines_land_one_row_a_story_and_name_by_utc_publish_day(gw):
    row = _run(ibkr_news.NAME)
    assert row["result"] == "changed" and row["rows"] == 4, row
    assert gw.sent[0][2] == str(ibkr_news.CLIENT_ID) == "75"
    got = read.news_headlines()
    assert [r["headline"] for r in got][:2] == ["Nvidia, Apple Lead Gains", "Apple Expands AI Deal"]
    rally = [r for r in got if r["headline"] == "Tech Stocks Rally as Yields Slip"]
    assert {r["ticker"] for r in rally} == {"AAPL", "NVDA"} and len({r["headline_id"] for r in rally}) == 1
    assert rally[0]["published_date"] == date(2026, 11, 8)
    assert [r["ticker"] for r in read.news_headlines(["nvda"])] == ["NVDA", "NVDA"]
    # The ten fixed names the fake gateway does not know are issues, not a failure.
    assert sum(i["kind"] == "fetch_failed" for i in ops.rows("build_issues")) == len(ibkr_news.BASE_TICKERS) - 2


def test_one_story_and_name_on_two_publish_days_serves_its_first_day_only(gw):
    # The 10 Nov 16:05 builder pass: the same words for AAPL on two UTC days were two partitions holding one key,
    # and the market build died on the news_headline primary key.
    story = "Apple Shares Edge Higher"
    gw.news[265598] = [("2026-11-08 23:50:00.0", "DJ-RT", "DJ-RT$1", story),
                       ("2026-11-09 08:00:00.0", "DJ-N", "DJ-N$2", story)]
    gw.news[4815747] = []
    row = _run(ibkr_news.NAME)
    assert row["result"] == "changed" and row["rows"] == 1, row
    # A later pass brings it again on the second day: a build on the live copy keeps the first day too.
    gw.news[265598] = [("2026-11-09 18:50:00.0", "DJ-N", "DJ-N$3", story)]
    row = _run(ibkr_news.NAME)
    assert row["result"] != "failed", row
    got = [r for r in read.news_headlines() if r["ticker"] == "AAPL"]
    assert [(r["headline"], r["published_date"]) for r in got] == [(story, date(2026, 11, 8))]


def test_the_watch_list_comes_first(gw):
    ops.watch_add("stock", "crwv", by="test")
    assert ibkr_news.tickers(ops.watch_list("stock"))[:2] == ["CRWV", "AAPL"]


def test_a_gateway_that_is_down_is_a_failed_check_and_writes_nothing(monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError("no gateway")
    monkeypatch.setattr(ibkr, "CONNECT", refuse)
    row = _run(ibkr_news.NAME)
    assert row["result"] == "failed" and "gateway" in row["note"].lower(), row
    assert not ibkr_news.day_dir(date(2026, 11, 9)).exists()


def test_an_hourly_dataset_is_due_again_an_hour_after_its_run(gw, monkeypatch):
    _run(ibkr_news.NAME)
    assert due.is_due(DATASETS[ibkr_news.NAME])[0] is False
    started = ops.run_started_at(ops.latest_check(ibkr_news.NAME)["run_id"])
    monkeypatch.setattr(calendar, "_now", lambda: datetime.fromtimestamp(started + 3600, timezone.utc))
    assert due.is_due(DATASETS[ibkr_news.NAME])[0] is True


def test_settings_reads_the_hourly_cadence_and_counts_it_like_a_daily_one():
    from RevenueChaser.module_settings import newdata
    assert newdata._cadence(ibkr_news.NAME, None) == newdata._cadence(econ.NAME, None) == "hourly"
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
