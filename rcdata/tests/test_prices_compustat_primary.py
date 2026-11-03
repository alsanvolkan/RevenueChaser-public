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
                "ajexdi DOUBLE, trfd DOUBLE, div DOUBLE, divd DOUBLE, divsp DOUBLE, source_as_of DATE, "
                "fetched_at DOUBLE, run_id TEXT)")
    con.executemany("INSERT INTO stg_wrds_secd VALUES ('001690', '01', ?, 'USD', ?, ?, 1.0, NULL, NULL, NULL, NULL, "
                    "NULL, ?)", rows)
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
    ret = [r[0] for r in con.execute(f"SELECT {wrds_history.COMP_RETURN} FROM (SELECT *, {wrds_history.SECD_DIV} "
                                     "AS div_day FROM stg_wrds_secd) "
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


def test_a_day_compustat_prices_without_trfd_is_compustats_bar_with_a_flagged_price_only_return(monkeypatch):
    """Coordinator's homesrv measurement (3 Nov 2026, 0 WRDS queries): an empty ``trfd`` almost always means no
    distribution that day; the price-only return matches CRSP's ``dlyret`` within 0.01 % on 97.69 % of 4,069,937
    matched days, as the ``trfd`` formula does on its rows. So the day stays Compustat's, CUSIP link or not."""
    data = _data()
    for r in data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] == JUNE[1]:
            r["trfd"] = None                                  # CMG-type: Compustat has the price, no trfd
    _serve(monkeypatch, data)
    june = _q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = ? "
              "AND session_date < '2026-01-01' ORDER BY session_date", [AAPL])
    assert {r["source"] for r in june} == {wrds_history.COMPUSTAT} and june[1]["close"] == 202.0


def _div_con(rows):
    """``stg_wrds_secd`` rows ``(day, prccd, ajexdi, trfd, divd, divsp)`` of one security, one answer."""
    con = duckdb.connect()
    con.execute("CREATE TABLE stg_wrds_secd (gvkey TEXT, iid TEXT, datadate DATE, prccd DOUBLE, ajexdi DOUBLE, "
                "trfd DOUBLE, div DOUBLE, divd DOUBLE, divsp DOUBLE, run_id TEXT)")
    con.executemany("INSERT INTO stg_wrds_secd VALUES ('001690', '01', ?, ?, ?, ?, NULL, ?, ?, 'r')", rows)
    return con.execute(f"SELECT {wrds_history.COMP_RETURN}, {wrds_history.COMP_RETURN_BASIS} FROM (SELECT *, "
                       f"{wrds_history.SECD_DIV} AS div_day FROM stg_wrds_secd) "
                       "WINDOW w AS (PARTITION BY gvkey, iid ORDER BY datadate) ORDER BY datadate").fetchall()


def test_the_return_rule_trfd_else_price_plus_the_days_cash_distribution():
    got = _div_con([("2025-06-02", 100.0, 1.0, 1.0, None, None), ("2025-06-03", 101.0, 1.0, 1.01, None, None),
                    ("2025-06-04", 100.0, 1.0, None, None, None), ("2025-06-05", 99.0, 1.0, None, 0.5, None),
                    ("2025-06-06", 49.0, 0.5, None, None, 0.25)])
    assert got[0] == (None, None)                            # no return, no basis
    assert got[1][0] == pytest.approx(101.0 * 1.01 / 100.0 - 1) and got[1][1] == "trfd"
    assert got[2][0] == pytest.approx(100.0 / 101.0 - 1) and got[2][1] == "price_only"
    assert got[3][0] == pytest.approx((99.0 + 0.5) / 100.0 - 1) and got[3][1] == "price_dividend"
    assert got[4][0] == pytest.approx((49.0 + 0.25) / 0.5 / 99.0 - 1) and got[4][1] == "price_dividend"


def test_the_secd_pull_names_its_dividend_columns():
    sql = wrds_history.year_sql("secd", 2025)
    for col in ("div", "divd", "divsp", "divrc", "cheqv", "capgn", "curcddv"):
        assert f" {col}," in sql or f" {col} " in sql, col


