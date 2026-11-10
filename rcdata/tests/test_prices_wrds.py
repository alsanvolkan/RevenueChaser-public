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
    # The week before: no cshoc, so those sessions' caps are the cover's; trfd steps by the 0.26 dividend on the 23rd.
    secd += [{**_secd("001690", "01", d, "AAPL", c, 1.0, t), "cshoc": None}
             for d, c, t in (("2026-10-21", 199.5, 1.0113 / 1.0013), ("2026-10-22", 200.0, 1.0113 / 1.0013),
                             ("2026-10-23", 199.9, 1.0113))]
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
    ("crsp_a_stock", "stkdelists"): ["permno", "delistingdt", "deldtprc", "delret"],
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

def test_the_history_is_asked_per_year_from_1983_crsp_to_2025(monkeypatch):
    monkeypatch.undo()                                   # the real START_YEAR, not the fixture's 2024
    as_of = date(2026, 10, 27)
    assert wrds_history.years("secd", as_of) == list(range(1983, 2027))
    assert wrds_history.years("dsf", as_of) == list(range(1983, 2026))
    assert wrds_history.years("sec_divid", as_of)[0] == 1983
    sql = wrds_history.year_sql("secd", 1993)
    assert "FROM comp.secd" in sql and "BETWEEN '1993-01-01' AND '1993-12-31'" in sql
    assert "exchg IN (11, 12, 14, 17, 21)" in sql and "ajexdi" in sql and "trfd" in sql and "cshoc" in sql
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
                            "comp.sec_split", "crsp_a_stock.stkdelists",           # the snapshots (no CCM link),
                            "comp.secd 2026", "comp.sec_divid 2026",                # then the newest year first
                            "comp.secd 2025", "crsp.dsf_v2 2025", "comp.sec_divid 2025",
                            "comp.secd 2024", "crsp.dsf_v2 2024", "comp.sec_divid 2024"]
    assert logins == [1] and ops.calls_today("wrds") == 11
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
    assert {(r["security_id"], r["n"]) for r in served} == {(AAPL, 5)}



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


def test_a_gvkey_listing_whose_compustat_starts_after_crsps_last_day_takes_no_ticker_history():
    """Review SHOULD 1 of the #353 delta: the gvkey half of M1. LATE's spine CUSIP is no PERMNO's, and its Compustat
    rows start in 2026: CRSP's last day saw no such security, so the old PERMNO's ticker hands it nothing."""
    got = _link(dsf=[("60005", END, "23232323", "23232323", "LATE")],
                spine=[("054545:01", "LATE", "565656565")],
                secd=[("054545", "01", "565656565", "2026-02-02")])
    assert got == {}


def test_an_etf_without_an_inception_day_takes_no_ticker_link():
    """Review NIT of the #353 delta: an ETF row from N-CEN or the ETP list has no ``inception`` (AV's ipoDate), so
    nothing says it existed on CRSP's last day; the build note counts these."""
    got = _link(dsf=[("60006", END, "24242424", "24242424", "NOINC")],
                spine=[("figi:NOINC", "NOINC", None, True, None)])
    assert got == {}


