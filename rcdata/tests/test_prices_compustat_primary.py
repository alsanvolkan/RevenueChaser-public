"""Compustat as the one primary price source (master plan of 2 Nov 2026, items 1-3 and 8; avo, 3 Nov 2026: "Biz şuan
compustat ile tüm geçmişi doldurmak ve geleceği de böyle devam ettirmek istiyoruz").

The fixture is ``test_prices_wrds``'s (AAPL ``001690:01``, a 2-for-1 split ex 4 Jun 2025 that ``ajexdi`` carries,
a 1.00 dividend ex 5 Jun 2025, CRSP's 3 Jun close 202.1 against Compustat's 202.0); a fake WRDS worker answers.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.rcdata.build.prices import wrds_history

from .test_prices_wrds import (AAPL, JUNE, FakeWrds, _backfill, _build, _data, _q, built, env, fake)  # noqa: F401
from RevenueChaser.rcdata.build.spine import wrds


def _serve(monkeypatch, data):
    conn = FakeWrds(data)
    monkeypatch.setattr(wrds, "_factory", lambda: conn)
    wrds.reset()
    _build()
    return conn


# ── Step 1: Compustat first, the whole priced history, the Arca listings ─────────────────────────────────────

def test_compustat_history_starts_on_its_earliest_priced_day_and_asks_the_arca_exchanges(monkeypatch):
    monkeypatch.undo()                                       # the real START_YEAR, not the fixture's 2024
    as_of = date(2026, 10, 27)
    assert wrds_history.COMPUSTAT_START == date(1983, 12, 30)
    assert wrds_history.years("secd", as_of)[0] == 1983 and wrds_history.years("sec_divid", as_of)[0] == 1983
    assert wrds_history.COMPUSTAT_EXCHG == (11, 12, 14, 17, 21)
    sql = wrds_history.year_sql("secd", 1983)
    assert "exchg IN (11, 12, 14, 17, 21)" in sql and "BETWEEN '1983-01-01' AND '1983-12-31'" in sql


def test_a_compustat_bar_wins_its_session_over_crsps(built):
    june = _q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = ? "
              "AND session_date < '2026-01-01' ORDER BY session_date", [AAPL])
    assert [str(r["session_date"]) for r in june] == JUNE
    assert {r["source"] for r in june} == {wrds_history.COMPUSTAT}
    assert june[1]["close"] == 202.0                          # Compustat's, not CRSP's 202.1


def test_an_ajexdi_change_is_a_split_where_compustats_split_table_has_none(monkeypatch):
    data = _data()
    data["comp.sec_split"] = []                               # only ajexdi (2 -> 1 on 4 Jun) says AAPL split
    _serve(monkeypatch, data)
    splits = _q("SELECT ex_date, ratio, applied, source_id FROM corporate_action WHERE security_id = ? "
                "AND action = 'split'", [AAPL])
    assert [(str(s["ex_date"]), s["ratio"], s["applied"]) for s in splits] == [("2025-06-04", 2.0, True)]
    assert splits[0]["source_id"].startswith("wrds:ajexdi:")
    view = {str(r["session_date"]): r["close"] for r in _q(
        "SELECT session_date, close FROM price_bars_daily WHERE ticker = 'AAPL'")}
    assert view["2025-06-03"] == pytest.approx(202.0 / 2)    # split-adjusted by the ajexdi factor


def test_an_ajexdi_split_is_not_added_where_compustats_split_table_has_it(built):
    splits = _q("SELECT source_id FROM corporate_action WHERE security_id = ? AND action = 'split'", [AAPL])
    assert [s["source_id"] for s in splits] == ["wrds:split:001690:01:2025-06-04"]


def _secd_con(rows):
    con = duckdb.connect()
    con.execute("CREATE TABLE stg_wrds_secd (gvkey TEXT, iid TEXT, datadate DATE, curcdd TEXT, prccd DOUBLE, "
                "ajexdi DOUBLE, trfd DOUBLE, source_as_of DATE, fetched_at DOUBLE, run_id TEXT)")
    con.executemany("INSERT INTO stg_wrds_secd VALUES ('001690', '01', ?, 'USD', ?, ?, 1.0, NULL, NULL, ?)", rows)
    con.execute("CREATE TABLE stg_wrds_split (gvkey TEXT, iid TEXT, datadate DATE, split DOUBLE)")
    return con


def test_an_ajexdi_or_return_step_between_two_answers_is_neither_a_split_nor_a_return():
    """A year partition keeps the ``ajexdi`` basis of the day it was asked: the monthly refresh re-asks only the
    newest years, so a split after an older year was asked shows as an ``ajexdi`` step at the partition boundary.
    Only two rows of one answer (one ``run_id``) are compared."""
    con = _secd_con([("2024-12-31", 100.0, 2.0, "old"), ("2025-01-02", 101.0, 1.0, "new"),
                     ("2025-01-03", 51.0, 0.5, "new")])
    got = con.execute(f"SELECT ex_date, new_rate FROM ({wrds_history.AJEXDI_SPLITS})").fetchall()
    assert got == [(date(2025, 1, 3), 2.0)]                  # the in-answer step only
    ret = [r[0] for r in con.execute(f"SELECT {wrds_history.COMP_RETURN} FROM stg_wrds_secd "
                                     "WINDOW w AS (PARTITION BY gvkey, iid ORDER BY datadate) "
                                     "ORDER BY datadate").fetchall()]
    assert ret[1] is None and ret[2] == pytest.approx(51.0 / 0.5 / (101.0 / 1.0) - 1)


# ── Step 2: CRSP only as a narrow fallback ─────────────────────────────────────────────────────────────────────

def _crsp(permno, day, close, cusip, ticker):
    return {"permno": permno, "dlycaldt": day, "dlyprc": close, "dlyopen": close, "dlyhigh": close, "dlylow": close,
            "dlyvol": 1e4, "dlyret": 0.0, "shrout": 1e3, "hdrcusip": cusip, "cusip": cusip, "ticker": ticker,
            "dlycumfacpr": 1.0, "dlyorddivamt": 0.0}


def test_a_crsp_bar_of_a_day_compustat_does_not_price_is_not_served(monkeypatch):
    data = _data()
    data["crsp.dsf_v2"] += [_crsp("84398", "2025-06-06", 590.0, "78462F10", "SPY")]     # linked, CRSP only
    for r in data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] == JUNE[2]:
            r["cshoc"] = None                                 # a Compustat day without shares, trfd present
    _serve(monkeypatch, data)
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE source = ?", [wrds_history.CRSP])[0]["n"] == 0
    assert not _q("SELECT 1 FROM market_cap_daily WHERE shares_source = ?", [wrds_history.CRSP]), \
        "no fallback day, so CRSP's shares size no cap either"


def test_a_day_compustat_prices_without_trfd_is_crsps_bar_when_the_cusip_links_it(monkeypatch):
    data = _data()
    for r in data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] == JUNE[1]:
            r["trfd"] = None                                  # CMG-type: Compustat has the price, no trfd
    _serve(monkeypatch, data)
    june = _q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = ? "
              "AND session_date < '2026-01-01' ORDER BY session_date", [AAPL])
    assert [(str(r["session_date"]), r["source"]) for r in june] == [
        (JUNE[0], wrds_history.COMPUSTAT), (JUNE[1], wrds_history.CRSP), (JUNE[2], wrds_history.COMPUSTAT),
        (JUNE[3], wrds_history.COMPUSTAT), (JUNE[4], wrds_history.COMPUSTAT)]
    assert june[1]["close"] == 202.1


def test_a_ticker_linked_permno_is_no_fallback_for_an_empty_trfd_day(monkeypatch):
    data = _data()
    # GAPX: the spine holds no CUSIP for it, so its PERMNO links by ticker; Compustat holds it under 077777:01.
    data["comp.secd"] += [_secd_row("077777", "2025-06-06", "GAPX", 50.0, "454545459", trfd=None)]
    data["crsp.dsf_v2"] += [_crsp("66666", "2025-06-06", 51.0, "45454545", "GAPX")]
    _serve(monkeypatch, data)
    got = _q("SELECT close, source FROM price_bar_daily WHERE session_date = '2025-06-06' AND close IN (50.0, 51.0)")
    assert got == [{"close": 50.0, "source": wrds_history.COMPUSTAT}]


def test_a_delisted_securitys_final_crsp_days_after_compustats_last_are_served(monkeypatch):
    """OLDCO (Compustat to 4 Mar 2024, linked by CUSIP) trades on CRSP two more days and is gone before CRSP's newest
    day: those two are served from CRSP, a day past ``DELISTED_WINDOW_DAYS`` is not. AAPL's PERMNO is still listed on CRSP's newest day, so a day Compustat lacks
    is not a delisted fallback."""
    data = _data()
    data["crsp.dsf_v2"] += [_crsp("50000", d, c, "68000010", "OLDA")
                            for d, c in (("2024-03-01", 5.0), ("2024-03-04", 5.1), ("2024-03-05", 4.0),
                                         ("2024-03-06", 3.0), ("2024-05-01", 2.0))]   # the last: past the window
    # AAPL's Compustat ends on 5 Jun 2025 here, its PERMNO still trades on CRSP's newest day (6 Jun).
    data["comp.secd"] = [r for r in data["comp.secd"]
                         if not (r["gvkey"] == "001690" and (r["datadate"] == JUNE[4] or r["datadate"] > "2026"))]
    _serve(monkeypatch, data)
    old = _q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = '099999:01' "
             "ORDER BY session_date")
    assert [(str(r["session_date"]), r["source"]) for r in old] == [
        ("2024-03-01", wrds_history.COMPUSTAT), ("2024-03-04", wrds_history.COMPUSTAT),
        ("2024-03-05", wrds_history.CRSP), ("2024-03-06", wrds_history.CRSP)]
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = ? AND session_date = ?", [AAPL, JUNE[4]])


def test_the_delisted_rule_is_one_function_with_its_pending_measurement_named():
    import inspect
    src = inspect.getsource(wrds_history.delisted_final_days)
    assert "TODO" in src and "measurement" in src
    assert "stg_wrds_dsf" in wrds_history.delisted_final_days()


def _secd_row(gvkey, day, tic, close, cusip, trfd=1.0):
    from .test_prices_wrds import _secd
    return _secd(gvkey, "01", day, tic, close, trfd=trfd, cusip=cusip)


# ── Step 3: Alpaca is the tester, never a served bar ───────────────────────────────────────────────────────────

def test_no_alpaca_bar_is_served_and_compustat_serves_the_newest_sessions(built):
    assert {r["source"] for r in _q("SELECT DISTINCT source FROM price_bar_daily")} <= {
        wrds_history.COMPUSTAT, wrds_history.CRSP, "market_series/cboe"}
    october = _q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = ? "
                 "AND session_date >= '2026-10-01' ORDER BY session_date", [AAPL])
    assert [(str(r["session_date"]), r["close"]) for r in october] == [
        ("2026-10-21", 199.5), ("2026-10-22", 200.0), ("2026-10-23", 199.9), ("2026-10-26", 201.0),
        ("2026-10-27", 202.5)]
    assert {r["source"] for r in october} == {wrds_history.COMPUSTAT}


def test_the_daily_alpaca_run_asks_only_the_names_at_risk(fake, http_mock):
    from urllib.parse import parse_qs, urlparse

    from RevenueChaser.rcdata.build import base
    from RevenueChaser.rcdata.build.prices import actions, bars
    assert _backfill()["result"] == "changed"
    assert base.run_request({"dataset": actions.NAME, "params": {"as_of": "2026-10-27"}})["result"] != "failed"
    row = base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})
    asked = {s for r in http_mock.calls("/v2/stocks/bars")
             for s in parse_qs(urlparse(str(r.url)).query)["symbols"][0].split(",")}
    assert asked == {"AAPL"}, "AAPL alone: its Compustat dividend went ex on 23 Oct"
    assert "Alpaca tester: 1 of 5 names at risk (1 dividend)" in row["note"]


def test_the_tester_flags_a_history_close_alpaca_disagrees_with(built):
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.prices import serving as prices_serving
    issues = {i["key"]: i["detail"] for i in ops.open_issues(prices_serving.NAME, limit=500)[1]
              if i["kind"] == "alpaca_close_differs"}
    assert issues == {f"{AAPL}:2026-10-27:{wrds_history.COMPUSTAT}": "compustat 202.5000, alpaca 202.0000"}


def test_the_risk_set_names_each_reason(monkeypatch):
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.prices import serving as prices_serving, stage, tester
    from .test_prices_wrds import _secd

    def rows(gvkey, closes, **kw):
        days = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]
        return [{**_secd(gvkey, "01", d, gvkey, c, **{k: (v[i] if isinstance(v, list) else v) for k, v in kw.items()}),
                 "datadate": date.fromisoformat(d), "source_as_of": date(2026, 10, 27), "fetched_at": 1.0,
                 "run_id": "r"} for i, (d, c) in enumerate(zip(days, closes))]
    secd = (rows("000001", [10] * 5) + rows("000002", [10] * 5, trfd=[1, 1, None, 1, 1])
            + rows("000003", [20, 20, 10, 10, 10], ajexdi=[2, 2, 1, 1, 1]) + rows("000004", [10] * 5, exchg=[14, 14, 11, 11, 11])
            + rows("000005", [4.0] * 5))
    stage.write_rows(secd, wrds_history.SECD + wrds_history.PROVENANCE, wrds_history.partition_path("secd", 2026))
    ops.sync_issues(prices_serving.NAME, prices_serving.ISSUE_KINDS,
                    [("price_gap_no_action", "000006:01:2026-10-22", "x3.000, no split")])
    assert tester.at_risk(date(2026, 10, 27)) == {
        "000002:01": ["trfd_empty"], "000003:01": ["split"], "000004:01": ["exchange_move"],
        "000005:01": ["low_price"], "000006:01": ["price_gap"]}

