"""``alphavantage/listing_status``: Alpha Vantage's list of every US listing, the ETF universe's first half (#346).

The key travels in the query string, so it is asserted absent from everything kept. The delisted call answered
``{}`` when it was measured (1 Nov 2026), so a non-CSV delisted answer is a build issue and the active list is kept.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import av_listing as av

KEY = "av-test-key-not-a-real-one"
# The header and the ``null`` literal of the real answer of 1 Nov 2026; the rows are picked for the cases.
ACTIVE = ("symbol,name,exchange,assetType,ipoDate,delistingDate,status\r\n"
          "A,Agilent Technologies Inc,NYSE,Stock,1999-11-18,null,Active\r\n"
          "XLK,TECHNOLOGY SELECT SECTOR SPDR FUND ,NYSE ARCA,ETF,1998-12-16,null,Active\r\n"
          "BNKU,Bank of Montreal,NYSE ARCA,ETF,2019-04-03,null,Active\r\n"
          "BRK.B,Berkshire Hathaway Inc,NYSE,Stock,1996-05-09,null,Active\r\n")
DELISTED = ("symbol,name,exchange,assetType,ipoDate,delistingDate,status\r\n"
            "OLDE,Old ETF,NYSE ARCA,ETF,2010-01-04,2024-06-28,Delisted\r\n")


@pytest.fixture
def env(monkeypatch, http_mock):
    monkeypatch.setenv("RC_ALPHAVANTAGE_BASE", "https://av.test")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", KEY)
    monkeypatch.setattr(av, "MIN_ETFS", 1)                   # the fixture's list is a few rows, the real one 5,907
    return http_mock


def _answer(env, active=ACTIVE, delisted=DELISTED):
    env.add("state=delisted", delisted)
    env.add("av.test/query", active)


def _run(day="2026-11-02") -> dict:
    return base.run_request({"dataset": av.NAME, "params": {"as_of": day}}, sleep=lambda _s: None)


def _parquet(day="2026-11-02") -> list[tuple]:
    part = av.parquet_dir() / f"date={day}" / "part-0.parquet"
    return duckdb.connect().execute(
        f"SELECT ticker, symbol, name, asset_type, ipo_date, delisting_date, status FROM read_parquet('{part}') "
        "ORDER BY ticker").fetchall()


def _kept_bytes() -> bytes:
    return b"".join(p.read_bytes() for p in base.raw_root().rglob("*") if p.is_file())


def test_two_calls_the_active_list_and_the_delisted_one_typed_into_one_snapshot(env):
    _answer(env)
    row = _run()
    assert row["result"] == "changed", row["note"]
    assert row["calls"] == 2
    active, delisted = env.calls("av.test/query")
    assert active.url.params["function"] == "LISTING_STATUS" and "state" not in active.url.params
    assert delisted.url.params["state"] == "delisted"
    rows = _parquet()
    assert ("XLK", "XLK", "TECHNOLOGY SELECT SECTOR SPDR FUND", "ETF", date(1998, 12, 16), None, "Active") in rows
    assert ("BRK-B", "BRK.B", "Berkshire Hathaway Inc", "Stock", date(1996, 5, 9), None, "Active") in rows
    assert ("OLDE", "OLDE", "Old ETF", "ETF", date(2010, 1, 4), date(2024, 6, 28), "Delisted") in rows
    assert "2 ETF, 2 Stock active; 1 delisted" in row["note"]


def test_the_key_is_never_kept_and_never_in_the_note(env):
    _answer(env)
    row = _run()
    assert KEY.encode() not in _kept_bytes() and KEY not in (row["note"] or "")


def test_no_key_asks_nothing_and_says_so(env, monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY")
    base.parquet_root().mkdir(parents=True)                 # a store that has built before: only the key is missing
    row = _run()
    assert row["result"] == "unchanged" and "no ALPHAVANTAGE_API_KEY" in row["note"]
    assert not env.requests
    assert [i["kind"] for i in ops.open_issues(av.NAME)[1]] == ["missing_key"]


def test_a_non_csv_delisted_answer_is_an_issue_and_the_active_list_is_kept(env):
    """Measured 1 Nov 2026: ``state=delisted`` answered ``{}`` (2 bytes)."""
    _answer(env, delisted="{}")
    row = _run()
    assert row["result"] == "changed", row["note"]
    assert {r[0] for r in _parquet()} == {"A", "XLK", "BNKU", "BRK-B"}
    assert [(i["kind"], i["key"]) for i in ops.open_issues(av.NAME)[1]] == [("no_rows", "delisted")]
    assert "delisted list not read" in row["note"]


@pytest.mark.parametrize("body", ['{"Information": "We have detected your API key as %s ..."}' % KEY,
                                  '{"Note": "Thank you for using Alpha Vantage!"}', "symbol,name\r\nXLK,x\r\n",
                                  "symbol,name,exchange,assetType,ipoDate,delistingDate,status\r\n"])
def test_a_rate_limit_error_or_empty_active_answer_fails_and_keeps_nothing(env, body):
    _answer(env, active=body)
    row = _run()
    assert row["result"] == "failed" and "alphavantage listing status" in row["note"]
    assert KEY not in row["note"] and not list(base.raw_root().rglob("*.csv.gz"))


def test_a_day_already_fetched_is_not_fetched_again(env):
    _answer(env)
    _run()
    env.requests.clear()
    assert _run()["calls"] == 0 and not env.requests


def test_the_build_stages_the_newest_snapshot_only(env):
    _answer(env)
    _run("2026-11-02")
    env.routes.clear()
    _answer(env, active=ACTIVE.replace("A,Agilent Technologies Inc,NYSE,Stock,1999-11-18,null,Active\r\n", ""))
    row = _run("2026-11-03")
    assert "2 ETF, 1 Stock active" in row["note"]


def test_it_is_a_daily_spine_dataset_that_starts_by_hand_inside_the_alpha_vantage_cap():
    from RevenueChaser.rcdata import caps
    from RevenueChaser.rcdata.build import DATASETS
    ds = DATASETS[av.NAME]
    assert ds.kind == "spine" and ds.cadence == "daily" and ds.starts_by_hand and ds.providers == ("alphavantage",)
    assert caps.DAY_CAPS["alphavantage"] == 25


# ── Review round 1 of PR 349 ──────────────────────────────────────────────────────────────────────────────────

def test_a_list_under_the_floor_fails_and_is_not_kept_as_the_days_resume_marker(env, monkeypatch):
    """S4: a truncated answer must not become today's list (its raw file would stop every retry that day)."""
    monkeypatch.setattr(av, "MIN_ETFS", 3)
    _answer(env)
    row = _run()
    assert row["result"] == "failed" and "2 ETF rows" in row["note"] and "under 3" in row["note"]
    assert not av.raw_path(date(2026, 11, 2)).exists()