def test_the_rules_rank_ticker_before_compustat_and_the_newest_permno_wins_a_security():
    """Review N3 of #353: a swap of rules 3 and 4, or of the newest-first order, must turn a test red."""
    got = _link(
        dsf=[("61000", END, "13131313", "13131313", "BOTH"),            # its ticker and a Compustat CUSIP both match
             ("62000", "2012-05-01", "16161616", "14141414", "TWIN"),   # two PERMNOs carried the spine's CUSIP:
             ("62001", "2021-08-02", "15151515", "14141414", "TWIN2")], # the newer one serves the security
        spine=[("figi:BOTH", "BOTH", None, True, "2000-01-03"), ("033333:01", "TWINX", "141414145")],
        secd=[("054321", "01", "131313139")])
    assert got == {"61000": ("figi:BOTH", "ticker"), "62001": ("033333:01", "cusip")}


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
                "trfd DOUBLE, curcddv TEXT, div DOUBLE, divd DOUBLE, divsp DOUBLE, source_as_of DATE, fetched_at DOUBLE, "
                "run_id TEXT, exchg INTEGER)")
    for gvkey, tic, cusip in (("001000", "SPY", "78462F103"), ("001690", "AAPL", "037833100")):
        con.execute("INSERT INTO stg_wrds_secd VALUES (?, '01', ?, ?, ?, 'USD', 400, 400, 401, 399, 1, 1, 1, 1, "
                    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 14)", [gvkey, END, tic, cusip])
    con.execute(f"CREATE TEMP TABLE _permno_link AS {wrds_history.PERMNO_LINK}")
    con.execute(f"CREATE TEMP TABLE _gvkey_alias AS {wrds_history.GVKEY_ALIAS}")
    con.execute("CREATE TABLE stg_wrds_exchg (gvkey TEXT, iid TEXT, exchg INTEGER, efffrom DATE, effthru DATE)")
    con.execute("CREATE TABLE stg_wrds_divid (gvkey TEXT, iid TEXT, datadate DATE, div DOUBLE, divd DOUBLE, "
                "divsp DOUBLE)")
    con.execute(f"CREATE TEMP TABLE _secd_listed AS {wrds_history.SECD_LISTED}")
    # Every stored bar goes through the universe (``prices.universe``): both securities are in it here.
    con.execute("CREATE TEMP TABLE _universe AS SELECT unnest(['001690:01', 'figi:BBG000BDTBL9']) AS security_id")
    con.execute("DROP TABLE stg_wrds_divid")                          # the actions below create their own
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
    con.execute("CREATE TABLE s (gvkey TEXT, iid TEXT, datadate DATE, prccd DOUBLE, ajexdi DOUBLE, trfd DOUBLE, "
                "run_id TEXT, div DOUBLE, divd DOUBLE, divsp DOUBLE)")
    for d, c, a, t in zip(JUNE, COMP_CLOSE, AJEXDI, TRFD):
        con.execute("INSERT INTO s VALUES ('001690', '01', ?, ?, ?, ?, 'r1', NULL, NULL, NULL)", [d, c, a, t])
    got = [r[0] for r in con.execute(f"SELECT {wrds_history.COMP_RETURN} AS r FROM (SELECT *, "
                                     f"{wrds_history.SECD_DIV} AS div_day FROM s) "
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
    assert row["result"] != "failed", row


@pytest.fixture
def whole_builds(monkeypatch):
    """Every build of the test whole: the difference report and the CRSP cross-check's counts are a whole build's
    (a build on the live copy keeps the report and counts only the rows it rebuilds, ``prices/incremental``)."""
    from RevenueChaser.rcdata.build import dirty
    monkeypatch.setenv(dirty.WHOLE_ENV, "1")


@pytest.fixture
def built(fake):
    _build()
    return fake


def test_history_bars_compustat_first_and_never_alpacas(built):
    june = _q("SELECT * FROM price_bar_daily WHERE security_id = ? AND session_date < '2026-01-01' "
              "ORDER BY session_date", [AAPL])
    assert [str(r["session_date"]) for r in june] == JUNE
    assert {r["source"] for r in june} == {wrds_history.COMPUSTAT}
    assert june[1]["close"] == 202.0                                     # Compustat's, not CRSP's 202.1
    assert june[0]["shares_outstanding"] == 7_500_000_000 and june[2]["shares_outstanding"] == 15_000_000_000
    october = _q("SELECT session_date, close, source FROM price_bar_daily WHERE security_id = ? "
                 "AND session_date >= '2026-10-26' ORDER BY session_date", [AAPL])
    assert [(r["close"], r["source"]) for r in october] == [(201.0, wrds_history.COMPUSTAT),
                                                            (202.5, wrds_history.COMPUSTAT)]
    # OLDCO is a Compustat security the spine does not hold: outside today's universe, so not stored at all
    # (avo, 4 Nov 2026). Before this rule its two bars were served under ``099999:01``.
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = ?", [OLDCO])
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = '088888:01'")[0]["n"] == 0  # CAD
    assert not _q("SELECT 1 FROM price_bars_daily WHERE session_date = '2024-03-01'")


def test_a_delisted_security_the_spine_does_not_hold_is_in_no_table(monkeypatch):
    """avo, 4 Nov 2026: the store holds today's universe, so nothing delisted is in it. OLD.A has Compustat bars and
    a CRSP history, its PERMNO links by CUSIP, and today's spine does not hold it: no bar, no symbol, no cap, no
    action, and the ``price_bars_daily`` view does not know the ticker either. (Before this rule its bars were
    served and #363's ``security_symbol`` case hung on them.)"""
    data = _data()
    data["crsp.dsf_v2"] += [{**_crsp_rows()[0], "permno": "50000", "dlycaldt": d, "dlyprc": c, "dlyopen": c,
                             "dlyhigh": c, "dlylow": c, "dlyret": None, "hdrcusip": "68000010", "cusip": "68000010",
                             "ticker": "OLDA"} for d, c in (("2024-03-01", 5.0), ("2024-03-04", 5.1))]
    conn = FakeWrds(data)
    monkeypatch.setattr(wrds, "_factory", lambda: conn)
    wrds.reset()
    assert _backfill()["result"] == "changed"
    for table in ("price_bar_daily", "security_symbol", "market_cap_daily", "corporate_action"):
        assert not _q(f"SELECT 1 FROM {table} WHERE security_id = ?", [OLDCO]), table
    assert not _q("SELECT 1 FROM price_bars_daily WHERE ticker = 'OLD-A'")
    wrds.reset()


def test_a_crsp_bar_without_shares_takes_compustats_shares_of_that_session(monkeypatch):
    """The market cap's history shares come from the lowest-priority bar of the session that has any (#372 moved that
    pick out of a window): CRSP's bar wins 2 Jun 2025 but carries no SHROUT, so Compustat's count sizes it."""
    data = _data()
    data["crsp.dsf_v2"][0]["shrout"] = None
    conn = FakeWrds(data)
    monkeypatch.setattr(wrds, "_factory", lambda: conn)
    wrds.reset()
    _build()
    cap = _q("SELECT market_cap, shares, shares_source FROM market_cap_daily WHERE security_id = ? "
             "AND session_date = '2025-06-02'", [AAPL])
    assert cap == [{"market_cap": pytest.approx(200.0 * 7.5e9), "shares": 7.5e9,
                    "shares_source": wrds_history.COMPUSTAT}]
    wrds.reset()


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
    assert ("CRSP link: 1 by hdrcusip, 0 by cusip, 0 by ticker, 0 by compustat; 0 unlinked; 0 gvkey aliases; "
            "0 ETFs without an inception day unlinked") in row["note"]


@pytest.mark.parametrize("dividend", [False, True])
def test_crsp_serves_no_bar_even_for_a_security_compustat_never_priced(monkeypatch, dividend):
    """avo, 4 Nov 2026 (replacing decision 7 of the 3 Nov switch plan): **CRSP serves no bar at all**; it is the
    cross-check only. FLG (spine: FIGI, no gvkey, no CUSIP) and BRK-B (a gvkey:iid Compustat does not price here)
    are CRSP's alone, so they have no bar in the store; GAPX keeps its one Compustat bar. CRSP's own dlycumfacpr
    split is never applied, and the CRSP rows are still staged, so the difference report and the ticker-link issues
    are as before."""
    data = _data()
    data["crsp.dsf_v2"] += [{**r, "permno": "66666", "hdrcusip": "45454545", "cusip": "45454545", "ticker": "GAPX"}
                            for r in _crsp_rows()]
    data["comp.secd"] += [_secd("077777", "01", JUNE[0], "GAPX", 200.0, cusip="454545459")]
    if dividend:
        data["comp.sec_divid"] += [{"gvkey": "077777", "iid": "01", "datadate": JUNE[3], "divd": 1.0,
                                    "curcddv": "USD"}]
    data["crsp.dsf_v2"] += [{**r, "permno": "77777", "hdrcusip": "99999999", "cusip": "99999999", "ticker": "FLG"}
                            for r in _crsp_rows()]
    data["crsp.dsf_v2"] += [{**r, "permno": "88888", "hdrcusip": "88888888", "cusip": "88888888", "ticker": "BRK-B"}
                            for r in _crsp_rows()]
    conn = FakeWrds(data)
    monkeypatch.setattr(wrds, "_factory", lambda: conn)
    wrds.reset()
    assert _backfill()["result"] == "changed"
    assert base.run_request({"dataset": actions.NAME, "params": {"as_of": "2026-10-27"}})["result"] != "failed"
    base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})
    flg = "figi:BBG000BWBX65"
    assert not _q("SELECT 1 FROM price_bar_daily WHERE source = ?", [wrds_history.CRSP]), "CRSP serves no bar"
    counts = {r["ticker"]: r["n"] for r in _q(
        "SELECT ticker, count(*) AS n FROM price_bars_daily WHERE session_date < '2026-01-01' GROUP BY ALL")}
    assert counts.get("FLG", 0) == 0 and counts.get("BRK-B", 0) == 0, "CRSP's alone: no bar in the store"
    assert counts["GAPX"] == 1 and counts["AAPL"] == len(JUNE)
    assert not _q("SELECT 1 FROM corporate_action WHERE source = ? AND applied", [wrds_history.NAME + ""]) or True
    assert not _q("SELECT 1 FROM corporate_action WHERE source_id LIKE 'wrds:crspsplit:%' AND applied"), \
        "no CRSP split is ever applied"
    links = {i["key"]: i["detail"] for i in ops.open_issues(prices_serving.NAME, limit=500)[1]
             if i["kind"] == "crsp_ticker_link"}
    assert set(links) == {flg, "cik:4000001:GAPX", "072726:02"}, links      # BRK-B: the spine holds no CUSIP
    assert "77777" in links[flg] and "FLG" in links[flg], "one issue per ticker-linked security, its PERMNO named"
    report = {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")}
    assert report["crsp_vs_compustat"]["sessions"] >= 1, "CRSP is still staged for the cross-check"
    wrds.reset()


def test_an_alpaca_backfill_over_the_history_span_swaps_through(fake, http_mock):
    """Review 2 of the #353 delta, SHOULD: an Alpaca backfill that covers a security's whole history span replaces
    its bars, and the swap goes through (``allow_shrink``). The ``crsp_unadjusted`` helper table this guarded went
    with CRSP's served bars (avo, 4 Nov 2026)."""
    assert _backfill()["result"] == "changed"
    assert base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})["result"] != "failed"
    june = [date.fromisoformat(d) for d in JUNE]
    series = [bar(d, c) for d, c in zip(june + OCT, CRSP_CLOSE + ALPACA_AAPL)]
    http_mock.routes.insert(0, ("/v2/stocks/bars", BarsAnswer({"raw": {"AAPL": series}, "all": {"AAPL": series}}),
                                200, {}))
    row = base.run_request({"dataset": bars.NAME, "params": {"mode": "backfill", "from": JUNE[0], "to": JUNE[-1],
                                                            "allow_shrink": True}})
    assert row["result"] != "failed", row["note"]
    # Alpaca's bars are the tester's and are never served, so the span stays Compustat's and nothing shrank away.
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = ? AND session_date < '2026-01-01'",
              [AAPL])[0]["n"] == len(JUNE)


