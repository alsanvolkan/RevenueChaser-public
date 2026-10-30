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
    assert f"{len(fx.MF_UNPARSED)} the symbol does not parse" in row["note"]


def test_each_reason_for_dropping_a_row_is_counted_on_its_own(env):
    """A good symbol on a row with no CIK used to be reported as a symbol that did not parse, which sends whoever
    reads the note after the wrong half of the file."""
    _, dropped = mf._parse(json.loads(json.dumps(fx.SEC_MF_FILE)))
    assert {r: sorted(s) for r, s in dropped.items()} == {
        "the symbol does not parse": ["", "(NWAGX)", "n/a"], "no CIK": ["NOCIK"], "no class id": ["NOCLASS"]}
    note = mf.drop_note(dropped)
    assert "1 no CIK (e.g. 'NOCIK')" in note and "1 no class id (e.g. 'NOCLASS')" in note
    assert mf.drop_note({r: [] for r in mf.DROP_REASONS}) is None


def test_a_blank_class_id_is_a_drop_and_not_a_key_that_swallows_every_other_row():
    """Keying on a blank class id collapses every such row into one and says nothing about it: three rows in, one
    row out, nothing counted. The guard is what makes that loud."""
    body = {"fields": list(mf.FIELDS),
            "data": [[1, "S1", None, "ABCD"], [2, "S2", "", "EFGH"], [3, "S3", None, "IJKL"],
                     [4, "S4", "C4", "XLK"]]}
    rows, dropped = mf._parse(body)
    assert [r["ticker"] for r in rows] == ["XLK"]
    assert sorted(dropped["no class id"]) == ["ABCD", "EFGH", "IJKL"]


def test_a_repeated_class_id_keeps_its_first_row_and_two_classes_of_a_series_are_both_rows():
    rows = {r["ticker"]: r for r in mf.parse(json.loads(json.dumps(fx.SEC_MF_FILE)))}
    assert "DUPCLS" in rows and "DUPCLX" not in rows             # one class id, the first symbol
    assert rows["TWINA"]["series_id"] == rows["TWINB"]["series_id"] == "S000009194"


@pytest.mark.parametrize("symbol,ticker", [("AT", "AT"), ("PCOMRSI", "PCOMRSI"), ("BRK.B", "BRK-B"),
                                           ("WFC$C", "WFC-PC"), ("9", None), ("A-B-C-D", None),
                                           ("ABCDEFGH", None), ("n/a", None)])
def test_the_ticker_shape_is_the_one_the_real_file_holds(symbol, ticker):
    """Measured over the real file: a letter first, two to seven characters, and no dot or hyphen in any symbol
    today — but a share class could gain one the way ``BRK.B`` does, so the equity file's suffix is allowed.
    ``ticker`` None is a symbol the shape rejects."""
    rows, dropped = mf._parse({"fields": list(mf.FIELDS), "data": [[1, "S1", "C1", symbol], [2, "S2", "C2", "XLK"]]})
    kept = {r["ticker"] for r in rows} - {"XLK"}
    assert kept == ({ticker} if ticker else set())
    assert dropped[mf.DROP_REASONS[0]] == ([] if ticker else [symbol])


# ── The served table ──────────────────────────────────────────────────────────────────────────────────────────

def test_the_fund_table_holds_every_class_of_the_newest_snapshot_with_its_provenance():
    row = _run()
    assert row["rows"] == len(fx.MF_TICKERS)                  # 17 file rows, 5 dropped and one repeated class id
    assert [t for (t,) in _fund()] == list(fx.MF_TICKERS)
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


def test_the_served_columns_are_the_parquets_without_last_modified():
    """``FUND`` is derived from ``SCHEMA``, so a column added to one silently joins the other. This is the pin."""
    assert [c for c, _ in mf.FUND] == [c for c, _ in mf.SCHEMA if c != "last_modified"]
    _run()
    with serving.open_read("spine") as con:
        served = [d[0] for d in con.execute(f"SELECT * FROM {mf.TABLE} LIMIT 0").description]
    assert served == [c for c, _ in mf.FUND] and "last_modified" not in served


def test_the_spine_builds_with_no_fund_file_yet_and_serves_the_table_empty(env):
    """The likely first state after this deploy: ``sec/company_tickers_mf`` starts by hand, so a daily spine build
    gets there first. ``fund`` is served at 0 rows, the file goes to version 2, and a never-run fund file is one
    ``source_never_run`` issue — not a silently absent table."""
    row = _run(dataset=sec_tickers.NAME)
    assert row["result"] == "changed"
    with serving.open_read("spine") as con:
        assert con.execute(f"SELECT count(*) FROM {mf.TABLE}").fetchone() == (0,)
        assert con.execute("SELECT version FROM serving_schema_version").fetchone() == (2,)
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.spine import security
    issues = {(i["kind"], i["key"]) for i in ops.open_issues(security.NAME, limit=500)[1]}
    assert ("source_never_run", mf.NAME) in issues


def test_a_missing_parquet_tree_refuses_the_swap_rather_than_serving_an_empty_fund(env):
    """``fund`` live and full, this dataset's Parquet gone: the build makes it empty and the swap is refused, so the
    live file keeps its rows. The hatch is narrow on purpose — only a request on this dataset relaxes ``fund`` —
    which is why the refusal is asserted here and the asymmetry is written down in the module docstring."""
    import shutil
    _run()
    before = _fund("SELECT count(*) FROM fund")[0][0]
    assert before == len(fx.MF_TICKERS)
    shutil.rmtree(mf.parquet_dir())
    row = _run("2026-11-12", dataset=sec_tickers.NAME)
    assert row["result"] == "failed" and "fund empties" in row["note"]
    assert _fund("SELECT count(*) FROM fund")[0][0] == before      # the live file was not touched

    # The way out: fetch this dataset again, which rewrites the partition the build reads.
    assert _run("2026-11-12")["result"] == "unchanged"
    assert _fund("SELECT count(*) FROM fund")[0][0] == before
