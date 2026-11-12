"""``sec/nport``: the SEC's quarterly Form N-PORT data sets, the ETFs' holdings (#354, ETF PR 3a).

The fetch is ``sec/ncen``'s, narrowed to the newest published quarter (a 404 is "not published yet", a quarter
held is not asked again, one held raw but never typed is typed); a backfill asks the quarters from ``--from``.
What is new is the ETF filter at typing (N-CEN's ETF series, plus a series whose name is an Alpha Vantage ETF's)
and the served ``etf_holdings_nport``.
"""
from __future__ import annotations

import io
from datetime import date

import duckdb
import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.ownership import sec as sec_http
from RevenueChaser.rcdata.build.spine import av_listing, ncen, nport

from . import spine_fixture as fx

ROOT = "https://sec.test/files/dera/data/form-n-port-data-sets/"
AV = ("symbol,name,exchange,assetType,ipoDate,delistingDate,status\r\n"
      "XLK,TECHNOLOGY SELECT SECTOR SPDR FUND ,NYSE ARCA,ETF,1998-12-16,null,Active\r\n"
      "QYLD,GLOBAL X NASDAQ 100 COVERED CALL ETF ,NASDAQ,ETF,2013-12-12,null,Active\r\n"
      "A,Agilent Technologies Inc,NYSE,Stock,1999-11-18,null,Active\r\n")


@pytest.fixture
def env(monkeypatch, http_mock):
    """N-CEN's four quarters and Alpha Vantage's list held, the N-PORT routes of 2026q3 answered."""
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_http, "_shared_pace", lambda: True)
    monkeypatch.setenv("RC_HTTP_CONTACT", "ops@example.org")
    monkeypatch.setenv("RC_ALPHAVANTAGE_BASE", "https://av.test")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "av-test-key")
    monkeypatch.setattr(av_listing, "MIN_ETFS", 1)
    fx.add_ncen_routes(http_mock)
    http_mock.add("state=delisted", "{}")
    http_mock.add("av.test/query", AV)
    base.run_request({"dataset": ncen.NAME, "params": {"mode": "backfill", "to": "2026-11-02"}})
    base.run_request({"dataset": av_listing.NAME, "params": {"as_of": "2026-11-02"}})
    fx.add_nport_routes(http_mock)
    http_mock.requests.clear()
    return http_mock


def _asked(env) -> list[str]:
    return [str(r.url).rsplit("/", 1)[1] for r in env.requests]


def _run(day="2026-11-02", **params) -> dict:
    return base.run_request({"dataset": nport.NAME, "params": {"as_of": day, **params}})


def _read(quarter: str, name: str = "part-0.parquet", select: str = "*", where: str = "",
          order: str = "") -> list[tuple]:
    path = nport.parquet_dir() / f"quarter={quarter}" / name
    sql = (f"SELECT {select} FROM read_parquet('{path}')" + (f" WHERE {where}" if where else "")
           + (f" ORDER BY {order}" if order else ""))
    return duckdb.connect().execute(sql).fetchall()


def test_the_daily_run_fetches_the_newest_quarter_and_types_only_the_etf_series(env):
    row = _run()
    assert row["result"] == "changed", row["note"]
    assert _asked(env) == ["2026q3_nport.zip"]
    assert str(env.requests[0].url) == ROOT + "2026q3_nport.zip"
    assert "ops@example.org" in env.requests[0].headers["user-agent"]
    # N-CEN's ETF series and QYLD's (Alpha Vantage's ETF of the same name); the Acorn fund is no ETF.
    assert _read("2026q3", "fund.parquet", "series_id, av_ticker", order="series_id") == [
        ("S000006408", "XLK"), ("S000024908", None), ("S000042000", "QYLD"), ("S000099001", None),
        ("S000099101", None)]
    aapl = _read("2026q3", select="series_id, report_date, accession, name, cusip, isin, ticker, balance, unit, "
                                  "value_usd, pct, payoff, asset_cat, issuer_type, net_assets",
                 where="series_id = 'S000042000' AND cusip = '037833100'")
    assert aapl == [("S000042000", date(2026, 7, 31), "0000001-26-000105", "Apple Inc", "037833100",
                     "US0378331005", "AAPL", 480_000_000.0, "NS", 4_800_000_000.0, 60.0, "Long", "EC", "CORP",
                     8_000_000_000.0)]