def test_compustats_secd_dividend_is_the_action_and_sec_divid_only_its_fallback(monkeypatch):
    data = _data()
    for r in data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] == JUNE[3]:
            r["divd"], r["curcddv"] = 1.0, "USD"              # secd carries the 5 Jun dividend; sec_divid does too
    data["comp.sec_divid"] += [{"gvkey": "001690", "iid": "01", "datadate": JUNE[1], "divd": 0.5, "curcddv": "USD"}]
    _serve(monkeypatch, data)
    divs = _q("SELECT ex_date, amount, source_id FROM corporate_action WHERE security_id = ? "
              "AND action = 'cash_dividend' ORDER BY ex_date", [AAPL])
    june = [d for d in divs if str(d["ex_date"]) == JUNE[3]]
    assert [(d["amount"], d["source_id"]) for d in june] == [(1.0, f"wrds:secddiv:001690:01:{JUNE[3]}")]
    assert [d["source_id"] for d in divs if str(d["ex_date"]) == JUNE[1]] == [f"wrds:div:001690:01:{JUNE[1]}"], \
        "sec_divid's dividend stays where secd has none that day"


def test_a_secd_row_after_the_build_date_is_never_served(monkeypatch):
    """Review SHOULD 1-2 of #382: the cut is the build's own date (the fixture's clock: the 27 Oct 2026 session), not
    the wall clock, so a rebuild for a past day serves nothing later and the test does not rot with the calendar."""
    from datetime import timedelta

    from .test_prices_wrds import _secd
    build_day = date(2026, 10, 27)
    data = _data()
    data["comp.secd"] += [_secd("001690", "01", (build_day + timedelta(days=d)).isoformat(), "AAPL", 300.0)
                          for d in (1, 30)]                            # the next day, and a month on: both later
    _serve(monkeypatch, data)
    assert not _q("SELECT 1 FROM price_bar_daily WHERE session_date > ?", [build_day])


def test_a_crsp_bar_never_replaces_compustats_on_an_empty_trfd_day(monkeypatch):
    data = _data()
    # GAPX: the spine holds no CUSIP for it, so its PERMNO links by ticker; Compustat holds it under 077777:01.
    data["comp.secd"] += [_secd_row("077777", "2025-06-06", "GAPX", 50.0, "454545459", trfd=None)]
    data["crsp.dsf_v2"] += [_crsp("66666", "2025-06-06", 51.0, "45454545", "GAPX")]
    _serve(monkeypatch, data)
    got = _q("SELECT close, source FROM price_bar_daily WHERE session_date = '2025-06-06' AND close IN (50.0, 51.0)")
    assert got == [{"close": 50.0, "source": wrds_history.COMPUSTAT}]


DELIST = "crsp_a_stock.stkdelists"