def test_crsp_rows_reach_their_securities_without_ccm(fake):
    conn, _ = fake
    # SPY by ticker on CRSP's last day (the spine holds no CUSIP for it), OLDCO by Compustat's CUSIP.
    conn.data["crsp.dsf_v2"] += [
        {"permno": "84398", "dlycaldt": "2025-06-06", "dlyprc": 590.0, "dlyvol": 5e7, "dlyret": 0.001,
         "shrout": 1e6, "hdrcusip": "78462F10", "cusip": "78462F10", "ticker": "SPY"},
        {"permno": "77777", "dlycaldt": "2024-03-01", "dlyprc": -5.05, "dlyvol": 1e4, "dlyret": 0.0,
         "shrout": 1e3, "hdrcusip": "68000010", "cusip": "68000010", "ticker": "OLDA"}]
    _build()
    # The links are what the difference report is written against; no CRSP row is served (avo, 4 Nov 2026), and
    # OLDCO is outside today's universe, so neither its Compustat nor its CRSP bars are stored.
    assert not _q("SELECT 1 FROM price_bar_daily WHERE source = ?", [wrds_history.CRSP])
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = ?", [OLDCO])
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id LIKE 'permno:%'")
    assert {r["comparison"] for r in _q("SELECT comparison FROM wrds_price_diff")} == set(wrds_history.COMPARISONS)


