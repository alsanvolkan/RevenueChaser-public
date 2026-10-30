"""``sec/company_tickers_mf``: the SEC fund file, the ETF rule, and the served ``fund`` table.

The fetch is the equity file's shape (one GET with the builder's User-Agent, ``If-Modified-Since`` from the last
copy, a 304 that writes nothing, a refused empty or malformed body), so those are asserted once here and not
re-derived. What is new, and what most of this file is about, is the rule that decides ``is_etf`` and the two
exceptions the fund file does not hold.
"""
from __future__ import annotations

import json
from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.spine import sec_tickers, sec_tickers_mf as mf

from . import spine_fixture as fx

LM = "Thu, 29 Oct 2026 21:00:02 GMT"


@pytest.fixture(autouse=True)
def env(monkeypatch, http_mock):
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    http_mock.add("company_tickers_mf.json", fx.SEC_MF_FILE, headers={"last-modified": LM})
    http_mock.add("company_tickers_exchange.json", fx.SEC_FILE, headers={"last-modified": LM})
    return http_mock


def _run(day: str = "2026-10-29", dataset: str = mf.NAME) -> dict:
    return base.run_request({"dataset": dataset, "params": {"as_of": day}})


def _fund(select: str = "SELECT ticker FROM fund ORDER BY ticker") -> list[tuple]:
    with serving.open_read("spine") as con:
        return con.execute(select).fetchall()


# ── The fetch ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_one_get_with_the_contact_in_the_user_agent_and_a_typed_snapshot(env):
    row = _run()
    assert row["result"] == "changed" and row["calls"] == 1
    (req,) = env.calls("company_tickers_mf")
    assert str(req.url) == "https://sec.test/files/company_tickers_mf.json"
    assert "ops@example.org" in req.headers["user-agent"] and "If-Modified-Since" not in req.headers
    part = mf.parquet_dir() / "date=2026-10-29" / "part-0.parquet"
    rows = duckdb.connect().execute(
        f"SELECT ticker, ticker_raw, cik, series_id, class_id, is_etf, is_etf_source, last_modified "
        f"FROM read_parquet('{part}') ORDER BY ticker").fetchall()
    assert ("XLK", "XLK", "1067839", "S000019524", "C000067288", True, mf.ETF_RULE, LM) in rows
    assert ("ELFNX", "elfnx", "2110", "S000009185", "C000024958", False, mf.ETF_RULE, LM) in rows


def test_the_next_fetch_asks_if_modified_since_and_a_304_writes_nothing(env):
    _run()
    env.routes.insert(0, ("company_tickers_mf.json", b"", 304, {}))
    row = _run("2026-11-05")
    assert env.calls("company_tickers_mf")[-1].headers["If-Modified-Since"] == LM
    assert row["result"] == "unchanged" and "not modified since" in row["note"]
    assert not mf.raw_path(date(2026, 11, 5)).exists()


@pytest.mark.parametrize("body", [{"fields": ["cik", "seriesId", "classId", "symbol"], "data": []},
                                  {"data": [[1, "S1", "C1", "XLK"]]}, "not json"])
def test_an_empty_or_malformed_file_fails_and_is_not_kept(env, body):
    env.routes.insert(0, ("company_tickers_mf.json", body if not isinstance(body, str) else b"<html>", 200, {}))
    row = _run()
    assert row["result"] == "failed"
    assert not mf.raw_path(date(2026, 10, 29)).exists()
    assert not serving.live_path("spine").exists()


def test_a_day_already_fetched_is_not_fetched_again(env):
    _run()
    env.requests.clear()
    row = _run()
    assert env.calls("company_tickers_mf") == [] and row["calls"] == 0


# ── The ETF rule ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_rule_drops_the_five_letter_x_share_classes_and_keeps_the_rest():
    rows = mf.parse(json.loads(json.dumps(fx.SEC_MF_FILE)))
    assert tuple(sorted(r["ticker"] for r in rows if r["is_etf"])) == fx.MF_ETF_TICKERS
    assert sorted(r["ticker"] for r in rows if not r["is_etf"]) == ["ACRNX", "ELFNX", "LACAX"]


def test_every_row_says_which_rule_decided_it_rather_than_a_bare_boolean():
    rows = mf.parse(json.loads(json.dumps(fx.SEC_MF_FILE)))
    assert {r["is_etf_source"] for r in rows} == {mf.ETF_RULE}
    assert mf.ETF_RULE == "length_rule_v1"


def test_the_rule_runs_on_the_normalised_ticker_so_a_lower_case_share_class_is_dropped():
    """The real file holds 25 lower-case 5-letter symbols. Reading the rule off the raw symbol keeps them, which is
    how the brief's 5,841 candidates came out 25 too high; the rule reads the normalised ticker."""
    assert mf.is_etf("elfnx") is False and mf.is_etf("ELFNX") is False


@pytest.mark.parametrize("ticker,etf", [("XLK", True), ("SOXX", True), ("AAAX", True), ("QINSQ", True),
                                        ("LACAX", False), ("ACRNX", False)])
def test_the_rules_edge_cases(ticker, etf):
    """A 4-letter fund ending in X and a 5-letter symbol not ending in X are both kept: the rule is a length rule,
    and these are the false positives it is known to carry until a second signal lands."""
    assert mf.is_etf(ticker) is etf


def test_a_symbol_that_does_not_parse_is_dropped_and_counted(env):
    row = _run()
    rows = mf.parse(json.loads(json.dumps(fx.SEC_MF_FILE)))
    assert not [r for r in rows if r["ticker_raw"] in fx.MF_UNPARSED]
    assert f"{len(fx.MF_UNPARSED)} symbols did not parse" in row["note"]


# ── The served table ──────────────────────────────────────────────────────────────────────────────────────────

def test_the_fund_table_holds_every_class_of_the_newest_snapshot_with_its_provenance():
    row = _run()
    assert row["rows"] == 8                                   # 11 rows, 3 of them unparseable
    assert [t for (t,) in _fund()] == ["AAAX", "ACRNX", "ELFNX", "LACAX", "QINSQ", "QQQ", "SOXX", "XLK"]
    (as_of, run_id, fetched_at), = _fund(
        "SELECT source_as_of, run_id, fetched_at FROM fund WHERE ticker = 'XLK'")
    assert as_of == date(2026, 10, 29) and run_id == row["run_id"] and fetched_at > 0


def test_the_etf_universe_is_the_fund_file_plus_the_etp_rows_of_the_equity_file():
    """SPY is a unit investment trust and GLD a commodity trust: neither has a series, so neither is in the fund
    file. A reader after the ETF universe reads ``fund`` and ``security``'s ``etp`` rows, never the fund file alone.
    """
    _run()
    _run(dataset=sec_tickers.NAME)
    with serving.open_read("spine") as con:
        assert con.execute("SELECT count(*) FROM fund WHERE ticker IN ('SPY', 'GLD')").fetchone() == (0,)
        assert con.execute("SELECT security_type FROM security WHERE ticker = 'SPY'").fetchone() == ("etp",)


def test_the_spine_serves_the_fund_table_at_schema_version_two():
    assert serving.SERVING_SCHEMA_VERSION["spine"] == 2
    _run()
    with serving.open_read("spine") as con:
        assert con.execute("SELECT version FROM serving_schema_version").fetchone() == (2,)