def test_a_list_that_lost_a_fifth_of_its_etfs_since_the_last_snapshot_fails(env):
    _answer(env)
    assert _run("2026-11-02")["result"] == "changed"
    env.routes.clear()
    truncated = "".join(line + "\r\n" for line in ACTIVE.split("\r\n")[:3] if line)   # header, A, XLK
    _answer(env, active=truncated)
    row = _run("2026-11-03")
    assert row["result"] == "failed" and "1 ETF rows" in row["note"] and "80% of the 2" in row["note"]
    assert not av.raw_path(date(2026, 11, 3)).exists()


def test_the_delisted_issue_stays_one_open_row_and_resolves_when_the_list_answers(env):
    """S5: the delisted call answers ``{}`` every day for now; that is one open issue, not one a day."""
    _answer(env, delisted="{}")
    _run("2026-11-02")
    _run("2026-11-03")
    _run("2026-11-04")
    assert [(i["kind"], i["key"]) for i in ops.open_issues(av.NAME)[1]] == [("no_rows", "delisted")]
    env.routes.clear()
    _answer(env)
    _run("2026-11-05")
    assert ops.open_issues(av.NAME)[0] == 0


def test_a_failed_run_is_not_due_again_until_the_next_new_york_day(monkeypatch):
    """S6: the hourly pass must not spend Alpha Vantage's 25 a day retrying a failure every hour."""
    from datetime import datetime, timezone
    from RevenueChaser.core import calendar
    from RevenueChaser.rcdata.build import DATASETS, due
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))
    ops.record_check(av.NAME, "changed", source_as_of="2026-11-19", checked_on="2026-11-19")
    ops.record_check(av.NAME, "failed", note="alphavantage listing status: a JSON answer", checked_on="2026-11-20")
    due_now, why = due.is_due(DATASETS[av.NAME])
    assert not due_now and "next New York day" in why
    # The day's raw file is held: a rerun reuses it at 0 calls, so a failed build or swap retries hourly (S6a).
    base.write_raw(av.raw_path(date(2026, 11, 20)), ACTIVE.encode())
    due_now, why = due.is_due(DATASETS[av.NAME])
    assert due_now and "the last check failed" in why
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc))
    assert due.is_due(DATASETS[av.NAME])[0] is True
    # Narrow: a keyless dataset's failure is still due on the next hourly pass.
    ops.record_check("sec/ncen", "changed", checked_on="2026-11-19")
    ops.record_check("sec/ncen", "failed", note="x", checked_on="2026-11-21")
    assert due.is_due(DATASETS["sec/ncen"])[0] is True


def test_the_earnings_calendar_shares_the_hold_and_its_raw_file_lifts_it(monkeypatch):
    """S6a for the other Alpha Vantage dataset: no raw file for the day, held; the day's raw file held, due."""
    from datetime import datetime, timezone
    from RevenueChaser.core import calendar
    from RevenueChaser.rcdata.build import DATASETS, due
    from RevenueChaser.rcdata.build.fundamentals import earnings
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))
    ops.record_check(earnings.NAME, "changed", source_as_of="2026-11-19", checked_on="2026-11-19")
    ops.record_check(earnings.NAME, "failed", note="x", checked_on="2026-11-20")
    assert due.is_due(DATASETS[earnings.NAME])[0] is False
    base.write_raw(earnings.raw_path(DATASETS[earnings.NAME].default_as_of()), b"symbol\r\n")
    assert due.is_due(DATASETS[earnings.NAME])[0] is True


def test_the_key_is_in_no_parquet_and_no_log_record_of_ours(env, caplog):
    """S7. httpx's own request log carries the query; the builder silences it (``rcdata/build/__main__.py``), so
    only our loggers are read here."""
    import logging
    _answer(env, delisted="{}")
    with caplog.at_level(logging.DEBUG):
        row = _run()
    assert row["result"] == "changed"
    parquet = b"".join(p.read_bytes() for p in base.parquet_root().rglob("*") if p.is_file())
    assert parquet and KEY.encode() not in parquet
    ours = [r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert ours and not [m for m in ours if KEY in m]


def test_a_5xx_fails_with_the_url_and_never_the_key(env):
    env.add("av.test/query", b"down", status=500)
    env.routes.reverse()                                          # the 500 first
    row = _run()
    assert row["result"] == "failed" and "HTTP 500" in row["note"] and "av.test/query" in row["note"]
    assert KEY not in row["note"] and row["calls"] == base.HTTP_ATTEMPTS
