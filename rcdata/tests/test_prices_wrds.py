"""``wrds/prices``: the history backfill against a fake WRDS worker (never WRDS): one query per table and year from
1984 (CRSP to 2025), an existing partition skipped, the monthly refresh keeping what it replaces, the history bars
and actions in the served tables, Compustat's ``ajexdi``/``trfd`` return within 1 bp, and the difference report.

The fixture: AAPL (``001690:01``, CRSP permno 14593) does a 2-for-1 split ex 4 Jun 2025 and goes ex a 1.00
dividend on the 5th (Compustat's ``trfd`` reinvests it at that close); CRSP's close on the 3rd is 202.1 against
Compustat's 202.0; Compustat's 27 Oct 2026 close is 202.5 against Alpaca's 202.0; OLDCO is a delisted Compustat
name the spine does not hold; a CAD row is never served. There is no CCM link (homesrv's subscription refuses
``crsp_a_ccm``): CRSP's rows reach a security by CUSIP or ticker (``wrds_history.PERMNO_LINK``). The CRSP column names
were read from WRDS's ``information_schema`` on homesrv (1 Nov 2026)."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

import duckdb
import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import caps, ops, read, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.prices import actions, bars, cover_shares, floats, wrds_history
from RevenueChaser.rcdata.build.prices import serving as prices_serving
from RevenueChaser.rcdata.build.spine import wrds

from .prices_fixture import SPINE, BarsAnswer, bar, write_spine
from .spine_fixture import write_jsonl

AAPL, OLDCO = "001690:01", "099999:01"
JUNE = ["2025-06-02", "2025-06-03", "2025-06-04", "2025-06-05", "2025-06-06"]
COMP_CLOSE = [200.0, 202.0, 101.5, 100.0, 100.5]
AJEXDI = [2.0, 2.0, 1.0, 1.0, 1.0]
TRFD = [1.0, 1.0, 1.0, 1.01, 1.01]
CRSP_CLOSE = [200.0, 202.1, 101.5, 100.0, 100.5]
OCT = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)]
ALPACA_AAPL = [199.5, 200.0, 199.9, 201.0, 202.0]


def _secd(gvkey, iid, day, tic, close, ajexdi=1.0, trfd=1.0, cshoc=15e9, curcdd="USD", exchg=14, cusip="037833100"):
    return {"gvkey": gvkey, "iid": iid, "datadate": day, "tic": tic, "cusip": cusip, "curcdd": curcdd,
            "prccd": close, "prcod": close, "prchd": close * 1.01, "prcld": close * 0.99, "cshtrd": 1_000_000,
            "cshoc": cshoc, "ajexdi": ajexdi, "trfd": trfd, "exchg": exchg}


def _crsp_rows():
    out, prev = [], None
    for day, close in zip(JUNE, CRSP_CLOSE):
        split = 2.0 if day == "2025-06-04" else 1.0
        div = 1.0 if day == "2025-06-05" else 0.0
        ret = None
        if prev is not None:
            ret = close / (prev / split) - 1 if split != 1 else (close + div) / prev - 1
        out.append({"permno": "14593", "dlycaldt": day, "dlyprc": close, "dlyopen": close, "dlyhigh": close * 1.01,
                    "dlylow": close * 0.99, "dlyvol": 1_000_000, "dlyret": ret,
                    "shrout": 7_500_000 if day < "2025-06-04" else 15_000_000,
                    "hdrcusip": "03783310", "cusip": "03783310", "ticker": "AAPL",
                    "dlycumfacpr": 1.0 if day >= "2025-06-04" else 2.0, "dlyorddivamt": div})
        prev = close
    return out


def _data():
    secd = [_secd("001690", "01", d, "AAPL", c, a, t, 7.5e9 if a == 2 else 15e9)
            for d, c, a, t in zip(JUNE, COMP_CLOSE, AJEXDI, TRFD)]
    secd += [_secd("001690", "01", "2026-10-26", "AAPL", 201.0, 1.0, 1.0113),
             _secd("001690", "01", "2026-10-27", "AAPL", 202.5, 1.0, 1.0113),
             _secd("099999", "01", "2024-03-01", "OLD.A", 5.0, exchg=11, cusip="680000101"),
             _secd("099999", "01", "2024-03-04", "OLD.A", 5.1, exchg=11, cusip="680000101"),
             _secd("088888", "01", "2025-06-02", "MAPLE", 30.0, curcdd="CAD", exchg=14, cusip="56000A109")]
    return {
        "comp.secd": secd,
        "crsp.dsf_v2": _crsp_rows(),
        "comp.sec_split": [{"gvkey": "001690", "iid": "01", "datadate": "2025-06-04", "split": 2.0}],
        "comp.sec_divid": [{"gvkey": "001690", "iid": "01", "datadate": "2025-06-05", "divd": 1.0, "curcddv": "USD"},
                           {"gvkey": "001690", "iid": "01", "datadate": "2026-10-23", "divd": 0.26,
                            "curcddv": "USD"}],
    }


DATE_OF = {"comp.secd": "datadate", "crsp.dsf_v2": "dlycaldt", "comp.sec_divid": "datadate"}

# WRDS's own column list (``information_schema.columns``, the columns this login can read), as the documented names.
COLUMNS = {
    ("comp", "secd"): [c for c, _ in wrds_history.SECD],
    ("crsp", "dsf_v2"): [c for c, _ in wrds_history.DSF],
    ("comp", "sec_divid"): [c for c, _ in wrds_history.DIVID],
    ("comp", "sec_split"): [c for c, _ in wrds_history.SPLIT],
}


def _catalog(columns=COLUMNS):
    return [{"table_schema": lib, "table_name": t, "column_name": c} for (lib, t), cs in columns.items() for c in cs]


class FakeWrds:
    """The worker's side: answers each table from ``data`` (a year's query its year's rows) as gzip JSON lines of
    text values, as ``wrds_worker.py`` writes them, and records every query."""

    def __init__(self, data):
        self.data, self.queries = data, []

    def query(self, sql: str, out) -> int:
        self.queries.append(sql)
        table = re.search(r"FROM (\w+\.\w+)", sql).group(1)
        rows = self.data.get(table, _catalog() if table == "information_schema.columns" else [])
        year = re.search(r"BETWEEN '(\d{4})-01-01' AND '(\d{4})-12-31'", sql)
        if year:
            rows = [r for r in rows if r[DATE_OF[table]][:4] == year.group(1)]
        write_jsonl(out, rows)
        return len(rows)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def env(monkeypatch, http_mock):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("RC_ALPACA_BASE", "https://alpaca.test")
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 27, 21, 0, tzinfo=timezone.utc))
    monkeypatch.setattr("RevenueChaser.rcdata.build.prices.ROTATION", ())
    monkeypatch.setattr("RevenueChaser.rcdata.build.prices.BENCHMARKS", ("SPY",))
    monkeypatch.setattr("RevenueChaser.rcdata.read.watched", lambda kind: set())
    monkeypatch.setattr(wrds_history, "START_YEAR", 2024)
    write_spine(SPINE)
    http_mock.add("/v2/stocks/bars", BarsAnswer({
        "raw": {"AAPL": [bar(d, c) for d, c in zip(OCT, ALPACA_AAPL)]},
        "all": {"AAPL": [bar(d, c * (1 - 0.26 / 200.0) if d < date(2026, 10, 23) else c)
                         for d, c in zip(OCT, ALPACA_AAPL)]}}))
    http_mock.add("/v1/corporate-actions", {"corporate_actions": {"cash_dividends": [
        {"id": "cd-1", "symbol": "AAPL", "rate": 0.26, "special": False, "foreign": False,
         "process_date": "2026-10-24", "ex_date": "2026-10-23", "currency": "USD"}]}, "next_page_token": None})


@pytest.fixture
def fake(monkeypatch):
    conn = FakeWrds(_data())
    logins = []

    def factory():
        logins.append(1)
        return conn
    monkeypatch.setattr(wrds, "_factory", factory)
    wrds.reset()
    yield conn, logins
    wrds.reset()


def _backfill(day="2026-10-27", by="cli"):
    return base.run_request({"dataset": wrds_history.NAME, "requested_by": by,
                             "params": {"mode": "backfill", "to": day}})


def _q(sql: str, params=()):
    with serving.open_read("prices") as con:
        cur = con.execute(sql, list(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _asked(conn) -> list[str]:
    out = []
    for sql in conn.queries:
        table = re.search(r"FROM (\w+\.\w+)", sql).group(1)
        year = re.search(r"BETWEEN '(\d{4})", sql)
        out.append(f"{table} {year.group(1)}" if year else table)
    return out


# ── What is asked ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_history_is_asked_per_year_from_1993_on_the_us_exchanges_crsp_to_2025(monkeypatch):
    monkeypatch.undo()                                   # the real START_YEAR, not the fixture's 2024
    as_of = date(2026, 10, 27)
    assert wrds_history.years("secd", as_of) == list(range(1993, 2027))
    assert wrds_history.years("dsf", as_of) == list(range(1993, 2026))
    assert wrds_history.years("sec_divid", as_of)[0] == 1993
    sql = wrds_history.year_sql("secd", 1993)
    assert "FROM comp.secd" in sql and "BETWEEN '1993-01-01' AND '1993-12-31'" in sql
    assert "exchg IN (11, 12, 14)" in sql and "ajexdi" in sql and "trfd" in sql and "cshoc" in sql
    dsf = wrds_history.year_sql("dsf", 2025)
    assert "FROM crsp.dsf_v2" in dsf
    for col in ("hdrcusip", "cusip", "ticker", "dlycumfacpr", "dlycumfacshr", "dlyorddivamt"):
        assert col in dsf, col                           # the link columns and the adjustment factor
    assert "ccm" not in " ".join(t["sql"] for t in wrds_history.TABLES.values())


def test_a_backfill_asks_each_table_and_year_once_and_skips_a_partition_that_exists(fake):
    conn, logins = fake
    row = _backfill()
    assert row["result"] == "changed", row
    assert _asked(conn) == ["information_schema.columns",                          # the columns, checked first,
                            "comp.sec_split",                                      # the snapshot (no CCM link),
                            "comp.secd 2026", "comp.sec_divid 2026",                # then the newest year first
                            "comp.secd 2025", "crsp.dsf_v2 2025", "comp.sec_divid 2025",
                            "comp.secd 2024", "crsp.dsf_v2 2024", "comp.sec_divid 2024"]
    assert logins == [1] and ops.calls_today("wrds") == 10
    conn.queries.clear()
    assert _backfill()["result"] in ("changed", "unchanged") and conn.queries == []     # every partition exists
    secd_2024 = wrds_history.partition_path("secd", 2024)
    secd_2024.unlink()
    for raw in wrds_history.raw_dir("secd", 2024).glob("*.jsonl.gz"):
        raw.unlink()
    wrds_history.partition_path("dsf", 2025).unlink()                   # its raw file stays: no query needed
    _backfill()
    assert _asked(conn) == ["comp.secd 2024"]
    assert secd_2024.is_file() and wrds_history.partition_path("dsf", 2025).is_file()


def test_a_wrds_backfill_queued_by_the_schedule_is_skipped_without_a_login(fake):
    conn, logins = fake
    row = _backfill(by="schedule")
    assert row["result"] == "skipped" and "attended" in row["note"]
    assert logins == [] and conn.queries == []


def test_the_backfill_stops_at_the_wrds_day_cap_and_serves_the_years_it_has(fake, monkeypatch):
    conn, _ = fake
    monkeypatch.setitem(caps.DAY_CAPS, "wrds", 4)
    row = _backfill()
    assert row["result"] == "changed", row
    assert len(conn.queries) == 4 and "day cap" in row["note"]   # the columns, the snapshot, 2026's secd and divid
    served = _q("SELECT security_id, count(*) AS n FROM price_bar_daily GROUP BY ALL")
    assert {(r["security_id"], r["n"]) for r in served} == {(AAPL, 2)}



# ── The PERMNO link without CCM (homesrv's subscription refuses crsp_a_ccm, 30 Oct 2026) ─────────────────────────

def _link(dsf, spine, secd=()):
    """``PERMNO_LINK`` over the given staging rows: {permno: (security_id, how)}."""
    con = duckdb.connect()
    con.execute("CREATE TABLE stg_wrds_dsf (permno TEXT, dlycaldt DATE, hdrcusip TEXT, cusip TEXT, ticker TEXT)")
    con.executemany("INSERT INTO stg_wrds_dsf VALUES (?, ?, ?, ?, ?)", dsf)
    con.execute("CREATE TABLE stg_spine (security_id TEXT, ticker TEXT, cusip TEXT, is_etf BOOLEAN, inception DATE)")
    con.executemany("INSERT INTO stg_spine VALUES (?, ?, ?, ?, ?)", [tuple(r) + (None, None)[len(r) - 3:] for r in spine])
    con.execute("CREATE TABLE stg_wrds_secd (gvkey TEXT, iid TEXT, cusip TEXT, datadate DATE)")
    if secd:
        con.executemany("INSERT INTO stg_wrds_secd VALUES (?, ?, ?, ?)", [tuple(r) + (None,)[len(r) - 3:] for r in secd])
    return {p: (sid, how) for p, sid, how in con.execute(
        f"SELECT permno, security_id, how FROM ({wrds_history.PERMNO_LINK})").fetchall()}


END = "2025-12-31"


def test_a_permno_links_by_its_header_cusip_then_an_old_cusip_then_its_last_ticker():
    got = _link(
        dsf=[("14593", END, "03783310", "03783310", "AAPL"),          # header CUSIP = the spine's 9-char's first 8
             ("10001", "2009-06-01", "11111111", "22222222", "OLDN"),   # an old CUSIP of the permno matches
             ("10001", END, "11111111", "11111111", "NEWN"),
             ("84398", END, "78462F10", "78462F10", "SPY")],           # an ETF the spine holds without a CUSIP
        spine=[("001690:01", "AAPL", "037833100"), ("002000:01", "NEWX", "222222229"),
               ("figi:BBG000BDTBL9", "SPY", None, True, "1993-01-22")])
    assert got == {"14593": ("001690:01", "hdrcusip"), "10001": ("002000:01", "cusip"),
                   "84398": ("figi:BBG000BDTBL9", "ticker")}


def test_a_ticker_links_only_a_permno_listed_on_crsps_last_day_and_alone_with_that_ticker():
    got = _link(
        dsf=[("70000", "2015-03-02", "33333333", "33333333", "XYZ"),   # delisted 2015; XYZ today is someone else
             ("80000", END, "44444444", "44444444", "XYZW"),
             ("17778", END, "08467010", "08467010", "BRK"),             # CRSP gives both Berkshire classes "BRK"
             ("83443", END, "08467070", "08467070", "BRK")],
        spine=[("figi:X", "XYZ", None), ("figi:B", "BRK", None), ("072726:02", "BRK-B", "084670702")])
    assert got == {"83443": ("072726:02", "hdrcusip")}           # by CUSIP only; no ticker guess


def test_an_unlinked_permno_reaches_compustat_by_cusip_and_one_permno_takes_one_security():
    got = _link(
        dsf=[("50000", "2004-05-03", "68000010", "68000010", "OLDA"),   # delisted, not on today's spine
             ("14593", END, "03783310", "03783310", "AAPL"),
             ("99990", "2001-01-02", "99999999", "03783310", "APPL")],  # a second PERMNO with AAPL's CUSIP
        spine=[("001690:01", "AAPL", "037833100")],
        secd=[("099999", "01", "680000101"), ("001690", "01", "037833100")])
    assert got == {"50000": ("099999:01", "compustat"), "14593": ("001690:01", "hdrcusip")}


def test_a_ticker_never_hands_a_delisted_names_history_to_the_listing_that_took_its_ticker():
    """Review M1 of #353: ABC is listed on CRSP's last day, delisted in 2026, and its ticker went to a new listing on
    today's spine. Its 1993-2025 bars must not become the newcomer's history: a spine CUSIP that contradicts, an ETF
    that did not exist on CRSP's last day, and a newest row without a ticker all leave the PERMNO unlinked."""
    got = _link(
        dsf=[("60000", END, "55555555", "55555555", "ABC"),             # spine ABC carries another CUSIP
             ("60001", END, "66666666", "66666666", "NEWF"),            # an ETF of that name launched in 2026
             ("60002", "2019-03-01", "77777777", "77777777", "OLDT"),   # its newest row has no ticker
             ("60002", END, "77777777", "77777777", None),
             ("60003", END, "88888888", "88888888", "GOOD"),            # an ETF listed before CRSP's last day
             ("60004", END, "12121212", "12121212", "KEPT")],           # Compustat holds the spine row that day
        spine=[("figi:ABC", "ABC", "999999999"), ("figi:NEWF", "NEWF", None, True, "2026-03-02"),
               ("figi:OLDT", "OLDT", None), ("figi:GOOD", "GOOD", None, True, "2010-05-04"),
               ("012345:01", "KEPT", "343434343")],
        secd=[("012345", "01", "343434343", END)])
    assert got == {"60003": ("figi:GOOD", "ticker"), "60004": ("012345:01", "ticker")}