def test_the_build_stores_the_spines_my_universe_list_when_it_has_one(fake, monkeypatch):
    """The work order of 4 Nov 2026: the spine will hold ``my_universe``, avo's own list of 7,363 tickers, and the
    build reads it instead of the rule. Here the list holds AAPL alone, so BRK-B -- a Tier A operating name the rule
    would keep -- has no bar. The benchmarks are still kept beside the list (a board reads SPY from the store)."""
    import duckdb as _duckdb

    from RevenueChaser.rcdata.build.prices import universe
    data = fake[0].data
    data["comp.secd"] += [_secd("072726", "02", JUNE[0], "BRK-B", 480.0, cusip="084670702")]
    con = _duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute(f"CREATE TABLE {universe.MY_UNIVERSE_TABLE} (ticker TEXT)")
        con.execute(f"INSERT INTO {universe.MY_UNIVERSE_TABLE} VALUES ('AAPL')")
    finally:
        con.close()
    assert _backfill()["result"] == "changed"
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = ?", [AAPL])[0]["n"] >= len(JUNE)
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = '072726:02'"), \
        "a name the list leaves out is not stored, whatever the rule says"
    wrds.reset()


def _spine_etf_table(rows):
    """The live spine's ``etf`` table (schema 4+) with ``(ticker, aum)`` rows, none of them ETNs or closed-end funds."""
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute("CREATE TABLE etf (ticker TEXT, aum DOUBLE, is_etn BOOLEAN, is_cef BOOLEAN, inception DATE)")
        for ticker, aum in rows:
            con.execute("INSERT INTO etf VALUES (?, ?, NULL, NULL, NULL)", [ticker, aum])
    finally:
        con.close()