def test_a_delisted_securitys_crsp_days_up_to_its_delisting_day_are_served(monkeypatch):
    """OLDCO (Compustat to 4 Mar 2024, linked by CUSIP) trades on CRSP one more day; CRSP's ``stkdelists`` delists it
    on 6 Mar at 3.0 with a delisting return (coordinator, 3 Nov 2026: CRSP's last day is one trading day after
    Compustat's in 25 of 30 sampled ended PERMNOs, and that last dsf_v2 row has no price). The days up to the
    delisting day are CRSP's; the delisting day's bar is ``stkdelists``' price; a later dsf_v2 row is not served. AAPL
    has no delisting row, so a day Compustat lacks is no fallback."""
    data = _data()
    data["crsp.dsf_v2"] += [_crsp("50000", d, c, "68000010", "OLDA")
                            for d, c in (("2024-03-01", 5.0), ("2024-03-04", 5.1), ("2024-03-05", 4.0),
                                         ("2024-05-01", 2.0))]                       # the last: after its delisting
    data["crsp.dsf_v2"] += [{**_crsp("50000", "2024-03-06", None, "68000010", "OLDA"), "dlyret": -0.25}]
    data[DELIST] = [{"permno": "50000", "delistingdt": "2024-03-06", "deldtprc": 3.0, "delret": -0.25}]
    data["comp.secd"] = [r for r in data["comp.secd"]
                         if not (r["gvkey"] == "001690" and (r["datadate"] == JUNE[4] or r["datadate"] > "2026"))]
    _serve(monkeypatch, data)
    old = _q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = '099999:01' "
             "ORDER BY session_date")
    assert [(str(r["session_date"]), r["close"], r["source"]) for r in old] == [
        ("2024-03-01", 5.0, wrds_history.COMPUSTAT), ("2024-03-04", 5.1, wrds_history.COMPUSTAT),
        ("2024-03-05", 4.0, wrds_history.CRSP), ("2024-03-06", 3.0, wrds_history.CRSP)]
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = ? AND session_date = ?", [AAPL, JUNE[4]])
    report = {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")}
    assert report["crsp_delisting_returns"]["sessions"] == 1          # 3.0 / 4.0 - 1 = delret: no difference
    assert report["crsp_delisting_returns"]["return_differs"] == 0


def test_the_delisted_rule_is_one_function_over_crsps_delisting_table():
    import inspect
    assert "TODO" not in inspect.getsource(wrds_history.delisted_final_days)     # the measurement is in
    assert "_delisted" in wrds_history.delisted_final_days() and "stg_wrds_delist" in wrds_history.DELISTED
    sql = wrds_history.TABLES["stkdelists"]["sql"]
    assert sql == "SELECT permno, delistingdt, deldtprc, delret FROM crsp_a_stock.stkdelists"
    assert "stkdelists" in wrds_history.CRSP_TABLES


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
            + rows("000003", [20, 20, 10, 10, 10], ajexdi=[2, 2, 1, 1, 1]) + rows("000004", [10] * 5)
            + rows("000005", [4.0] * 5))
    stage.write_rows(secd, wrds_history.SECD + wrds_history.PROVENANCE, wrds_history.partition_path("secd", 2026))
    # secd.exchg is today's code: an exchange move is a sec_idhist EXCHG period starting in the window (the spine's
    # wrds/sec_idhist snapshot).
    from RevenueChaser.rcdata.build.spine import wrds as spine_wrds
    stage.write_rows([{"gvkey": "000004", "iid": "01", "item": "EXCHG", "itemvalue": "11",
                       "efffrom": date(2026, 10, 23), "effthru": None, "source_as_of": date(2026, 10, 27),
                       "fetched_at": 1.0, "run_id": "s"},
                      {"gvkey": "000001", "iid": "01", "item": "EXCHG", "itemvalue": "14",
                       "efffrom": date(2001, 1, 2), "effthru": None, "source_as_of": date(2026, 10, 27),
                       "fetched_at": 1.0, "run_id": "s"}],
                     spine_wrds.IDHIST + spine_wrds.PROVENANCE,
                     spine_wrds.parquet_dir("wrds/sec_idhist") / "snapshot=2026-10-27" / "sec_idhist.parquet")
    ops.sync_issues(prices_serving.NAME, prices_serving.ISSUE_KINDS,
                    [("price_gap_no_action", "000006:01:2026-10-22", "x3.000, no split")])
    assert tester.at_risk(date(2026, 10, 27)) == {
        "000002:01": ["trfd_empty"], "000003:01": ["split"], "000004:01": ["exchange_move"],
        "000005:01": ["low_price"], "000006:01": ["price_gap"]}


# ── Step 4 (Order 4): real splits stop raising price_gap_no_action ─────────────────────────────────────────────
# homesrv, 2 Nov 2026: 1,167 open price_gap_no_action issues sit on a split CRSP's dlycumfacpr records and
# corporate_action lacked (22 of them Tier A). The detector (serving.py, GAP_FACTOR) counts a split from any source.

