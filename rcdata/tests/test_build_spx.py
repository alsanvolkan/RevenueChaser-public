"""``uw/spot_expiry_strike`` through the builder, with UW mocked at the HTTP boundary."""
from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import parse_qs

import duckdb
import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import ops, read, serving
from RevenueChaser.rcdata.build import base, uw_spot_expiry_strike as uw

from . import uw_fixture

UW_HEADERS = {"x-uw-token-req-limit": "150000", "x-uw-req-per-minute-remaining": "119",
              "content-type": "application/json"}


@pytest.fixture(autouse=True)
def uw_env(monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc))


def _daily(as_of="2026-10-26"):
    return base.run_request({"dataset": uw.NAME, "params": {"as_of": as_of}})


def test_one_call_per_daily_run_with_the_two_nearest_expiries(http_mock):
    http_mock.add("/stock/SPX/spot-exposures/expiry-strike", uw_fixture.body(), headers=UW_HEADERS)
    row = _daily()
    assert row["result"] == "changed" and row["rows"] == 391 and row["calls"] == 1
    reqs = http_mock.calls("expiry-strike")
    assert len(reqs) == 1
    query = parse_qs(reqs[0].url.query.decode())
    assert query["expirations[]"] == ["2026-10-26", "2026-10-27"]
    assert query["date"] == ["2026-10-26"], "every call names its session (B1)"
    assert reqs[0].headers["authorization"].startswith("Bearer ")


def test_the_391_rows_land_typed_with_their_provenance(http_mock):
    http_mock.add("expiry-strike", uw_fixture.body(), headers=UW_HEADERS)
    _daily()
    with serving.open_read("market") as con:
        types = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                                 "WHERE table_name = 'spx_gamma_expiry_strike'").fetchall())
        by_dte = dict(con.execute("SELECT dte, count(*) FROM spx_gamma_expiry_strike GROUP BY dte").fetchall())
        lo, hi = con.execute("SELECT min(strike), max(strike) FROM spx_gamma_expiry_strike").fetchone()
        version = con.execute("SELECT kind, version FROM serving_schema_version").fetchone()
    assert by_dte == {0: 155, 1: 236} and (lo, hi) == (4800.0, 9200.0)
    assert version == ("market", 1)
    for f in uw.NUMERIC_FIELDS:
        assert types[f] == "DOUBLE", f
    assert types["session_date"] == "DATE" and types["observed"] == "BOOLEAN" and types["fetched_at"] == "DOUBLE"
    rows = read.spx_expiry_strike(date(2026, 10, 26))
    assert len(rows) == 391 and rows[0]["observed"] is True and rows[0]["source"] == "uw"
    assert rows[0]["status"] == "changed" and rows[0]["snapshot_time"] == "2026-10-26T20:15:00Z"
    assert isinstance(rows[0]["call_gamma_oi"], float)
    assert uw.raw_path(date(2026, 10, 26)).exists() and uw.partition_path(date(2026, 10, 26)).exists()


def test_a_day_whose_raw_file_exists_makes_no_call(http_mock):
    http_mock.add("expiry-strike", uw_fixture.body(), headers=UW_HEADERS)
    _daily()
    second = _daily()
    assert len(http_mock.calls("expiry-strike")) == 1
    assert second["result"] == "unchanged" and second["calls"] == 0 and second["rows"] == 391


def test_uw_remaining_and_the_call_counter_are_written(http_mock):
    http_mock.add("expiry-strike", uw_fixture.body(), headers=UW_HEADERS)
    _daily()
    with ops.connect() as con:
        sample = con.execute("SELECT token_req_limit, req_per_minute_remaining FROM uw_remaining").fetchall()
        calls = con.execute("SELECT provider, calls, failures, http_429 FROM provider_calls_daily").fetchall()
        run = con.execute("SELECT dataset, status FROM job_runs2").fetchall()
    assert [tuple(s) for s in sample] == [(150000, 119)]
    assert [tuple(c) for c in calls] == [("uw", 1, 0, 0)]
    assert [tuple(r) for r in run] == [(uw.NAME, "ok")]


def test_a_429_counts_in_http_429_and_the_run_fails_without_a_serving_file(http_mock):
    http_mock.add("expiry-strike", {"error": "slow down"}, status=429, headers={"Retry-After": "1"})
    row = _daily()
    assert row["result"] == "failed" and "HTTP 429" in row["note"]
    with ops.connect() as con:
        calls = con.execute("SELECT calls, failures, http_429 FROM provider_calls_daily WHERE provider='uw'").fetchone()
    assert tuple(calls) == (unusual_whales.MAX_ATTEMPTS, 1, unusual_whales.MAX_ATTEMPTS)
    assert not serving.live_path("market").exists()