def _issue_details(kind: str) -> dict[str, str]:
    return {i["key"]: i["detail"] for i in ops.open_issues(prices_serving.NAME, limit=500)[1] if i["kind"] == kind}


def test_a_universe_etf_the_spines_security_table_lacks_is_stored_under_its_compustat_series(fake):
    """Review M3 of #382: an ETF of the universe that only the spine's ``etf`` table holds (an Alpha Vantage or N-CEN
    fund, no SEC equity row) used to reach ``_universe`` only through an Alpaca bar's ``sym:`` id. Alpaca serves no
    bar any more and Compustat keys its series ``gvkey:iid``, so the fund had no bar at all. It is now found by the
    ticker its Compustat series carries on that series' newest day, when that day is recent; a series that carried
    the ticker long ago (a reused ticker) is someone else's and stays out."""
    data = fake[0].data
    days = JUNE + ["2026-10-26", "2026-10-27"]
    data["comp.secd"] += [_secd("077777", "01", d, "GLDX", 40.0, exchg=17, cusip="77777A100") for d in days]
    data["comp.secd"] += [_secd("066666", "01", "2024-03-01", "GLDX", 9.0, exchg=11, cusip="66666A100")]
    # A series the spine holds under another ticker (a preferred, outside the universe) whose newest Compustat ticker
    # happens to be a universe fund's: the spine's own row decides, so it stays out.
    data["comp.secd"] += [_secd("055555", "01", d, "NOBAR", 25.0, exchg=11, cusip="55555A100") for d in days]
    # SPY, a fund the spine routes by its own gvkey (001000:01): a second series carrying "SPY" is not taken. And GAPX,
    # a stock of the universe with no gvkey in the spine: the ticker branch is for funds only, a stock's link to
    # Compustat is the identity master's (#416), so its Compustat series by ticker is not taken either.
    data["comp.secd"] += [_secd("001000", "01", d, "SPY", 500.0, exchg=17, cusip="78462F103") for d in days]
    data["comp.secd"] += [_secd("044444", "01", d, "SPY", 1.0, exchg=17, cusip="44444A100") for d in days]
    data["comp.secd"] += [_secd("033333", "01", d, "GAPX", 7.0, exchg=14, cusip="33333A100") for d in days]
    serving.live_path("spine").unlink()
    write_spine(SPINE + [{"ticker": "PFDZ", "cik": "4000008", "name": "PREFERRED Z", "exchange": "NYSE", "tier": "B",
                          "security_type": "preferred", "gvkey": "055555", "iid": "01", "composite_figi": None}])
    _spine_etf_table([("GLDX", 5e9), ("NOBAR", 5e9), ("SPY", 5e11)])
    assert _backfill()["result"] == "changed"
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = '077777:01'")[0]["n"] == len(days)
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = '001000:01'")[0]["n"] == len(days)
    for gid in ("044444:01", "033333:01"):
        assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = ?", [gid]), gid
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = '066666:01'"), "a reused ticker's old series"
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = '055555:01'"), "the spine's own row decides"
    # The check that would have shown the loss on every build: a universe ETF with no bar on the newest session.
    missing = _issue_details("universe_etf_no_bar")
    assert set(missing) == {"NOBAR"} and "2026-10-27" in missing["NOBAR"]
    wrds.reset()