def _gap_issues():
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.prices import serving as prices_serving
    return {i["key"] for i in ops.open_issues(prices_serving.NAME, limit=500)[1] if i["kind"] == "price_gap_no_action"}


def _split_unknown_to_compustat(data):
    """AAPL's 4 Jun 2025 split as comp.secd and comp.sec_split miss it: no split row, ajexdi flat."""
    data["comp.sec_split"] = []
    for r in data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] < "2026":
            r["ajexdi"] = 1.0
    return data


def test_a_split_only_crsps_factor_records_is_a_cross_check_issue_not_a_gap(monkeypatch):
    """The WRDS access inventory (3 Nov 2026): Compustat's daily splits (``sec_split``, ``ajexdi``) are the source and
    CRSP's only the cross-check. A split only ``dlycumfacpr`` records is not applied; the gap it explains is no
    ``price_gap_no_action`` but a ``split_only_crsp`` issue, which names what Compustat lacks."""
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.prices import serving as prices_serving
    _serve(monkeypatch, _split_unknown_to_compustat(_data()))      # CRSP's dlycumfacpr: 2.0 -> 1.0 on 4 Jun
    assert f"{AAPL}:{JUNE[2]}" not in _gap_issues()
    assert not _q("SELECT 1 FROM corporate_action WHERE security_id = ? AND action = 'split'", [AAPL])
    crsp_only = {i["key"]: i["detail"] for i in ops.open_issues(prices_serving.NAME, limit=500)[1]
                 if i["kind"] == "split_only_crsp"}
    assert set(crsp_only) == {f"{AAPL}:{JUNE[2]}"} and "PERMNO 14593" in crsp_only[f"{AAPL}:{JUNE[2]}"]


def test_the_same_gap_with_no_source_of_the_split_is_still_an_issue(monkeypatch):
    data = _split_unknown_to_compustat(_data())
    for r in data["crsp.dsf_v2"]:
        r["dlycumfacpr"] = 1.0
    _serve(monkeypatch, data)
    assert f"{AAPL}:{JUNE[2]}" in _gap_issues()


def test_a_split_compustat_and_crsp_both_carry_is_one_action(built):
    from RevenueChaser.rcdata import ops
    from RevenueChaser.rcdata.build.prices import serving as prices_serving
    splits = _q("SELECT source_id FROM corporate_action WHERE security_id = ? AND action = 'split'", [AAPL])
    assert [s["source_id"] for s in splits] == ["wrds:split:001690:01:2025-06-04"]
    assert f"{AAPL}:{JUNE[2]}" not in _gap_issues()
    assert not [i for i in ops.open_issues(prices_serving.NAME, limit=500)[1] if i["kind"] == "split_only_crsp"]


def test_a_crsp_factor_step_between_two_answers_or_by_a_ticker_link_is_no_split():
    con = duckdb.connect()
    con.execute("CREATE TABLE stg_wrds_dsf (permno TEXT, dlycaldt DATE, dlycumfacpr DOUBLE, source_as_of DATE, "
                "fetched_at DOUBLE, run_id TEXT)")
    con.executemany("INSERT INTO stg_wrds_dsf VALUES (?, ?, ?, NULL, NULL, ?)", [
        ("1", "2024-12-31", 4.0, "old"), ("1", "2025-01-02", 2.0, "new"), ("1", "2025-01-03", 1.0, "new"),
        ("2", "2025-01-02", 2.0, "new"), ("2", "2025-01-03", 1.0, "new")])
    con.execute("CREATE TABLE _permno_link AS SELECT * FROM (VALUES ('1', 'a:01', 'hdrcusip'), "
                "('2', 'b:01', 'ticker')) t(permno, security_id, how)")
    got = con.execute(f"SELECT security_id, ex_date, new_rate FROM ({wrds_history.CRSP_SPLITS})").fetchall()
    assert got == [("a:01", date(2025, 1, 3), 2.0)]


# ── Step 5: the backfill of the new range, and its dry run ─────────────────────────────────────────────────────