def test_compustat_bars_of_a_permnos_gvkey_join_the_security_its_crsp_bars_won():
    """Review S2 of #353: an ETF the spine names by FIGI links its PERMNO by ticker, and Compustat holds the same
    fund under a gvkey:iid (the CUSIP matches). Its Compustat bars and actions must join the FIGI security, not stay
    a second security with a bar on every day CRSP has one."""
    con = duckdb.connect()
    con.execute("CREATE TABLE stg_wrds_dsf (permno TEXT, dlycaldt DATE, hdrcusip TEXT, cusip TEXT, ticker TEXT)")
    con.execute(f"INSERT INTO stg_wrds_dsf VALUES ('84398', '{END}', '78462F10', '78462F10', 'SPY')")
    con.execute("CREATE TABLE stg_spine (security_id TEXT, ticker TEXT, cusip TEXT, is_etf BOOLEAN, inception DATE)")
    con.execute("INSERT INTO stg_spine VALUES ('figi:BBG000BDTBL9', 'SPY', NULL, true, '1993-01-22'), "
                "('001690:01', 'AAPL', '037833100', false, NULL)")
    con.execute("CREATE TABLE stg_wrds_secd (gvkey TEXT, iid TEXT, datadate DATE, tic TEXT, cusip TEXT, curcdd TEXT, "
                "prccd DOUBLE, prcod DOUBLE, prchd DOUBLE, prcld DOUBLE, cshtrd DOUBLE, cshoc DOUBLE, ajexdi DOUBLE, "
                "trfd DOUBLE, source_as_of DATE, fetched_at DOUBLE, run_id TEXT)")
    for gvkey, tic, cusip in (("001000", "SPY", "78462F103"), ("001690", "AAPL", "037833100")):
        con.execute("INSERT INTO stg_wrds_secd VALUES (?, '01', ?, ?, ?, 'USD', 400, 400, 401, 399, 1, 1, 1, 1, "
                    "NULL, NULL, NULL)", [gvkey, END, tic, cusip])
    con.execute(f"CREATE TEMP TABLE _permno_link AS {wrds_history.PERMNO_LINK}")
    con.execute(f"CREATE TEMP TABLE _gvkey_alias AS {wrds_history.GVKEY_ALIAS}")
    got = sorted(con.execute(f"SELECT security_id, symbol FROM ({wrds_history.COMP_BARS})").fetchall())
    assert got == [("001690:01", "AAPL"), ("figi:BBG000BDTBL9", "SPY")], "SPY's Compustat bar is the FIGI's own"
    con.execute("CREATE TABLE stg_wrds_split (gvkey TEXT, iid TEXT, datadate DATE, split DOUBLE, source_as_of DATE, "
                "fetched_at DOUBLE, run_id TEXT)")
    con.execute("CREATE TABLE stg_wrds_divid (gvkey TEXT, iid TEXT, datadate DATE, divd DOUBLE, div DOUBLE, "
                "divsp DOUBLE, curcddv TEXT, recorddate DATE, paydate DATE, source_as_of DATE, fetched_at DOUBLE, "
                "run_id TEXT)")
    con.execute(f"INSERT INTO stg_wrds_divid VALUES ('001000', '01', '{END}', 1.5, NULL, NULL, 'USD', NULL, NULL, "
                "NULL, NULL, NULL)")
    acts = con.execute(f"SELECT security_id FROM ({wrds_history.ACTIONS_SQL})").fetchall()
    assert acts == [("figi:BBG000BDTBL9",)], "and so is its Compustat dividend"


