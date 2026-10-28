"""``sec/company_tickers_exchange``: one GET with the builder's User-Agent, ``If-Modified-Since`` from the last copy,
a 304 that writes nothing, a refused empty or malformed file, and a day already fetched that is not fetched again."""
from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import sec_tickers as sec

from . import spine_fixture as fx

LM = "Mon, 26 Oct 2026 22:00:01 GMT"


@pytest.fixture(autouse=True)
def env(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    http_mock.add("company_tickers_exchange.json", fx.SEC_FILE, headers={"last-modified": LM})
    return http_mock


def _run(day: str) -> dict:
    return base.run_request({"dataset": sec.NAME, "params": {"as_of": day}})


def test_one_get_with_the_contact_in_the_user_agent_and_a_typed_snapshot(env):
    row = _run("2026-10-27")
    assert row["result"] == "changed" and row["rows"] == 14 and row["calls"] == 1
    (req,) = env.calls("company_tickers_exchange")
    assert str(req.url) == "https://sec.test/files/company_tickers_exchange.json"
    assert "ops@example.org" in req.headers["user-agent"] and "If-Modified-Since" not in req.headers
    part = sec.parquet_dir() / "date=2026-10-27" / "part-0.parquet"
    rows = duckdb.connect().execute(f"SELECT ticker, cik, exchange, last_modified FROM read_parquet('{part}') "
                                    "ORDER BY ticker").fetchall()
    assert ("HEI-A", "46619", "NYSE", LM) in rows and ("NOEX", "3000002", None, LM) in rows
    with serving.open_read("spine") as con:
        assert con.execute("SELECT count(*) FROM security").fetchone() == (14,)


def test_the_next_fetch_asks_if_modified_since_and_a_304_writes_nothing(env):
    _run("2026-10-27")
    env.routes.insert(0, ("company_tickers_exchange.json", b"", 304, {}))
    row = _run("2026-11-03")
    assert env.calls("company_tickers_exchange")[-1].headers["If-Modified-Since"] == LM
    assert row["result"] == "unchanged" and "not modified since" in row["note"]
    assert not sec.raw_path(date(2026, 11, 3)).exists()
    assert [d for d, _ in sec.__dict__["stage"].partitions(sec.parquet_dir(), "date")] == ["2026-10-27"]


@pytest.mark.parametrize("body", [{"fields": ["cik", "name", "ticker", "exchange"], "data": []},
                                  {"data": [[1, "x", "Y", "NYSE"]]}, "not json"])
def test_an_empty_or_malformed_file_fails_and_is_not_kept(env, body):
    env.routes.insert(0, ("company_tickers_exchange.json", body if not isinstance(body, str) else b"<html>", 200, {}))
    row = _run("2026-10-27")
    assert row["result"] == "failed"
    assert not sec.raw_path(date(2026, 10, 27)).exists()
    assert not serving.live_path("spine").exists()


def test_a_day_already_fetched_is_not_fetched_again(env):
    _run("2026-10-27")
    env.requests.clear()
    row = _run("2026-10-27")
    assert env.calls("company_tickers_exchange") == [] and row["calls"] == 0


def test_the_parse_normalises_tickers_and_keeps_the_first_of_a_repeat():
    body = {"fields": ["cik", "name", "ticker", "exchange"],
            "data": [[1067983, "BERKSHIRE", "BRK.B", "NYSE"], [1067983, "BERKSHIRE AGAIN", "BRK-B", "NYSE"]]}
    assert sec.parse(json.loads(json.dumps(body))) == [
        {"ticker": "BRK-B", "cik": "1067983", "name": "BERKSHIRE", "exchange": "NYSE"}]
