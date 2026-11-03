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