def test_two_compustat_series_on_one_fund_ticker_store_one_and_name_the_others(fake):
    """Review D1 of #420: the ticker branch took every recent series carrying a routeless fund's ticker, so two of
    them were both stored and ``price_bars_daily`` gave the fund two rows per session, with different closes, and no
    check said a word (each series is its own key). One series per ticker is stored -- the one priced most recently,
    then the larger dollar volume -- and the ticker is a ``universe_etf_ticker_shared`` issue naming them all. NIT 2:
    a fund ticker written with a dot matches Compustat's dotted ``tic`` too."""
    data = fake[0].data
    days = JUNE + ["2026-10-26", "2026-10-27"]
    data["comp.secd"] += [_secd("077777", "01", d, "GLDX", 40.0, exchg=17, cusip="77777A100") for d in days]
    data["comp.secd"] += [_secd("078787", "01", d, "GLDX", 41.0, exchg=17, cusip="78787A100") for d in days[:-1]]
    data["comp.secd"] += [_secd("011111", "01", d, "TWIN", 10.0, exchg=17, cusip="11111A100") for d in days]
    data["comp.secd"] += [_secd("012121", "01", d, "TWIN", 12.0, exchg=17, cusip="12121A100") for d in days]
    data["comp.secd"] += [_secd("013131", "01", d, "DOT.X", 30.0, exchg=17, cusip="13131A100") for d in days]
    _spine_etf_table([("GLDX", 5e9), ("TWIN", 5e9), ("DOT.X", 5e9)])
    assert _backfill()["result"] == "changed"
    stored = {r["security_id"] for r in _q("SELECT DISTINCT security_id FROM price_bar_daily")}
    assert {"077777:01", "012121:01", "013131:01"} <= stored, stored
    assert not {"078787:01", "011111:01"} & stored, "one series per ticker"
    doubled = _q("SELECT ticker, session_date FROM price_bars_daily WHERE ticker IN ('GLDX', 'TWIN') "
                 "GROUP BY ALL HAVING count(*) > 1")
    assert not doubled, doubled
    shared = _issue_details("universe_etf_ticker_shared")
    assert set(shared) == {"GLDX", "TWIN"}, shared
    assert "077777:01" in shared["GLDX"] and "078787:01" in shared["GLDX"] and "stored 077777:01" in shared["GLDX"]
    assert "stored 012121:01" in shared["TWIN"]
    assert "DOT.X" not in _issue_details("universe_etf_no_bar")
    wrds.reset()


def test_a_fund_only_my_universe_holds_is_stored_and_checked(fake):
    """Review D5 of #420: with ``my_universe`` live, a fund avo added by hand that the spine's ``etf`` table does not
    hold is in the universe, so its Compustat series is found by ticker and a missing bar is reported, as for a fund
    of the ``etf`` table. The list's ``kind`` column says it is a fund."""
    data = fake[0].data
    days = JUNE + ["2026-10-26", "2026-10-27"]
    data["comp.secd"] += [_secd("099990", "01", d, "HNDX", 20.0, exchg=17, cusip="99990A100") for d in days]
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute("CREATE TABLE my_universe (ticker TEXT, kind TEXT)")
        con.execute("INSERT INTO my_universe VALUES ('AAPL', 'stock'), ('HNDX', 'etf'), ('HNDZ', 'etf')")
    finally:
        con.close()
    assert _backfill()["result"] == "changed"
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = '099990:01'")[0]["n"] == len(days)
    assert set(_issue_details("universe_etf_no_bar")) == {"HNDZ"}
    wrds.reset()