def test_the_newest_filing_of_a_series_and_month_wins(env):
    """XLK's July report and its amendment, filed later: the amendment's lines and net assets are kept, once."""
    _run()
    assert _read("2026q3", "fund.parquet", "accession, net_assets", where="series_id = 'S000006408'") == [
        ("0000001-26-000199", 91_000_000_000.0)]
    assert _read("2026q3", select="pct", where="series_id = 'S000006408' AND cusip = '037833100'") == [(61.0,)]


def test_the_mix_counts_long_equity_and_debt_and_leaves_a_funds_shares_out(env):
    """Equity is EC/EP and debt DBT/LON/SN/ABS-*, long only; a registered fund's shares (CCOI's QQQ, a money
    market fund) count in neither, so a fund of funds is not called equity by the ETFs it holds."""
    _run()
    mix = {s: (e, b) for s, e, b in _read("2026q3", "fund.parquet", "series_id, equity_pct, bond_pct")}
    assert mix == {"S000006408": (98.0, 0.0), "S000099101": (10.0, 85.0), "S000099001": (0.0, 0.0),
                   "S000024908": (0.0, 0.0), "S000042000": (101.0, 0.0)}


def test_a_quarter_not_published_yet_falls_back_to_the_one_before(env):
    env.routes.insert(0, ("2026q3_nport.zip", b"not found", 404, {}))
    fx.add_nport_routes(env, quarters=("2026q2",))
    row = _run()
    assert row["result"] == "changed", row["note"]
    assert _asked(env) == ["2026q3_nport.zip", "2026q2_nport.zip"] and "2026q3 not published yet" in row["note"]
    assert sorted(p.name for p in nport.parquet_dir().iterdir()) == ["quarter=2026q2"]


def test_a_quarter_held_is_not_asked_again(env):
    _run()
    env.requests.clear()
    row = _run("2026-12-01")
    assert not env.requests and row["result"] == "unchanged", row["note"]


def test_a_quarter_held_raw_but_never_typed_is_typed_on_the_next_run(env):
    import shutil
    _run()
    shutil.rmtree(nport.parquet_dir() / "quarter=2026q3")
    env.requests.clear()
    row = _run("2026-11-03")
    assert not env.requests and row["result"] == "changed", row["note"]
    assert nport.typed("2026q3")


def test_without_an_etf_list_the_quarter_is_kept_raw_and_typed_once_the_list_is_held(monkeypatch, http_mock):
    """No N-CEN and no Alpha Vantage list: typing would keep nothing, so the run fails and keeps the zip."""
    import shutil
    monkeypatch.setattr(sec_edgar, "SEC_WWW_BASE", "https://sec.test")
    monkeypatch.setattr(sec_http, "_shared_pace", lambda: True)
    fx.add_nport_routes(http_mock)
    fx.add_ncen_routes(http_mock)
    row = _run()
    assert row["result"] == "failed" and "no ETF list held yet" in row["note"]
    assert nport.raw_path("2026q3").exists() and not nport.typed("2026q3")
    base.run_request({"dataset": ncen.NAME, "params": {"mode": "backfill", "to": "2026-11-02"}})
    http_mock.requests.clear()
    row = _run("2026-11-03")
    assert row["result"] == "changed" and not [r for r in http_mock.requests if "nport" in str(r.url)]
    assert {s for (s,) in _read("2026q3", "fund.parquet", "series_id")} == {
        "S000006408", "S000099101", "S000099001", "S000024908"}          # N-CEN's only: no AV list, no QYLD


def test_the_backfill_asks_every_quarter_from_its_first_day(env):
    fx.add_nport_routes(env, quarters=("2026q2",))
    row = base.run_request({"dataset": nport.NAME, "params": {"mode": "backfill", "from": "2026-01-15",
                                                              "to": "2026-11-02"}})
    assert row["result"] == "changed", row["note"]
    assert _asked(env) == ["2026q3_nport.zip", "2026q2_nport.zip", "2026q1_nport.zip"]
    assert "2026q1 not published yet" in row["note"]
    assert sorted(p.name for p in nport.parquet_dir().iterdir()) == ["quarter=2026q2", "quarter=2026q3"]


def test_a_backfill_needs_a_first_day():
    row = base.run_request({"dataset": nport.NAME, "params": {"mode": "backfill", "to": "2026-11-02"}})
    assert row["result"] == "failed" and "needs a first day" in row["note"]


