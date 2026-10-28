"""``wrds/prices``: the history backfill against a fake WRDS worker (never WRDS): one query per table and year from
1984 (CRSP to 2025), an existing partition skipped, the monthly refresh keeping what it replaces, the history bars
and actions in the served tables, Compustat's ``ajexdi``/``trfd`` return within 1 bp, and the difference report.

The fixture: AAPL (``001690:01``, CRSP permno 14593) does a 2-for-1 split ex 4 Jun 2025 and goes ex a 1.00
dividend on the 5th (Compustat's ``trfd`` reinvests it at that close); CRSP's close on the 3rd is 202.1 against
Compustat's 202.0; Compustat's 27 Oct 2026 close is 202.5 against Alpaca's 202.0; OLDCO is a delisted Compustat
name the spine does not hold; a CAD row is never served. **Column names are Compustat's and CRSP's as documented,
not read from WRDS** (no cloud thread logs in)."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

import duckdb
import pytest

from RevenueChaser.core import calendar
from RevenueChaser.rcdata import caps, ops, serving
from RevenueChaser.rcdata.build import base
from RevenueChaser.rcdata.build.prices import actions, bars, wrds_history
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


def _secd(gvkey, iid, day, tic, close, ajexdi=1.0, trfd=1.0, cshoc=15e9, curcdd="USD", exchg=14):
    return {"gvkey": gvkey, "iid": iid, "datadate": day, "tic": tic, "cusip": "037833100", "curcdd": curcdd,
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
                    "shrout": 7_500_000 if day < "2025-06-04" else 15_000_000})
        prev = close
    return out


def _data():
    secd = [_secd("001690", "01", d, "AAPL", c, a, t, 7.5e9 if a == 2 else 15e9)
            for d, c, a, t in zip(JUNE, COMP_CLOSE, AJEXDI, TRFD)]
    secd += [_secd("001690", "01", "2026-10-26", "AAPL", 201.0, 1.0, 1.0113),
             _secd("001690", "01", "2026-10-27", "AAPL", 202.5, 1.0, 1.0113),
             _secd("099999", "01", "2024-03-01", "OLD.A", 5.0, exchg=11),
             _secd("099999", "01", "2024-03-04", "OLD.A", 5.1, exchg=11),
             _secd("088888", "01", "2025-06-02", "MAPLE", 30.0, curcdd="CAD", exchg=14)]
    return {
        "comp.secd": secd,
        "crsp.dsf_v2": _crsp_rows(),
        "comp.sec_split": [{"gvkey": "001690", "iid": "01", "datadate": "2025-06-04", "split": 2.0}],
        "comp.sec_divid": [{"gvkey": "001690", "iid": "01", "datadate": "2025-06-05", "divd": 1.0, "curcddv": "USD"},
                           {"gvkey": "001690", "iid": "01", "datadate": "2026-10-23", "divd": 0.26,
                            "curcddv": "USD"}],
        "crsp.ccmxpf_lnkhist": [{"gvkey": "001690", "iid": "01", "permno": "14593", "linktype": "LC",
                                 "linkprim": "P", "linkdt": "1980-12-12", "linkenddt": None}],
    }


DATE_OF = {"comp.secd": "datadate", "crsp.dsf_v2": "dlycaldt", "comp.sec_divid": "datadate"}


class FakeWrds:
    """The worker's side: answers each table from ``data`` (a year's query its year's rows) as gzip JSON lines of
    text values, as ``wrds_worker.py`` writes them, and records every query."""

    def __init__(self, data):
        self.data, self.queries = data, []

    def query(self, sql: str, out) -> int:
        self.queries.append(sql)
        table = re.search(r"FROM (\w+\.\w+)", sql).group(1)
        rows = self.data.get(table, [])
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

def test_the_history_is_asked_per_year_from_1984_on_the_us_exchanges_crsp_to_2025(monkeypatch):
    monkeypatch.setattr(wrds_history, "START_YEAR", 1984)
    as_of = date(2026, 10, 27)
    assert wrds_history.years("secd", as_of) == list(range(1984, 2027))
    assert wrds_history.years("dsf", as_of) == list(range(1984, 2026))
    assert wrds_history.years("sec_divid", as_of)[0] == 1984
    sql = wrds_history.year_sql("secd", 1984)
    assert "FROM comp.secd" in sql and "BETWEEN '1984-01-01' AND '1984-12-31'" in sql
    assert "exchg IN (11, 12, 14)" in sql and "ajexdi" in sql and "trfd" in sql and "cshoc" in sql
    assert "FROM crsp.dsf_v2" in wrds_history.year_sql("dsf", 2025)


def test_a_backfill_asks_each_table_and_year_once_and_skips_a_partition_that_exists(fake):
    conn, logins = fake
    row = _backfill()
    assert row["result"] == "changed", row
    assert _asked(conn) == ["comp.sec_split", "crsp.ccmxpf_lnkhist",              # the snapshots first,
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
    assert len(conn.queries) == 4 and "day cap" in row["note"]                # the snapshots and 2026
    served = _q("SELECT security_id, count(*) AS n FROM price_bar_daily GROUP BY ALL")
    assert {(r["security_id"], r["n"]) for r in served} == {(AAPL, 2)}


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

@pytest.fixture
def built(fake):
    assert _backfill()["result"] == "changed"
    assert base.run_request({"dataset": actions.NAME, "params": {"as_of": "2026-10-27"}})["result"] != "failed"
    row = base.run_request({"dataset": bars.NAME, "params": {"as_of": "2026-10-27"}})
    assert row["result"] == "changed", row
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
    assert _asked(conn) == ["comp.sec_split", "crsp.ccmxpf_lnkhist", "comp.secd 2026", "comp.sec_divid 2026",
                            "comp.secd 2025", "crsp.dsf_v2 2025", "comp.sec_divid 2025", "crsp.dsf_v2 2024"]
    revs = _q("SELECT * FROM price_revision WHERE source = ?", [wrds_history.COMPUSTAT])
    assert [(r["security_id"], str(r["session_date"]), r["close"]) for r in revs] == [(AAPL, "2025-06-03", 202.0)]
    assert revs[0]["revised_by"] == ops.latest_check(wrds_history.NAME)["run_id"]
    report = {r["comparison"]: r for r in _q("SELECT * FROM wrds_price_diff")}
    assert report["crsp_vs_compustat"]["max_close_diff"] == pytest.approx(0.4 / 202.5)
    assert {str(r["as_of"]) for r in report.values()} == {"2026-11-27"}