# ── Compustat's own adjusted return: the ajexdi/trfd rule ─────────────────────────────────────────────────────

def test_compustat_adjusted_return_is_the_true_total_return_within_1bp():
    con = duckdb.connect()
    con.execute("CREATE TABLE s (gvkey TEXT, iid TEXT, datadate DATE, prccd DOUBLE, ajexdi DOUBLE, trfd DOUBLE)")
    for d, c, a, t in zip(JUNE, COMP_CLOSE, AJEXDI, TRFD):
        con.execute("INSERT INTO s VALUES ('001690', '01', ?, ?, ?, ?)", [d, c, a, t])
    got = [r[0] for r in con.execute(f"SELECT {wrds_history.COMP_RETURN} AS r FROM s "
                                     "WINDOW w AS (PARTITION BY gvkey, iid ORDER BY datadate) ORDER BY datadate").fetchall()]
    # The economic return: the close plus the dividend over the previous close, both in one share basis.
    want = [None, 202.0 / 200.0 - 1, 101.5 / (202.0 / 2) - 1, (100.0 + 1.0) / 101.5 - 1, 100.5 / 100.0 - 1]
    assert got[0] is None
    for g, w in zip(got[1:], want[1:]):
        assert abs(g - w) <= 1e-4, (got, want)


# ── The served history ────────────────────────────────────────────────────────────────────────────────────────