def test_no_wrds_price_query_selects_every_column():
    for table, spec in wrds_history.TABLES.items():
        assert "SELECT *" not in spec["sql"].upper(), table
    assert wrds_history.ASKED_COLUMNS["sec_divid"] == [c for c, _ in wrds_history.DIVID]


def _asked_tables(conn):
    import re
    return [f"{re.search(r'FROM (\w+\.\w+)', q).group(1)} {y.group(1) if (y := re.search(r'BETWEEN .(\d{4})', q)) else ''}".strip()
            for q in conn.queries]


def _old_filter(year):
    """Rewrite the newest raw sidecar of a secd year as the old exchange filter asked it."""
    from RevenueChaser.rcdata.build import base
    meta = sorted(wrds_history.raw_dir("secd", year).glob("*.meta.json"))[-1]
    m = base.read_raw_json(meta)
    m["query"] = m["query"].replace("exchg IN (11, 12, 14, 17, 21)", "exchg IN (11, 12, 14)")
    base.write_raw_json(meta, m)


def test_a_secd_year_asked_with_the_old_exchange_filter_is_asked_again(fake):
    conn, _ = fake
    assert _backfill()["result"] == "changed"
    _old_filter(2024)
    conn.queries.clear()
    _backfill()
    assert _asked_tables(conn) == ["comp.secd 2024"]           # the columns were read today already
    assert list(wrds_history.raw_dir("secd", 2024).glob("*.superseded")), "the old answer is kept aside"
    conn.queries.clear()
    _backfill()
    assert conn.queries == []                                  # asked with today's filter: current again