def test_a_backfill_stops_once_the_uw_breaker_opens(http_mock):
    http_mock.add("expiry-strike", {"error": "down"}, status=503)
    row = base.run_request({"dataset": uw.NAME, "params": {"mode": "backfill", "from": "2026-09-01",
                                                            "to": "2026-10-23"}})
    assert row["result"] == "failed" and "breaker_open" in row["note"]
    assert len(http_mock.calls("expiry-strike")) == unusual_whales.BREAKER_THRESHOLD * unusual_whales.MAX_ATTEMPTS


def test_dte_counts_sessions():
    assert uw.dte(date(2026, 10, 23), date(2026, 10, 26)) == 1       # Friday -> Monday
    assert uw.dte(date(2026, 10, 26), date(2026, 10, 26)) == 0


def test_the_response_observer_changes_no_result(http_mock):
    """The header-sampling hook (#206) on the UW client: old callers see the same results, a raising observer is
    ignored."""
    http_mock.add("/stock/AAPL/info", {"data": {"ticker": "AAPL"}}, headers=UW_HEADERS)
    http_mock.add("/stock/NOPE/info", {"error": "x"}, status=404)
    plain = unusual_whales.UWClient(key="k")
    seen = []
    observed = unusual_whales.UWClient(key="k")
    observed.on_response = lambda ep, r: seen.append((ep, r.status_code, r.headers.get("x-uw-token-req-limit")))
    broken = unusual_whales.UWClient(key="k")
    broken.on_response = lambda ep, r: 1 / 0
    for ep in ("/stock/AAPL/info", "/stock/NOPE/info"):
        a, b, c = plain.get_result(ep), observed.get_result(ep), broken.get_result(ep)
        assert (a.status, a.data, a.error) == (b.status, b.data, b.error) == (c.status, c.data, c.error)
    assert plain.health() == observed.health() == broken.health()
    assert seen == [("/stock/AAPL/info", 200, "150000"), ("/stock/NOPE/info", 404, None)]


def test_a_run_during_the_next_session_asks_for_the_closed_day_by_date(http_mock, monkeypatch):
    """B1: at 10:30 New York on Tue 27 Oct the latest complete session is the 26th; without date= UW would answer
    the 27th's intraday snapshot and it would be saved as the 26th, for good (the raw file is the resume marker)."""
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc))
    http_mock.add("expiry-strike", uw_fixture.body(date(2026, 10, 26)), headers=UW_HEADERS)
    row = base.run_request({"dataset": uw.NAME, "params": {}})
    assert row["result"] == "changed" and row["source_as_of"] == "2026-10-26"
    query = parse_qs(http_mock.calls("expiry-strike")[0].url.query.decode())
    assert query["date"] == ["2026-10-26"] and query["expirations[]"] == ["2026-10-26", "2026-10-27"]
    rows = read.spx_expiry_strike(date(2026, 10, 26))
    assert len(rows) == 391 and {r["observed"] for r in rows} == {False}, "inside the next session it is not observed"


def test_an_answer_stamped_on_another_day_is_refused_and_leaves_no_raw_file(http_mock, monkeypatch):
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc))
    http_mock.add("expiry-strike", uw_fixture.body(date(2026, 10, 27)), headers=UW_HEADERS)   # the 27th's rows
    row = base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-26"}})
    assert row["result"] == "failed" and "not on 2026-10-26" in row["note"]
    assert not uw.raw_path(date(2026, 10, 26)).exists(), "a wrong answer must not become the resume marker"


def test_rows_of_another_day_inside_a_good_answer_are_dropped_with_an_issue(http_mock):
    body = uw_fixture.body()
    body["data"][0]["time"] = "2026-10-27T14:00:00Z"
    http_mock.add("expiry-strike", body, headers=UW_HEADERS)
    row = _daily()
    assert row["rows"] == 390
    with ops.connect() as con:
        kinds = [r[0] for r in con.execute("SELECT kind FROM build_issues")]
    assert kinds == ["wrong_session"]


def test_s6_an_answer_with_no_usable_row_fails_instead_of_marking_the_day_done(http_mock):
    body = uw_fixture.body()
    for r in body["data"]:
        del r["strike"]
    http_mock.add("expiry-strike", body, headers=UW_HEADERS)
    row = _daily()
    assert row["result"] == "failed" and "no usable row" in row["note"]
    assert not uw.partition_path(date(2026, 10, 26)).exists()
