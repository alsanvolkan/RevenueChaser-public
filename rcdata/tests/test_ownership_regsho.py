"""``regsho/threshold``: one file per market and day (Nasdaq's by name, NYSE's three markets by query), parsed by its
header, an empty list or a 404 of a recent day asked again, of an older day kept (empty) or marked absent, an answer
that is not the list refused, and each symbol named by the spine."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import read, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import regsho

from . import ownership_fixture as ox


@pytest.fixture
def src(monkeypatch, http_mock):
    monkeypatch.setenv("RC_REGSHO_NASDAQ_BASE", "https://regsho.test/dynamic/symdir/regsho")
    monkeypatch.setenv("RC_NYSE_BASE", "https://nyse.test")
    feed = ox.RegSho()
    feed.nasdaq["20261027"] = ox.nasdaq_list(["AAPD|Direxion Daily AAPL Bear 1X Shares|G|Y|N|",
                                              "GME|GameStop Corp. Class A|Q|Y|Y|"])
    feed.nyse[("27-Oct-2026", "NYSE")] = ox.nyse_list(["BRK.B|BERKSHIRE HATHAWAY INC|N|Y||"])
    feed.install(http_mock)
    ox.make_spine()
    return feed


def _run(day: str, **params) -> dict:
    return base.run_request({"dataset": regsho.NAME, "params": {"as_of": day, **params}})


def _served(sql: str, *args) -> list[tuple]:
    with serving.open_read("ownership") as con:
        return con.execute(sql, list(args)).fetchall()


def test_the_first_run_reads_each_market_list_of_its_day(src, http_mock):
    row = _run("2026-10-27")
    assert row["result"] == "changed" and row["rows"] == 3 and row["calls"] == 4
    assert [str(c.url).rsplit("/", 1)[1] for c in http_mock.calls("regsho.test")] == ["nasdaqth20261027.txt"]
    assert sorted(c.url.params["market"] for c in http_mock.calls("nyse.test")) == ["NYSE", "NYSE American",
                                                                                   "NYSE Arca"]
    assert {c.url.params["selectedDate"] for c in http_mock.calls("nyse.test")} == {"27-Oct-2026"}
    assert _served("SELECT list_date, market, symbol, ticker, threshold_flag, rule_3210, file_stamp "
                   "FROM regsho_threshold ORDER BY market, symbol") == [
        (date(2026, 10, 27), "nasdaq", "AAPD", None, "Y", "N", "20261027230025"),
        (date(2026, 10, 27), "nasdaq", "GME", "GME", "Y", "Y", "20261027230025"),
        (date(2026, 10, 27), "nyse", "BRK-B", "BRK-B", "Y", None, "20261027220001")]


def test_an_empty_list_of_a_recent_day_is_asked_again_and_kept_once_the_day_is_older(src, http_mock):
    _run("2026-10-27")
    assert not regsho.raw_path("nyse_american", date(2026, 10, 27)).exists()
    http_mock.requests.clear()
    _run("2026-10-30")
    asked = {(c.url.params["selectedDate"], c.url.params["market"]) for c in http_mock.calls("nyse.test")}
    assert ("27-Oct-2026", "NYSE American") in asked and ("27-Oct-2026", "NYSE") not in asked
    assert regsho.raw_path("nyse_american", date(2026, 10, 27)).exists()
    assert not regsho.raw_path("nyse_american", date(2026, 10, 30)).exists()


def test_a_day_nasdaq_has_no_file_for_is_marked_absent_once_older_and_not_asked_again(src, http_mock):
    row = base.run_request({"dataset": regsho.NAME, "params": {"mode": "backfill", "from": "2026-10-26",
                                                               "to": "2026-10-30"}})
    assert row["result"] == "changed"
    assert regsho.none_path("nasdaq", date(2026, 10, 26)).exists()
    assert not regsho.none_path("nasdaq", date(2026, 10, 30)).exists()       # recent: not published yet
    http_mock.requests.clear()
    base.run_request({"dataset": regsho.NAME, "params": {"mode": "backfill", "from": "2026-10-26",
                                                         "to": "2026-10-30"}})
    names = [str(c.url).rsplit("/", 1)[1] for c in http_mock.calls("regsho.test")]
    assert "nasdaqth20261026.txt" not in names and "nasdaqth20261030.txt" in names


def test_an_answer_that_is_not_the_list_is_refused_and_not_kept(src):
    src.nasdaq["20261027"] = b"<html><body>Service unavailable</body></html>"
    row = _run("2026-10-27")
    assert row["result"] == "failed" and "not the threshold list" in row["note"]
    assert not regsho.raw_path("nasdaq", date(2026, 10, 27)).exists()


def test_the_default_day_is_the_session_before_today(monkeypatch):
    from RevenueChaser.core import calendar
    monkeypatch.setattr(calendar, "today_ny", lambda: date(2026, 10, 26))       # a Monday
    assert regsho.DATASET.default_as_of() == date(2026, 10, 23)


def test_the_read_api_gives_the_newest_list_or_a_days(src):
    assert read.threshold_list() == []
    _run("2026-10-27")
    rows = read.threshold_list()
    assert [(r["market"], r["symbol"], r["ticker"]) for r in rows] == [
        ("nasdaq", "AAPD", None), ("nasdaq", "GME", "GME"), ("nyse", "BRK-B", "BRK-B")]
    assert {r["list_date"] for r in rows} == {date(2026, 10, 27)} and {r["source"] for r in rows} == {regsho.NAME}
    assert read.threshold_list(on=date(2026, 10, 26)) == []


def test_a_redirect_to_the_same_file_is_the_file_not_an_absent_day(src, http_mock):
    """Review S4: only a redirect away from the file (Nasdaq's not-found page) means no file. A redirect to the same
    file elsewhere (http to https, a CDN host) is read, and an older day is never marked absent for it."""
    def moved(request):
        return 302, b"", {"location": str(request.url).replace("https://nyse.test", "https://cdn.nyse.test")}
    http_mock.routes.insert(0, ("https://nyse.test/api/regulatory/threshold-securities/download", moved, 302, {}))
    src.nyse[("20-Oct-2026", "NYSE")] = ox.nyse_list(["BRK.B|BERKSHIRE HATHAWAY INC|N|Y||"])
    _run("2026-10-27", mode="backfill", **{"from": "2026-10-20", "to": "2026-10-27"})
    assert regsho.raw_path("nyse", date(2026, 10, 20)).exists()
    assert not regsho.none_path("nyse", date(2026, 10, 20)).exists()
    assert _served("SELECT market, symbol FROM regsho_threshold WHERE list_date = '2026-10-20'") == [("nyse", "BRK-B")]
    assert regsho.none_path("nasdaq", date(2026, 10, 20)).exists()            # Nasdaq's not-found page: absent