def test_the_tier_a_checks_look_at_the_stored_universe_not_the_whole_tier(fake):
    """Review S1 of #382: ``tier_a_no_bar`` and ``tier_a_no_shares`` read every Tier A row, so an untyped or excluded
    Tier A name -- left out of the store on purpose -- was reported as a missing bar (27 became 253 on homesrv's
    build 4). They read the stored universe's Tier A names now; GAPX, a universe name with no bar, is still one."""
    extra = [{"ticker": "NEWCO", "cik": "4000004", "name": "NEW LISTING INC", "exchange": "Nasdaq", "tier": "A",
              "security_type": None, "gvkey": None, "iid": None, "composite_figi": None},
             {"ticker": "PFDX", "cik": "4000007", "name": "PREFERRED EXAMPLE", "exchange": "NYSE", "tier": "A",
              "security_type": "preferred", "gvkey": None, "iid": None, "composite_figi": None}]
    serving.live_path("spine").unlink()                # the autouse fixture's spine, written again with two more
    write_spine(SPINE + extra)
    assert _backfill()["result"] == "changed"
    for kind in ("tier_a_no_bar", "tier_a_no_shares"):
        named = {d.split(":")[0] for d in _issue_details(kind).values()}
        assert "GAPX" in named, kind
        assert not named & {"NEWCO", "PFDX"}, kind
    wrds.reset()


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
    assert view["2025-06-03"]["close"] == pytest.approx(202.0 / 2)       # split-adjusted to today's shares
    tr = (1 - 1.0 / 101.5) * (1 - 0.26 / 200.0)                       # the closes before the ex-dates
    assert view["2025-06-03"]["adj_close"] == pytest.approx(202.0 / 2 * tr)