def _build():
    assert _backfill()["result"] == "changed"
    assert base.run_request({"dataset": actions.NAME, "params": {"as_of": "2026-10-27"}})["result"] != "failed"
    row = base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})
    assert row["result"] == "changed", row


@pytest.fixture
def built(fake):
    _build()
    return fake


def test_history_bars_crsp_first_then_compustat_and_alpaca_wins_its_sessions(built):
    june = _q("SELECT * FROM price_bar_daily WHERE security_id = ? AND session_date < '2026-01-01' "
              "ORDER BY session_date", [AAPL])
    assert [str(r["session_date"]) for r in june] == JUNE
    assert {r["source"] for r in june} == {wrds_history.CRSP}
    assert june[1]["close"] == 202.1                                     # CRSP's, not Compustat's 202.0
    assert june[0]["shares_outstanding"] == 7_500_000_000 and june[2]["shares_outstanding"] == 15_000_000_000
    october = _q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = ? "
                 "AND session_date >= '2026-10-26' ORDER BY session_date", [AAPL])
    assert [(r["close"], r["source"]) for r in october] == [(201.0, bars.NAME), (202.0, bars.NAME)]
    old = _q("SELECT * FROM price_bar_daily WHERE security_id = ? ORDER BY session_date", [OLDCO])
    assert [(r["close"], r["symbol"], r["source"]) for r in old] == [(5.0, "OLD-A", wrds_history.COMPUSTAT),
                                                                     (5.1, "OLD-A", wrds_history.COMPUSTAT)]
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = '088888:01'")[0]["n"] == 0  # CAD
    assert _q("SELECT ticker FROM price_bars_daily WHERE session_date = '2024-03-01'")[0]["ticker"] == "OLD-A"


