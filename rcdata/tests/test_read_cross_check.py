"""The two second-source reports a cross-check can read: the WRDS price comparison and the stored revisions.

Karar 32-B (avo, 29 Oct 2026): a real second source is looked for in two fields only, prices and interest rates.
In the code those two reports are the prices store's: ``wrds_price_diff``, one row per comparison of our closes
and returns against WRDS's, and ``price_revision``, every stored bar a later run replaced. Neither had a reader in
``rcdata/read.py``, so a board or the Settings screen could not see them without writing SQL of its own, which the
API's first rule forbids.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.rcdata import ops, read, serving
from RevenueChaser.rcdata.api import catalog
from RevenueChaser.rcdata.build.prices import serving as prices_serving


@pytest.fixture(autouse=True)
def log(monkeypatch):
    monkeypatch.setattr(ops, "read_log", ops.ReadLog(process="test"))


def _prices(diff=(), revisions=()):
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        for table in ("wrds_price_diff", "price_revision"):
            cols = ", ".join(f'"{c}" {t}' for c, t in prices_serving.SCHEMAS[table])
            con.execute(f"CREATE TABLE {table} ({cols})")
        for row in diff:
            names = list(row)
            con.execute(f"INSERT INTO wrds_price_diff ({', '.join(names)}) "
                        f"VALUES ({', '.join('?' * len(names))})", [row[c] for c in names])
        for row in revisions:
            names = list(row)
            con.execute(f"INSERT INTO price_revision ({', '.join(names)}) "
                        f"VALUES ({', '.join('?' * len(names))})", [row[c] for c in names])
    finally:
        con.close()


# ── the WRDS comparison ──

def test_the_comparison_rows_come_back_newest_first():
    _prices(diff=[{"comparison": "crsp_vs_alpaca", "as_of": date(2026, 9, 30), "sessions": 10, "close_differs": 1},
                  {"comparison": "crsp_vs_alpaca", "as_of": date(2026, 10, 31), "sessions": 20,
                   "close_differs": 0}])
    rows = read.price_source_diff()
    assert [r["as_of"] for r in rows] == [date(2026, 10, 31), date(2026, 9, 30)]
    assert rows[0]["sessions"] == 20


def test_one_comparison_can_be_asked_for_on_its_own():
    _prices(diff=[{"comparison": "crsp_vs_alpaca", "as_of": date(2026, 10, 31)},
                  {"comparison": "compustat_returns", "as_of": date(2026, 10, 31)}])
    assert [r["comparison"] for r in read.price_source_diff(comparison="compustat_returns")] == ["compustat_returns"]


def test_no_prices_file_is_no_rows_and_not_an_error():
    assert read.price_source_diff() == []
    assert read.price_revisions() == []


def test_the_comparison_read_is_counted_under_its_table(monkeypatch):
    seen = []
    monkeypatch.setattr(ops.read_log, "bump", lambda name: seen.append(name))
    _prices(diff=[{"comparison": "crsp_vs_alpaca", "as_of": date(2026, 10, 31)}])
    read.price_source_diff()
    assert seen == ["wrds_price_diff"]


# ── the stored revisions ──

def test_a_revision_is_read_by_security_and_session():
    _prices(revisions=[{"security_id": "001690:01", "session_date": date(2026, 10, 27), "close": 202.0,
                        "source": "alpaca/bars"},
                       {"security_id": "999:01", "session_date": date(2026, 10, 27), "close": 1.0,
                        "source": "wrds/prices"}])
    rows = read.price_revisions(["001690:01"])
    assert [r["security_id"] for r in rows] == ["001690:01"]
    assert rows[0]["close"] == pytest.approx(202.0)


def test_every_revision_is_read_when_no_security_is_named():
    _prices(revisions=[{"security_id": "a", "session_date": date(2026, 10, 27)},
                       {"security_id": "b", "session_date": date(2026, 10, 28)}])
    assert {r["security_id"] for r in read.price_revisions()} == {"a", "b"}


def test_a_revision_can_be_bounded_by_session():
    _prices(revisions=[{"security_id": "a", "session_date": date(2026, 10, 20)},
                       {"security_id": "a", "session_date": date(2026, 10, 28)}])
    rows = read.price_revisions(["a"], start=date(2026, 10, 25))
    assert [r["session_date"] for r in rows] == [date(2026, 10, 28)]


def test_asking_for_no_security_at_all_reads_nothing(monkeypatch):
    """``price_revisions([])`` is "no name asked for", which is no rows - not "every name"."""
    _prices(revisions=[{"security_id": "a", "session_date": date(2026, 10, 27)}])
    monkeypatch.setattr(read, "_rows", lambda *a, **k: pytest.fail("the store was read for an empty ask"))
    assert read.price_revisions([]) == []


def test_a_security_that_was_never_revised_is_absent_rather_than_an_empty_row():
    _prices(revisions=[{"security_id": "a", "session_date": date(2026, 10, 27)}])
    assert read.price_revisions(["b"]) == []


# ── both tables are needs of the catalog, so a board asks by name ──

@pytest.mark.parametrize("need,table", [("prices.wrds_diff", "wrds_price_diff"),
                                        ("prices.revision", "price_revision")])
def test_each_report_is_a_need_pointing_at_its_own_reader(need, table):
    n = catalog.NEEDS[need]
    assert n.table == table and n.kind == "prices"
    assert callable(n.reader)
    assert n.max_lag_unit in catalog.UNITS


def test_the_comparison_is_counted_in_calendar_days_because_it_is_a_monthly_run():
    n = catalog.NEEDS["prices.wrds_diff"]
    assert n.max_lag_unit == catalog.DAYS and n.max_lag >= 28, "a monthly report is not stale after a session"


def test_a_report_table_a_built_file_does_not_have_is_no_rows_and_not_a_crash():
    """A prices file built before these report tables existed has neither. On a page render that must read as "no
    rows yet", not as a 500: the Settings screen reads the comparison on every render."""
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE price_bar_daily (security_id TEXT)")
    finally:
        con.close()
    assert read.price_source_diff() == []
    assert read.price_revisions(["a"]) == []


def test_a_table_that_is_simply_misspelt_still_raises():
    """The guard is for one named report, not for every typo: the catalog's column guard depends on a wrong name
    being loud."""
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE price_bar_daily (security_id TEXT)")
    finally:
        con.close()
    with pytest.raises(Exception, match="does not exist"):
        read._rows("price_bar_daily", "SELECT * FROM price_bar_dialy", [])