@pytest.mark.parametrize("body", [b"<html>not a zip</html>", "no_holdings"])
def test_an_answer_that_is_not_the_data_set_fails_and_is_not_kept(env, body):
    if body == "no_holdings":
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("SUBMISSION.tsv", "ACCESSION_NUMBER\tREPORT_DATE\n")
        body = buf.getvalue()
    env.routes.insert(0, ("2026q3_nport.zip", body, 200, {}))
    row = _run()
    assert row["result"] == "failed" and "sec nport 2026q3" in row["note"]
    assert not nport.raw_path("2026q3").exists()


def test_the_build_stages_each_series_newest_month_over_the_held_quarters(env):
    """XLK's July report (2026q3) wins over its April one (2026q2); SOXX filed only in 2026q2 and keeps it."""
    fx.add_nport_routes(env, quarters=("2026q2",))
    row = base.run_request({"dataset": nport.NAME, "params": {"mode": "backfill", "from": "2026-04-01",
                                                              "to": "2026-11-02"}})
    assert "6 ETF series, newest report 2026-07-31" in row["note"]
    assert row["rows"] == 3 + 3 + 3 + 3 + 3 + 2       # each series' newest month's lines


def test_it_is_a_monthly_keyless_spine_dataset_that_starts_by_hand():
    from RevenueChaser.rcdata.build import DATASETS
    ds = DATASETS[nport.NAME]
    assert ds.kind == "spine" and ds.cadence == "monthly" and ds.starts_by_hand and ds.providers == ("sec",)


@pytest.mark.parametrize("equity,bond,cls", [(80.0, 0.0, "equity"), (79.9, 20.0, "mixed"), (10.0, 85.0, "bond"),
                                             (50.0, 50.0, "mixed"), (None, None, "mixed"), (0.0, 80.0, "bond")])
def test_the_asset_class_rule_is_80_percent_of_net_assets(equity, bond, cls):
    assert nport.asset_class(equity, bond) == cls


# ── Served: ``etf_holdings_nport`` and what ``etf`` takes from N-PORT ────────────────────────────────────────

SEC_FILE = {"fields": ["cik", "name", "ticker", "exchange"],
            "data": [[789019, "MICROSOFT CORP", "MSFT", "Nasdaq"], [1222333, "SPDR GOLD TRUST", "GLD", "NYSE"]]}


@pytest.fixture
def served(env):
    """2026q3 and 2026q2 of N-PORT held, then the spine built (the SEC file's run builds the kind)."""
    fx.add_nport_routes(env, quarters=("2026q2",))
    env.add("company_tickers_exchange.json", SEC_FILE)
    base.run_request({"dataset": nport.NAME, "params": {"mode": "backfill", "from": "2026-04-01",
                                                        "to": "2026-11-02"}})
    from RevenueChaser.rcdata.build.spine import sec_tickers
    return base.run_request({"dataset": sec_tickers.NAME, "params": {"as_of": "2026-11-02"}})


def _spine(sql: str) -> list[tuple]:
    from RevenueChaser.rcdata import serving
    with serving.open_read("spine") as con:
        return con.execute(sql).fetchall()


def test_the_spine_serves_each_etfs_newest_month_of_holdings_under_its_ticker(served):
    assert served["result"] != "failed", served["note"]
    rows = _spine("SELECT etf, series_id, report_date, count(*), sum(weight) FROM etf_holdings_nport "
                  "GROUP BY ALL ORDER BY etf")
    assert rows == [("CCOI", "S000099001", date(2026, 7, 31), 3, 97.0),
                    ("QYLD", "S000042000", date(2026, 7, 31), 3, 100.0),       # linked by its AV name
                    ("SOXL", "S000024908", date(2026, 7, 31), 3, 100.0),
                    ("SOXX", "S000004348", date(2026, 5, 31), 2, 17.0),        # its newest is 2026q2's May
                    ("TAPR", "S000099101", date(2026, 6, 30), 3, 100.0),
                    ("XLK", "S000006408", date(2026, 7, 31), 3, 101.0)]        # July's amendment, not April
    aapl = _spine("SELECT name, cusip, isin, ticker_filed, balance, unit, value_usd, weight, payoff, asset_cat, "
                  "issuer_type, net_assets, accession, filing_date FROM etf_holdings_nport "
                  "WHERE etf = 'XLK' AND cusip = '037833100'")
    assert aapl == [("Apple Inc", "037833100", "US0378331005", "AAPL", 5_551_000_000.0, "NS", 55_510_000_000.0,
                     61.0, "Long", "EC", "CORP", 91_000_000_000.0, "0000001-26-000199", date(2026, 9, 15))]