@pytest.mark.parametrize("col", ["hdrcusip", "ticker"])
def test_a_crsp_year_without_its_link_columns_is_a_run_issue(monkeypatch, col):
    """Review N1 of #353: the link reads ``hdrcusip`` and ``ticker``; a year that answers none is an issue, not a
    quiet year of unlinked PERMNOs."""
    data = _data()
    data["crsp.dsf_v2"] = [{**r, col: None} for r in data["crsp.dsf_v2"]]
    conn = FakeWrds(data)
    monkeypatch.setattr(wrds, "_factory", lambda: conn)
    wrds.reset()
    _backfill()
    n, issues = ops.open_issues(wrds_history.NAME)
    assert f"dsf.{col}" in {i["key"] for i in issues}
    wrds.reset()


def test_the_build_note_counts_the_crsp_link_by_rule_and_the_unlinked_permnos(fake):
    """Review S4 of #353: the link is observable. AAPL's PERMNO links by its header CUSIP; nothing is unlinked."""
    assert _backfill()["result"] == "changed"
    row = base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})
    assert "CRSP link: 1 by hdrcusip, 0 by cusip, 0 by ticker, 0 by compustat; 0 unlinked; 0 gvkey aliases" in row["note"]


def test_crsp_rows_reach_their_securities_without_ccm(fake):
    conn, _ = fake
    # SPY by ticker on CRSP's last day (the spine holds no CUSIP for it), OLDCO by Compustat's CUSIP.
    conn.data["crsp.dsf_v2"] += [
        {"permno": "84398", "dlycaldt": "2025-06-06", "dlyprc": 590.0, "dlyvol": 5e7, "dlyret": 0.001,
         "shrout": 1e6, "hdrcusip": "78462F10", "cusip": "78462F10", "ticker": "SPY"},
        {"permno": "77777", "dlycaldt": "2024-03-01", "dlyprc": -5.05, "dlyvol": 1e4, "dlyret": 0.0,
         "shrout": 1e3, "hdrcusip": "68000010", "cusip": "68000010", "ticker": "OLDA"}]
    _build()
    spy = _q("SELECT security_id, close, source FROM price_bar_daily WHERE session_date = '2025-06-06' "
             "AND source = ?", [wrds_history.CRSP])
    assert {(r["security_id"], r["close"]) for r in spy} == {(AAPL, 100.5), ("001000:01", 590.0)}
    old = _q("SELECT close, source FROM price_bar_daily WHERE security_id = ? AND session_date = '2024-03-01'",
             [OLDCO])
    assert old == [{"close": 5.05, "source": wrds_history.CRSP}]           # CRSP first, as with the CCM link
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id LIKE 'permno:%'")


