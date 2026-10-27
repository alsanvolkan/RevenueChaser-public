"""Backfill: one call per past session, an existing partition skipped, an interrupted run resumed where it stopped."""
from __future__ import annotations

from datetime import date, datetime, timezone
from urllib.parse import parse_qs

import pytest

from RevenueChaser.core import calendar
from RevenueChaser.core.providers import unusual_whales
from RevenueChaser.rcdata import read, serving
from RevenueChaser.rcdata.build import base, uw_spot_expiry_strike as uw

from . import uw_fixture

DAYS = [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]


@pytest.fixture(autouse=True)
def env(monkeypatch, http_mock):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "test-key")
    monkeypatch.setattr(unusual_whales, "BASE", "https://uw.test/api")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc))

    def answer(request):
        day = date.fromisoformat(parse_qs(request.url.query.decode())["date"][0])
        return 200, uw_fixture.body(day), {}
    http_mock.add("expiry-strike", answer)


def _backfill(start="2026-10-19", end="2026-10-23"):
    return base.run_request({"dataset": uw.NAME, "params": {"mode": "backfill", "from": start, "to": end}})


def _asked(http_mock) -> list[str]:
    return [parse_qs(r.url.query.decode())["date"][0] for r in http_mock.calls("expiry-strike")]


def test_one_call_per_session_with_the_date_and_that_sessions_expiries(http_mock):
    row = _backfill()
    assert row["result"] == "changed" and row["rows"] == 391 * len(DAYS) and row["calls"] == len(DAYS)
    assert _asked(http_mock) == [d.isoformat() for d in DAYS]
    friday = parse_qs(http_mock.calls("expiry-strike")[-1].url.query.decode())
    assert friday["expirations[]"] == ["2026-10-23", "2026-10-26"]
    rows = read.spx_expiry_strike(date(2026, 10, 23))
    assert len(rows) == 391 and {r["observed"] for r in rows} == {False}


def test_an_existing_partition_is_skipped(http_mock):
    _backfill("2026-10-20", "2026-10-21")
    http_mock.requests.clear()
    row = _backfill()
    assert _asked(http_mock) == ["2026-10-19", "2026-10-22", "2026-10-23"]
    assert row["rows"] == 391 * len(DAYS)


def test_a_daily_row_is_never_overwritten_by_the_backfill(http_mock, monkeypatch):
    http_mock.routes.insert(0, ("expiry-strike?expirations", uw_fixture.body(date(2026, 10, 23)), 200, {}))
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc))
    base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-23"}})
    http_mock.routes.pop(0)
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc))
    _backfill()
    assert {r["observed"] for r in read.spx_expiry_strike(date(2026, 10, 23))} == {True}
    assert {r["observed"] for r in read.spx_expiry_strike(date(2026, 10, 22))} == {False}


def test_a_run_interrupted_mid_write_after_day_n_resumes_at_n_plus_1(http_mock, monkeypatch):
    """Killed while day N+1's Parquet is being written: the half file must not pass for a done day."""
    real_copy = base._copy
    n_plus_1 = uw.partition_path(DAYS[2])

    def dying_copy(con, select, path):
        if path.parent == n_plus_1.parent:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"PAR1 half a file")       # what a killed writer leaves behind
            raise KeyboardInterrupt("killed")
        real_copy(con, select, path)

    monkeypatch.setattr(base, "_copy", dying_copy)
    with pytest.raises(KeyboardInterrupt):
        _backfill()
    assert _asked(http_mock) == [d.isoformat() for d in DAYS[:3]]
    assert not serving.live_path("market").exists()

    monkeypatch.setattr(base, "_copy", real_copy)
    http_mock.requests.clear()
    row = _backfill()
    # Days 1-2 have partitions; day 3's raw file is whole, so it is typed again without a call; days 4-5 are fetched.
    assert _asked(http_mock) == [d.isoformat() for d in DAYS[3:]]
    assert row["result"] == "changed" and row["rows"] == 391 * len(DAYS)
    assert len(read.spx_expiry_strike(DAYS[2])) == 391


def test_a_rerun_of_an_older_session_asks_for_that_day(http_mock):
    base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-22"}})
    assert _asked(http_mock) == ["2026-10-22"]
    assert {r["observed"] for r in read.spx_expiry_strike(date(2026, 10, 22))} == {False}


def test_the_backfill_never_asks_before_uw_serves_the_data(http_mock):
    _backfill("2025-01-10", "2025-01-17")
    assert _asked(http_mock) == ["2025-01-16", "2025-01-17"]


def test_n1_one_unusable_answer_is_quarantined_and_the_backfill_goes_on(http_mock):
    """Review N1: the 21st answered without ``strike``. The day is set aside (its raw file renamed .rejected, a
    type_failed issue), the 22nd and 23rd still land, and a later run asks for the 21st again."""
    bad = {"2026-10-21"}

    def answer(request):
        day = parse_qs(request.url.query.decode())["date"][0]
        body = uw_fixture.body(date.fromisoformat(day))
        if day in bad:
            for r in body["data"]:
                del r["strike"]
        return 200, body, {}
    http_mock.routes.clear()
    http_mock.add("expiry-strike", answer)
    row = _backfill()
    assert row["result"] == "changed" and row["rows"] == 391 * 4
    raw = uw.raw_path(DAYS[2])
    assert not raw.exists() and raw.with_name(raw.name + ".rejected").exists()
    from RevenueChaser.rcdata import ops
    with ops.connect() as con:
        kinds = [r[0] for r in con.execute("SELECT kind FROM build_issues WHERE key = '2026-10-21' ORDER BY id")]
    assert kinds == ["bad_row", "type_failed"], "one issue for the rows, one for the day"

    bad.clear()
    http_mock.requests.clear()
    row = _backfill()
    assert _asked(http_mock) == ["2026-10-21"] and row["rows"] == 391 * 5


def test_n1_a_daily_run_with_an_unusable_answer_fetches_again_next_time(http_mock):
    body = uw_fixture.body(DAYS[2])
    for r in body["data"]:
        del r["strike"]
    http_mock.routes.clear()
    http_mock.add("expiry-strike", body)
    assert base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-21"}})["result"] == "failed"
    http_mock.routes.clear()
    http_mock.add("expiry-strike", uw_fixture.body(DAYS[2]))
    assert base.run_request({"dataset": uw.NAME, "params": {"as_of": "2026-10-21"}})["result"] == "changed"
    assert len(http_mock.calls("expiry-strike")) == 2