def test_etf_takes_its_asset_class_from_the_nport_mix_and_keeps_the_commodity_rule(served):
    classes = dict(_spine("SELECT ticker, asset_class FROM etf"))
    assert {t: classes[t] for t in ("XLK", "QYLD", "TAPR", "CCOI", "SOXL", "SOXX", "GLD", "LEVX")} == {
        "XLK": "equity", "QYLD": "equity", "TAPR": "bond", "CCOI": "mixed", "SOXL": "mixed",
        "SOXX": "mixed", "GLD": "commodity", "LEVX": None}       # SOXX: NVDA + AVGO are 17%; LEVX files no N-PORT


def test_etf_aum_is_nports_net_assets_when_newer_than_ncens_average(served):
    rows = {t: (a, d, s) for t, a, d, s in _spine("SELECT ticker, aum, aum_as_of, aum_source FROM etf")}
    assert rows["XLK"] == (91_000_000_000.0, date(2026, 7, 31), "nport")       # N-CEN's year ended 2026-04-30
    assert rows["QYLD"] == (8_000_000_000.0, date(2026, 7, 31), "nport")       # no N-CEN at all
    assert rows["LEVX"] == (20_000_000.0, date(2025, 11, 30), "ncen")          # no N-PORT
    assert rows["GLD"] == (None, None, None)


def test_etf_takes_the_series_id_its_name_linked(served):
    assert _spine("SELECT series_id, source FROM etf WHERE ticker = 'QYLD'") == [("S000042000", "av")]


def test_the_table_is_windowed_on_its_newest_month_and_the_schema_is_version_five(served):
    """Each series' newest month is served, so a quarter whose funds hold fewer lines is no shrink; the swap's guard
    compares the newest month's rows (``serving.declare_window``)."""
    from RevenueChaser.rcdata import serving
    from RevenueChaser.rcdata.build.spine import security
    assert serving.SERVING_SCHEMA_VERSION["spine"] == 5 and nport.TABLE in security.TABLES
    assert _spine("SELECT version FROM serving_schema_version") == [(5,)]
    assert _spine(f"SELECT column_name, first_value FROM {serving.WINDOW_TABLE} "
                  f"WHERE table_name = '{nport.TABLE}'") == [("report_date", "2026-07-31")]


def test_a_holdings_cusip_maps_to_the_spines_ticker():
    """``security.cusip`` is Compustat's (``wrds/sec_idhist``), which this file's fixture does not route: the staging
    and the two tables are made directly. One CUSIP shared by two tickers maps to the first, never to both."""
    from RevenueChaser.rcdata.build.spine import stage
    con = duckdb.connect()
    stage.create(con, nport.STAGING, nport.HOLDING)
    con.execute(f"INSERT INTO {nport.STAGING} (series_id, report_date, cusip, ticker, pct) VALUES "
                "('S1', DATE '2026-07-31', '037833100', 'AAPL', 60), ('S1', DATE '2026-07-31', '30303M102', NULL, 30),"
                " ('S1', DATE '2026-07-31', NULL, NULL, 10)")
    con.execute("CREATE TABLE etf (ticker TEXT, series_id TEXT)")
    con.execute("INSERT INTO etf VALUES ('XLK', 'S1'), ('XLK2', 'S1'), ('NONE', NULL)")
    con.execute("CREATE TABLE security (ticker TEXT, cusip TEXT)")
    con.execute("INSERT INTO security VALUES ('AAPL', '037833100'), ('META', '30303M102'), ('FB', '30303M102')")
    assert nport.serve(con) == 6
    assert con.execute(f"SELECT etf, cusip, ticker FROM {nport.TABLE} ORDER BY etf, weight DESC").fetchall() == [
        ("XLK", "037833100", "AAPL"), ("XLK", "30303M102", "FB"), ("XLK", None, None),
        ("XLK2", "037833100", "AAPL"), ("XLK2", "30303M102", "FB"), ("XLK2", None, None)]