def test_the_yahoo_shares_check_reads_the_newest_cover_sourced_cap_not_the_newest_cap(fake):
    # The Opus delta review's nit 1: the check took the newest cap on or before Yahoo's day and only then kept it if
    # its shares were the cover's. On the 26th and 27th Alpaca's bars take Compustat's shares of the session, so the
    # newest cap is not cover-sourced and AAPL, whose cover count is a third over Yahoo's, was skipped silently.
    base.upsert_partition([{"cik": "320193", "accession": "0000320193-26-000090", "form": "10-K",
                            "filed": date(2026, 10, 18), "period_end": None, "class_member": "", "symbol": "AAPL",
                            "shares": 14_800_000_000, "as_of": date(2026, 10, 17), "source_as_of": date(2026, 10, 27),
                            "fetched_at": 1.0, "run_id": "cover"}], cover_shares.SCHEMA, cover_shares.KEY,
                          cover_shares.partition_path(2026))
    base.upsert_partition([{"ticker": "AAPL", "security_id": AAPL, "float_shares": 9_900_000_000,
                            "shares_outstanding": 10_000_000_000, "source_as_of": date(2026, 10, 27),
                            "fetched_at": 1.0, "run_id": "yf"}], floats.YAHOO_SCHEMA, ["ticker"],
                          base.parquet_root() / "yfinance" / "float" / "date=2026-10-27" / "part-0.parquet")
    _build()
    source = {str(r["session_date"]): r["shares_source"] for r in _q(
        "SELECT session_date, shares_source FROM market_cap_daily WHERE security_id = ? "
        "AND session_date >= '2026-10-21'", [AAPL])}
    assert source == {"2026-10-21": cover_shares.NAME, "2026-10-22": cover_shares.NAME,
                      "2026-10-23": cover_shares.NAME, "2026-10-26": wrds_history.COMPUSTAT,
                      "2026-10-27": wrds_history.COMPUSTAT}
    keys = {i["key"]: i for i in ops.open_issues(prices_serving.NAME, limit=500)[1]
            if i["kind"] == "shares_disagree_yahoo"}
    assert set(keys) == {AAPL}


def test_wrds_actions_feed_the_factors_and_alpaca_wins_an_action_both_know(built):
    acts = _q("SELECT ex_date, action, ratio, amount, source, applied FROM corporate_action WHERE security_id = ? "
              "ORDER BY ex_date, action", [AAPL])
    assert [(str(a["ex_date"]), a["action"], a["source"]) for a in acts] == [
        ("2025-06-04", "split", wrds_history.NAME), ("2025-06-05", "cash_dividend", wrds_history.NAME),
        ("2026-10-23", "cash_dividend", actions.NAME)]
    assert acts[0]["ratio"] == 2.0 and acts[1]["amount"] == 1.0 and all(a["applied"] for a in acts)
    view = {str(r["session_date"]): r for r in _q(
        "SELECT session_date, close, adj_close FROM price_bars_daily WHERE ticker = 'AAPL'")}
    assert view["2025-06-03"]["close"] == pytest.approx(202.1 / 2)       # split-adjusted to today's shares
    tr = (1 - 1.0 / 101.5) * (1 - 0.26 / 200.0)                       # the closes before the ex-dates
    assert view["2025-06-03"]["adj_close"] == pytest.approx(202.1 / 2 * tr)