def test_the_dry_run_prints_the_queries_and_expected_rows_and_asks_nothing(fake, capsys, monkeypatch):
    from RevenueChaser.rcdata.build import __main__ as cli
    conn, _ = fake
    assert _backfill()["result"] == "changed"
    monkeypatch.setattr(wrds_history, "START_YEAR", 2008)        # so 2008-2023 are missing
    _old_filter(2024)
    wrds_history.partition_path("sec_divid", 2025).unlink()
    for raw in wrds_history.raw_dir("sec_divid", 2025).glob("*"):
        raw.unlink()
    conn.queries.clear()
    assert cli.main(["backfill", "wrds/prices", "--to", "2026-10-27", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert conn.queries == []
    assert "comp.secd 2024: asked with an older query (exchange filter or columns)" in out
    assert "comp.sec_divid 2025: missing" in out
    assert "wrds/prices dry run: 51 queries" in out   # the columns, 16 missing years of 3 tables, the two partitions
    assert "through 2009-12-31: 6 queries" in out                 # 2008 and 2009, three tables each
    assert "exchg 17 (NYSE Arca) 7,500,000 and 21 (BATS BZX) 1,700,000" in out
    assert "expected rows" in out


def test_a_trfd_day_after_a_day_without_trfd_is_read_without_trfd():
    """trfd on one side only is no ratio of two factors: the day is read as price (plus its distribution)."""
    got = _div_con([("2025-06-02", 100.0, 1.0, None, None, None), ("2025-06-03", 101.0, 1.0, 1.5, None, None)])
    assert got[1][0] == pytest.approx(101.0 / 100.0 - 1) and got[1][1] == "price_only"


def _delisted(monkeypatch, dsf_days, delisting):
    data = _data()
    data["crsp.dsf_v2"] += [_crsp("50000", d, c, "68000010", "OLDA") for d, c in dsf_days]
    data[DELIST] = [{"permno": "50000", "delistingdt": delisting[0], "deldtprc": delisting[1], "delret": -0.1}]
    _serve(monkeypatch, data)
    return (_q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = '099999:01' "
               "ORDER BY session_date"), {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")})


def test_a_delisting_on_compustats_last_day_keeps_compustats_bar_and_checks_delret(monkeypatch):
    bars_, report = _delisted(monkeypatch, [("2024-03-01", 5.0), ("2024-03-04", 5.1)], ("2024-03-04", 4.59))
    assert {r["source"] for r in bars_} == {wrds_history.COMPUSTAT}
    assert report["crsp_delisting_returns"]["sessions"] == 1
    assert report["crsp_delisting_returns"]["return_differs"] == 1, "Compustat's 5.1/5.0 is not delret's -10 %"


def test_a_priced_dsf_row_on_the_delisting_day_is_that_days_bar(monkeypatch):
    bars_, _ = _delisted(monkeypatch, [("2024-03-04", 5.1), ("2024-03-05", 3.5)], ("2024-03-05", 3.0))
    assert [(str(r["session_date"]), r["close"]) for r in bars_][-1] == ("2024-03-05", 3.5)


# ── Step 10: a day is listed by the exchange it had that day (comp.sec_idhist), not by today's code ───────────
# homesrv, 3 Nov 2026 (read-only): secd.exchg is the security's current code (0 of 18,024 securities carry two), so a
# name that moved to OTC lost all its listed years. avo, 3 Nov ~09:50 Amsterdam: "Evet kurali uygula".

def _listed(secd, idhist):
    """``wrds_history.SECD_LISTED`` over ``secd`` ``(gvkey, day, current exchg)`` and EXCHG history ``idhist``
    ``(gvkey, exchg, efffrom, effthru)``: {(gvkey, day): listed}."""
    con = duckdb.connect()
    con.execute("CREATE TABLE stg_wrds_secd (gvkey TEXT, iid TEXT, datadate DATE, exchg INTEGER)")
    con.executemany("INSERT INTO stg_wrds_secd VALUES (?, '01', ?, ?)", secd)
    con.execute("CREATE TABLE stg_wrds_exchg (gvkey TEXT, iid TEXT, exchg INTEGER, efffrom DATE, effthru DATE)")
    con.executemany("INSERT INTO stg_wrds_exchg VALUES (?, '01', ?, ?, ?)", idhist)
    con.execute("ALTER TABLE stg_wrds_secd ADD COLUMN div DOUBLE; ALTER TABLE stg_wrds_secd ADD COLUMN divd DOUBLE; "
                "ALTER TABLE stg_wrds_secd ADD COLUMN divsp DOUBLE")
    con.execute("CREATE TABLE stg_wrds_divid (gvkey TEXT, iid TEXT, datadate DATE, div DOUBLE, divd DOUBLE, "
                "divsp DOUBLE)")
    return {(g, str(d)): bool(x) for g, d, x in con.execute(
        f"SELECT gvkey, datadate, listed FROM ({wrds_history.SECD_LISTED})").fetchall()}


SECD_CASES = [
    # A: on NYSE (11) to 2019-12-31, OTC (19) since; today's code 19
    ("A", "1990-06-01", 19), ("A", "2019-12-31", 19), ("A", "2020-01-01", 19), ("A", "2020-06-01", 19),
    # B: OTC to 2014-12-31, Nasdaq (14) since; today's code 14
    ("B", "1995-06-01", 14), ("B", "2014-12-31", 14), ("B", "2015-01-01", 14),
    # C: no exchange history; today's code 14
    ("C", "1990-06-01", 14), ("C", "2020-06-01", 14),
    # D: on NYSE 2000-2005 in the history, nothing after; today's code 11
    ("D", "2010-06-01", 11),
    # E: Nasdaq to 2009-12-31, NYSE from 2010-01-04 with no end; today's code 19 (secd and sec_idhist disagree)
    ("E", "2010-01-04", 19), ("E", "2012-06-01", 19),
]
IDHIST_CASES = [("A", 11, "1998-01-02", "2019-12-31"), ("A", 19, "2020-01-01", None),
                ("B", 19, "1998-01-02", "2014-12-31"), ("B", 14, "2015-01-01", None),
                ("D", 11, "2000-01-03", "2005-12-30"),
                ("E", 14, "1998-01-02", "2009-12-31"), ("E", 11, "2010-01-04", None)]


def test_a_day_is_listed_by_its_own_exchange_and_before_1998_by_the_first_known_one():
    got = _listed(SECD_CASES, IDHIST_CASES)
    assert got == {
        ("A", "1990-06-01"): True,      # before sec_idhist starts: its first known exchange, NYSE
        ("A", "2019-12-31"): True,      # the last day of its NYSE period
        ("A", "2020-01-01"): False,     # the first day on OTC
        ("A", "2020-06-01"): False,
        ("B", "1995-06-01"): False,     # first known exchange OTC
        ("B", "2014-12-31"): False,
        ("B", "2015-01-01"): True,      # the first day on Nasdaq
        ("C", "1990-06-01"): True, ("C", "2020-06-01"): True,     # no history: today's code
        ("D", "2010-06-01"): True,      # after its history ends: today's code
        ("E", "2010-01-04"): True,      # the first day of its NYSE period, not the day after
        ("E", "2012-06-01"): True,      # an open-ended period holds every day after it starts
    }


def test_the_rule_against_todays_code_on_the_fixture():
    """The measurement asked for (fixture only, not real data): today's-code filter versus the dated rule."""
    got = _listed(SECD_CASES, IDHIST_CASES)
    today = {(g, d): x in wrds_history.COMPUSTAT_EXCHG for g, d, x in SECD_CASES}
    added = sorted(k for k in got if got[k] and not today[k])
    dropped = sorted(k for k in got if today[k] and not got[k])
    assert added == [("A", "1990-06-01"), ("A", "2019-12-31"),                     # 2 securities, 4 rows gained
                     ("E", "2010-01-04"), ("E", "2012-06-01")]
    assert dropped == [("B", "1995-06-01"), ("B", "2014-12-31")]                   # 1 security, 2 rows lost


def test_the_secd_pull_reaches_securities_listed_once_whatever_their_code_today():
    sql = wrds_history.year_sql("secd", 2025)
    assert "exchg IN (11, 12, 14, 17, 21) OR (gvkey, iid) IN (SELECT gvkey, iid FROM comp.sec_idhist" in sql
    assert "item = 'EXCHG'" in sql and "itemvalue IN ('11', '12', '14', '17', '21')" in sql


def test_a_security_on_otc_today_keeps_the_bars_of_its_listed_days(monkeypatch):
    from RevenueChaser.rcdata.build.prices import stage
    from RevenueChaser.rcdata.build.spine import wrds as spine_wrds
    from .test_prices_wrds import _secd
    data = _data()
    data["comp.secd"] += [_secd("055555", "01", d, "WENT", 3.0, exchg=19, cusip="55555X109")
                          for d in ("2024-03-01", "2024-03-04", "2024-03-05")]
    stage.write_rows([{"gvkey": "055555", "iid": "01", "item": "EXCHG", "itemvalue": "11", "efffrom": date(1998, 1, 2),
                       "effthru": date(2024, 3, 4), "source_as_of": date(2026, 10, 27), "fetched_at": 1.0,
                       "run_id": "s"},
                      {"gvkey": "055555", "iid": "01", "item": "EXCHG", "itemvalue": "19", "efffrom": date(2024, 3, 5),
                       "effthru": None, "source_as_of": date(2026, 10, 27), "fetched_at": 1.0, "run_id": "s"}],
                     spine_wrds.IDHIST + spine_wrds.PROVENANCE,
                     spine_wrds.parquet_dir("wrds/sec_idhist") / "snapshot=2026-10-27" / "sec_idhist.parquet")
    # A newer delta of the spine's sec_idhist ends the NYSE period a day earlier: the newest row of a period wins.
    stage.write_rows([{"gvkey": "055555", "iid": "01", "item": "EXCHG", "itemvalue": "11", "efffrom": date(1998, 1, 2),
                       "effthru": date(2024, 3, 3), "source_as_of": date(2026, 10, 28), "fetched_at": 2.0,
                       "run_id": "d"}], spine_wrds.IDHIST + spine_wrds.PROVENANCE,
                     spine_wrds.parquet_dir("wrds/sec_idhist") / "delta=2026-10-28" / "sec_idhist.parquet")
    _serve(monkeypatch, data)
    got = [str(r["session_date"]) for r in _q("SELECT session_date FROM price_bar_daily "
                                              "WHERE security_id = '055555:01' ORDER BY 1")]
    assert got == ["2024-03-01"]               # on NYSE to 3 Mar (the delta's end), OTC from 5 Mar; 4 Mar in a gap


def test_a_stock_dividend_alpaca_knows_and_compustats_ajexdi_step_the_same_day_apply_once(monkeypatch, http_mock):
    """Review SHOULD 3 of #382: an ajexdi step is written as a split, an Alpaca stock dividend that day as a
    stock_dividend; as two kinds both were applied, the day's shares adjusted twice."""
    data = _data()
    for r in data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] == "2026-10-21":
            r["ajexdi"] = 2.0                                  # Compustat's step on the 22nd: a 2-for-1
    http_mock.routes.insert(0, ("/v1/corporate-actions", {"corporate_actions": {
        "cash_dividends": [{"id": "cd-1", "symbol": "AAPL", "rate": 0.26, "special": False, "foreign": False,
                            "process_date": "2026-10-24", "ex_date": "2026-10-23", "currency": "USD"}],
        "stock_dividends": [{"id": "sd-1", "symbol": "AAPL", "rate": 1.0, "process_date": "2026-10-22",
                             "ex_date": "2026-10-22"}]}, "next_page_token": None}, 200, {}))
    _serve(monkeypatch, data)
    applied = _q("SELECT source_id, action FROM corporate_action WHERE security_id = ? AND ex_date = '2026-10-22' "
                 "AND applied", [AAPL])
    assert len(applied) == 1, applied


def _listed_returns(secd, divid):
    """COMP_RETURN and COMP_RETURN_BASIS over ``SECD_LISTED`` (the build's ``_secd_listed``): ``secd`` rows
    ``(day, prccd, trfd)`` of one security and answer, ``divid`` its comp.sec_divid rows ``(day, divd)``."""
    con = duckdb.connect()
    con.execute("CREATE TABLE stg_wrds_secd (gvkey TEXT, iid TEXT, datadate DATE, prccd DOUBLE, ajexdi DOUBLE, "
                "trfd DOUBLE, div DOUBLE, divd DOUBLE, divsp DOUBLE, exchg INTEGER, run_id TEXT)")
    con.executemany("INSERT INTO stg_wrds_secd VALUES ('001690', '01', ?, ?, 1.0, ?, NULL, NULL, NULL, 14, 'r')", secd)
    con.execute("CREATE TABLE stg_wrds_exchg (gvkey TEXT, iid TEXT, exchg INTEGER, efffrom DATE, effthru DATE)")
    con.execute("CREATE TABLE stg_wrds_divid (gvkey TEXT, iid TEXT, datadate DATE, div DOUBLE, divd DOUBLE, "
                "divsp DOUBLE)")
    con.executemany("INSERT INTO stg_wrds_divid VALUES ('001690', '01', ?, NULL, ?, NULL)", divid)
    return con.execute(f"SELECT {wrds_history.COMP_RETURN}, {wrds_history.COMP_RETURN_BASIS} "
                       f"FROM ({wrds_history.SECD_LISTED}) WINDOW w AS (PARTITION BY gvkey, iid ORDER BY datadate) "
                       "ORDER BY datadate").fetchall()


def test_a_dividend_only_sec_divid_holds_flags_its_day_and_is_in_its_return():
    """Review SHOULD 4 of #382: the flag and the return read the secd row's dividend only; sec_divid's, its fallback,
    left a dividend day without trfd counted as price_only. The first row, with no return, has no basis either."""
    got = _listed_returns([("2025-06-02", 100.0, None), ("2025-06-03", 99.0, None)], [("2025-06-03", 0.5)])
    assert got[0] == (None, None)
    assert got[1][0] == pytest.approx((99.0 + 0.5) / 100.0 - 1) and got[1][1] == "price_dividend"
