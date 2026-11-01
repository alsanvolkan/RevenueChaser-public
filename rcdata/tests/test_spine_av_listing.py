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
    return http_mock


def _answer(env, active=ACTIVE, delisted=DELISTED):
    env.add("state=delisted", delisted)
    env.add("av.test/query", active)


def _run(day="2026-11-02") -> dict:
    return base.run_request({"dataset": av.NAME, "params": {"as_of": day}})


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
    _answer(env, active=ACTIVE.replace("BNKU,Bank of Montreal,NYSE ARCA,ETF,2019-04-03,null,Active\r\n", ""))
    row = _run("2026-11-03")
    assert "1 ETF, 2 Stock active" in row["note"]


def test_it_is_a_daily_spine_dataset_that_starts_by_hand_inside_the_alpha_vantage_cap():
    from RevenueChaser.rcdata import caps
    from RevenueChaser.rcdata.build import DATASETS
    ds = DATASETS[av.NAME]
    assert ds.kind == "spine" and ds.cadence == "daily" and ds.starts_by_hand and ds.providers == ("alphavantage",)
    assert caps.DAY_CAPS["alphavantage"] == 25