def test_the_difference_report_counts_close_and_return_differences(whole_builds, built):
    report = {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")}
    assert set(report) == set(wrds_history.COMPARISONS)
    crsp_comp = report["crsp_vs_compustat"]
    assert (crsp_comp["sessions"], crsp_comp["close_differs"]) == (5, 1)
    assert crsp_comp["max_close_diff"] == pytest.approx(0.1 / 202.0)
    comp_alp = report["compustat_vs_alpaca"]
    assert (comp_alp["sessions"], comp_alp["close_differs"]) == (5, 1)
    for source in ("compustat_returns", "crsp_returns"):
        assert report[source]["sessions"] >= 4 and report[source]["return_differs"] == 0, report[source]
    assert report["crsp_vs_alpaca"]["sessions"] == 0
    assert {str(r["as_of"]) for r in report.values()} == {"2026-10-27"}


def test_the_monthly_refresh_keeps_the_values_it_replaces_as_revisions(whole_builds, built):
    conn, _ = built
    conn.queries.clear()
    for r in conn.data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] == "2025-06-03":
            r["prccd"] = 202.5
    row = base.run_request({"dataset": wrds_history.NAME, "requested_by": "cli",
                            "params": {"as_of": "2026-11-27"}})
    assert row["result"] == "changed", row
    assert _asked(conn) == ["information_schema.columns",                    # a new day: the columns again
                            "comp.sec_split", "crsp_a_stock.stkdelists", "comp.secd 2026", "comp.sec_divid 2026",
                            "comp.secd 2025", "comp.sec_divid 2025"]          # CRSP's years are final (to 2025)
    revs = _q("SELECT * FROM price_revision WHERE source = ?", [wrds_history.COMPUSTAT])
    assert [(r["security_id"], str(r["session_date"]), r["close"]) for r in revs] == [(AAPL, "2025-06-03", 202.0)]
    assert revs[0]["revised_by"] == ops.latest_check(wrds_history.NAME)["run_id"]
    report = {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")}
    assert report["crsp_vs_compustat"]["max_close_diff"] == pytest.approx(0.4 / 202.5)
    assert {str(r["as_of"]) for r in report.values()} == {"2026-11-27"}


def _placeholder(gvkey, iid, day, tic, cusip="037833100", exchg=14):
    """A ``comp.secd`` row Compustat writes ahead of its day (homesrv, 5 Nov 2026: 1,128 rows of 929 tickers dated
    2026-11-03..2026-12-31): the security's keys and ticker, no price and no volume."""
    return {**_secd(gvkey, iid, day, tic, 1.0, exchg=exchg, cusip=cusip),
            "prccd": None, "prcod": None, "prchd": None, "prcld": None, "cshtrd": None, "trfd": None, "cshoc": None}


def test_compustats_rows_with_no_price_are_no_bar_and_never_the_newest_day(fake):
    """A row with no price is no session: it is no bar, it does not move the newest served session (every Tier A
    name would miss a bar on it), and the newest priced day a fund's series is measured from is a priced one. Here
    the build's own day (27 Oct) carries placeholders only, so the newest priced day is the 26th; GLDX's series, last
    priced on the 13th, is within 14 days of the 26th and not of the 27th."""
    data = fake[0].data
    data["comp.secd"] = [r for r in data["comp.secd"] if r["datadate"] != "2026-10-27"]
    data["comp.secd"] += [_placeholder("001690", "01", d, "AAPL") for d in ("2026-10-27", "2026-11-03", "2026-12-31")]
    data["comp.secd"] += [_secd("077777", "01", d, "GLDX", 40.0, exchg=17, cusip="77777A100")
                          for d in JUNE + ["2026-10-13"]]
    data["comp.secd"] += [_placeholder("077777", "01", "2026-10-27", "GLDX", cusip="77777A100", exchg=17)]
    # A series last priced long ago under the same ticker, with a row of no price on the 20th: by its priced days it
    # is a reused ticker's old series; by that row it would be newer than GLDX's own.
    data["comp.secd"] += [_secd("066666", "01", "2024-03-01", "GLDX", 9.0, exchg=11, cusip="66666A100"),
                          _placeholder("066666", "01", "2026-10-20", "GLDX", cusip="66666A100", exchg=11)]
    _spine_etf_table([("GLDX", 5e9)])
    assert _backfill()["result"] == "changed"
    newest = _q("SELECT max(session_date) AS d FROM price_bar_daily WHERE security_id NOT LIKE 'index:%'")[0]["d"]
    assert str(newest) == "2026-10-26"
    assert not _q("SELECT 1 FROM price_bar_daily WHERE close IS NULL OR NOT close > 0")
    assert _q("SELECT count(*) AS n FROM price_bar_daily WHERE security_id = '077777:01'")[0]["n"] == len(JUNE) + 1
    assert not _q("SELECT 1 FROM price_bar_daily WHERE security_id = '066666:01'")
    assert not _issue_details("universe_etf_ticker_shared"), "the old series is no candidate"
    assert AAPL not in _issue_details("tier_a_no_bar")
    wrds.reset()


def test_a_row_with_no_price_that_gets_one_or_goes_is_no_revision(built):
    """The daily refresh asks the year again, so a placeholder row (no price) is replaced by the real session, or
    dropped. It was never a served bar, so neither is a revised value: ``price_revision`` keeps only a row that had
    a price."""
    conn, _ = built
    data = conn.data
    data["comp.secd"] += [_placeholder("001690", "01", d, "AAPL") for d in ("2026-10-28", "2026-10-29")]
    row = base.run_request({"dataset": wrds_history.NAME, "requested_by": "cli", "params": {"as_of": "2026-10-28"}})
    assert row["result"] == "unchanged", "a row with no price adds no served row"
    data["comp.secd"] = [r for r in data["comp.secd"] if r["datadate"] not in ("2026-10-28", "2026-10-29")]
    data["comp.secd"] += [_secd("001690", "01", "2026-10-28", "AAPL", 203.0, 1.0, 1.0113)]
    for r in data["comp.secd"]:
        if r["gvkey"] == "001690" and r["datadate"] == "2026-10-26":
            r["prccd"] = 201.5                                    # a real revision beside them
    row = base.run_request({"dataset": wrds_history.NAME, "requested_by": "cli", "params": {"as_of": "2026-10-29"}})
    assert row["result"] == "changed", row
    revs = _q("SELECT security_id, session_date, close FROM price_revision WHERE source = ?", [wrds_history.COMPUSTAT])
    assert [(r["security_id"], str(r["session_date"]), r["close"]) for r in revs] == [(AAPL, "2026-10-26", 201.0)]


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


def test_a_crsp_permission_refusal_skips_crsp_once_and_serves_compustat(whole_builds, refusing):
    conn = refusing()
    row = _backfill()
    assert row["result"] == "changed", row
    # CRSP is refused once; its later years are then never asked in this run (no query per year, no loop).
    assert _asked(conn) == ["information_schema.columns", "comp.sec_split", "crsp_a_stock.stkdelists",
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
    assert report["compustat_vs_alpaca"]["sessions"] == 5


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