def test_the_difference_report_counts_close_and_return_differences(built):
    report = {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")}
    assert set(report) == set(wrds_history.COMPARISONS)
    crsp_comp = report["crsp_vs_compustat"]
    assert (crsp_comp["sessions"], crsp_comp["close_differs"]) == (5, 1)
    assert crsp_comp["max_close_diff"] == pytest.approx(0.1 / 202.0)
    comp_alp = report["compustat_vs_alpaca"]
    assert (comp_alp["sessions"], comp_alp["close_differs"]) == (2, 1)
    for source in ("compustat_returns", "crsp_returns"):
        assert report[source]["sessions"] >= 4 and report[source]["return_differs"] == 0, report[source]
    assert report["crsp_vs_alpaca"]["sessions"] == 0
    assert {str(r["as_of"]) for r in report.values()} == {"2026-10-27"}


def test_the_monthly_refresh_keeps_the_values_it_replaces_as_revisions(built):
    conn, _ = built
    conn.queries.clear()
    for r in conn.data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] == "2025-06-03":
            r["prccd"] = 202.5
    row = base.run_request({"dataset": wrds_history.NAME, "requested_by": "cli",
                            "params": {"as_of": "2026-11-27"}})
    assert row["result"] == "changed", row
    assert _asked(conn) == ["information_schema.columns",                    # a new day: the columns again
                            "comp.sec_split", "comp.secd 2026", "comp.sec_divid 2026",
                            "comp.secd 2025", "crsp.dsf_v2 2025", "comp.sec_divid 2025", "crsp.dsf_v2 2024"]
    revs = _q("SELECT * FROM price_revision WHERE source = ?", [wrds_history.COMPUSTAT])
    assert [(r["security_id"], str(r["session_date"]), r["close"]) for r in revs] == [(AAPL, "2025-06-03", 202.0)]
    assert revs[0]["revised_by"] == ops.latest_check(wrds_history.NAME)["run_id"]
    report = {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")}
    assert report["crsp_vs_compustat"]["max_close_diff"] == pytest.approx(0.4 / 202.5)
    assert {str(r["as_of"]) for r in report.values()} == {"2026-11-27"}


# ── The factor lookup at the WRDS history's size (homesrv, 30 Oct 2026: request 34 took 41 of its 50 minutes) ──

@pytest.mark.parametrize("threshold", [0, 1000])
def test_a_day_finds_its_factor_by_asof_join_as_the_period_rule_does(threshold):
    """``factor_join`` (and the read side's ``_FACTOR_JOIN``) match the row ``period`` matches, at every edge: before
    the first period, on an ex-date, after the last one (no row: factor 1), a NULL day (a SEC cover with no as-of) and
    a NULL security on either side. Run under both of DuckDB's ASOF plans: threshold 0 is the ASOF_JOIN operator every
    real build uses (more than ``asof_loop_join_threshold`` left rows), 1000 the nested-loop rewrite of a small table;
    the operator matched a NULL day to the oldest period until the day was read as ``DATE '9999-12-31'``."""
    con = duckdb.connect()
    con.execute(f"SET asof_loop_join_threshold = {threshold}")
    con.execute("CREATE TABLE adj_factor (security_id TEXT, valid_from DATE, valid_to DATE, split_factor DOUBLE)")
    con.execute("""INSERT INTO adj_factor VALUES ('x', NULL, DATE '2025-03-03', 4.0),
        ('x', DATE '2025-03-03', DATE '2025-06-04', 2.0), ('y', NULL, DATE '2025-06-04', 3.0),
        (NULL, NULL, DATE '2025-06-04', 5.0)""")
    con.execute("CREATE TABLE b (security_id TEXT, session_date DATE)")
    days = ["2024-01-02", "2025-03-02", "2025-03-03", "2025-06-03", "2025-06-04", "2026-10-27", None]
    con.executemany("INSERT INTO b VALUES (?, ?)", [(s, d) for s in ("x", "y", "z", None) for d in days])
    rule = con.execute(f"""SELECT b.security_id, b.session_date, a.split_factor FROM b LEFT JOIN adj_factor a
        ON {prices_serving.period('a', 'b.security_id', 'b.session_date')} ORDER BY ALL""").fetchall()
    for join in (prices_serving.factor_join("a", "b.security_id", "b.session_date"), read._FACTOR_JOIN):
        asof = con.execute(f"SELECT b.security_id, b.session_date, a.split_factor FROM b {join} ORDER BY ALL").fetchall()
        assert asof == rule, join
    assert [f for s, d, f in rule if s == "x"] == [4.0, 4.0, 2.0, 2.0, None, None, None]
    assert all(f is None for s, d, f in rule if s is None or d is None)


def test_no_served_lookup_of_the_factors_is_a_blockwise_loop(built):
    """The range join on ``adj_factor`` planned as ``BLOCKWISE_NL_JOIN``: 347 s for one pass over 5.6M Compustat bars,
    three passes in the build. Every lookup of a column day is now an ASOF join, the read views too."""
    with serving.open_read("prices") as con:
        for view in ("price_bars_daily", "price_actions"):
            plan = "\n".join(r[1] for r in con.execute(f"EXPLAIN SELECT * FROM {view}").fetchall())
            assert "BLOCKWISE_NL_JOIN" not in plan, (view, plan)
        for sql in (prices_serving.MARKET_CAP_SQL.format(within=prices_serving.ISSUER_WITHIN_DAYS),):
            assert sql.count("ASOF LEFT JOIN adj_factor") == 2


# ── A subscription without CRSP (homesrv, 30 Oct 2026: run 32 refused on ``crsp_a_ccm``) ──────────────────────

REFUSED = ("ProgrammingError: (psycopg2.errors.InsufficientPrivilege) permission denied for schema {schema}\n\n"
           "[SQL: {sql}]")


class RefusingWrds(FakeWrds):
    """The fake worker of a subscription that holds Compustat but not CRSP: every query of a ``refused`` schema
    answers the error the real worker sends back, as ``wrds_worker.py`` reports a failed query."""

    def __init__(self, data, refused=("crsp",)):
        super().__init__(data)
        self.refused = refused

    def query(self, sql: str, out) -> int:
        table = re.search(r"FROM (\w+\.\w+)", sql).group(1)
        if table.split(".")[0] in self.refused:
            self.queries.append(sql)
            raise base.SourceError("wrds query failed: " + REFUSED.format(
                schema="crsp_a_ccm" if "ccm" in table else "crsp_a_stock", sql=sql))
        return super().query(sql, out)


@pytest.fixture
def refusing(monkeypatch):
    def install(refused=("crsp",)):
        conn = RefusingWrds(_data(), refused)
        monkeypatch.setattr(wrds, "_factory", lambda: conn)
        wrds.reset()
        return conn
    yield install
    wrds.reset()


def test_a_crsp_permission_refusal_skips_crsp_once_and_serves_compustat(refusing):
    conn = refusing()
    row = _backfill()
    assert row["result"] == "changed", row
    # CRSP is refused once; its later years are then never asked in this run (no query per year, no loop).
    assert _asked(conn) == ["information_schema.columns", "comp.sec_split",
                            "comp.secd 2026", "comp.sec_divid 2026",
                            "comp.secd 2025", "crsp.dsf_v2 2025", "comp.sec_divid 2025",
                            "comp.secd 2024", "comp.sec_divid 2024"]
    assert "crsp_a_stock" in row["note"] and "CRSP skipped" in row["note"]
    issues = [i for i in ops.open_issues(wrds_history.NAME, limit=50)[1] if i["kind"] == "wrds_no_access"]
    assert [i["key"] for i in issues] == ["crsp_a_stock"]
    # A refused query leaves no file behind: no raw answer, no sidecar that would read as "asked".
    assert not [f for f in wrds_history.raw_dir("dsf").rglob("*") if f.is_file()]
    # Compustat alone serves the bars, the shares and so the cap.
    assert base.run_request({"dataset": actions.NAME, "params": {"as_of": "2026-10-27"}})["result"] != "failed"
    assert base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})["result"] == "changed"
    june = _q("SELECT session_date, close, source, shares_outstanding FROM price_bar_daily WHERE security_id = ? "
              "AND session_date < '2026-01-01' ORDER BY session_date", [AAPL])
    assert [str(r["session_date"]) for r in june] == JUNE
    assert {r["source"] for r in june} == {wrds_history.COMPUSTAT} and june[1]["close"] == 202.0
    cap = _q("SELECT market_cap, shares_source FROM market_cap_daily WHERE security_id = ? "
             "AND session_date = '2025-06-02'", [AAPL])
    assert cap and cap[0]["market_cap"] == pytest.approx(200.0 * 7.5e9) and cap[0]["shares_source"] == (
        wrds_history.COMPUSTAT)
    # The difference report keeps its rows; the CRSP ones have no session to compare.
    report = {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")}
    assert set(report) == set(wrds_history.COMPARISONS)
    assert {n: report[n]["sessions"] for n in ("crsp_vs_compustat", "crsp_vs_alpaca", "crsp_returns")} == {
        "crsp_vs_compustat": 0, "crsp_vs_alpaca": 0, "crsp_returns": 0}
    assert report["compustat_vs_alpaca"]["sessions"] == 2


def test_the_next_backfill_asks_the_refused_crsp_year_again_and_nothing_compustat_holds(refusing):
    refusing()
    _backfill()
    conn = refusing()
    _backfill()
    assert _asked(conn) == ["crsp.dsf_v2 2025"]             # once a run, so a granted subscription is picked up


def test_a_refused_compustat_query_still_fails_the_run(refusing):
    refusing(refused=("comp",))
    row = _backfill()
    assert row["result"] == "failed" and "permission denied" in row["note"]


def test_another_wrds_error_on_crsp_still_fails_the_run(fake, monkeypatch):
    conn, _ = fake

    def broken(sql, out):
        if "crsp." in sql:
            raise base.SourceError('wrds query failed: ProgrammingError: column "hdrcusip" does not exist')
        return FakeWrds.query(conn, sql, out)
    monkeypatch.setattr(conn, "query", broken)
    row = _backfill()
    assert row["result"] == "failed" and "hdrcusip" in row["note"]


# ── The columns, checked before any data is asked (one ``information_schema`` query on the same login) ──────────

def test_a_compustat_column_wrds_lacks_fails_the_run_before_any_data_is_asked(fake, monkeypatch):
    conn, _ = fake
    columns = dict(COLUMNS)
    columns[("comp", "secd")] = [c for c in COLUMNS[("comp", "secd")] if c != "exchg"]
    monkeypatch.setitem(conn.data, "information_schema.columns", _catalog(columns))
    row = _backfill()
    assert row["result"] == "failed" and "comp.secd" in row["note"] and "exchg" in row["note"], row
    assert _asked(conn) == ["information_schema.columns"]            # no data query, no year spent


def test_a_crsp_library_this_login_cannot_read_is_skipped_unasked(fake, monkeypatch):
    conn, _ = fake
    readable = {k: v for k, v in COLUMNS.items() if k[0] != "crsp"}
    monkeypatch.setitem(conn.data, "information_schema.columns", _catalog(readable))
    row = _backfill()
    assert row["result"] == "changed", row
    assert _asked(conn) == ["information_schema.columns", "comp.sec_split",
                            "comp.secd 2026", "comp.sec_divid 2026", "comp.secd 2025", "comp.sec_divid 2025",
                            "comp.secd 2024", "comp.sec_divid 2024"]
    issues = [i for i in ops.open_issues(wrds_history.NAME, limit=50)[1] if i["kind"] == "wrds_no_access"]
    assert [i["key"] for i in issues] == ["crsp"] and "CRSP skipped" in row["note"]


def test_a_crsp_column_wrds_lacks_skips_crsp_and_compustat_still_serves(fake, monkeypatch):
    # The delta review's MUST: a CRSP column the catalog lacks degrades like a CRSP library it lacks (crsp.dsf_v2's
    # CIZ names are the least verified here), so the attended run still fills Compustat and no Duo is wasted.
    conn, _ = fake
    columns = dict(COLUMNS)
    columns[("crsp", "dsf_v2")] = [c for c in COLUMNS[("crsp", "dsf_v2")] if c != "dlyopen"]
    monkeypatch.setitem(conn.data, "information_schema.columns", _catalog(columns))
    row = _backfill()
    assert row["result"] == "changed", row
    assert _asked(conn) == ["information_schema.columns", "comp.sec_split",
                            "comp.secd 2026", "comp.sec_divid 2026", "comp.secd 2025", "comp.sec_divid 2025",
                            "comp.secd 2024", "comp.sec_divid 2024"]
    issues = [i for i in ops.open_issues(wrds_history.NAME, limit=50)[1] if i["kind"] == "wrds_no_access"]
    assert [i["key"] for i in issues] == ["crsp"] and "dlyopen" in issues[0]["detail"]
    assert "CRSP skipped" in row["note"]
    assert base.run_request({"dataset": actions.NAME, "params": {"as_of": "2026-10-27"}})["result"] != "failed"
    assert base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})["result"] == "changed"
    june = _q("SELECT source FROM price_bar_daily WHERE security_id = ? AND session_date < '2026-01-01'", [AAPL])
    assert len(june) == 5 and {r["source"] for r in june} == {wrds_history.COMPUSTAT}
    cap = _q("SELECT market_cap FROM market_cap_daily WHERE security_id = ? AND session_date = '2025-06-02'",
             [AAPL])
    assert cap and cap[0]["market_cap"] == pytest.approx(200.0 * 7.5e9)
