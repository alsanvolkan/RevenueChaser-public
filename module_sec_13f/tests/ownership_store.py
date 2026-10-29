"""A small ownership store and spine for the 13F board's tests (G9): serving files written straight into a temp
``MARKET_DIR``, in the served tables' shapes (``rcdata/build/ownership/serve.py``, ``finra.py``)."""
from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from RevenueChaser.core import calendar

Q1, Q2, Q3 = date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30)
TICKER_COLS = ["ticker", "period", "shares", "value", "holders", "top10_pct", "hhi", "new_holders", "exited_holders",
               "exited_confidential", "exited_no_filing", "buy_flow", "sell_flow", "breadth", "breadth_chg",
               "prev_period", "filers", "cusips", "filing_deadline", "filed_through", "source"]


@pytest.fixture
def market(tmp_path, monkeypatch):
    """The serving files in this test's ``tmp_path``, and the board page too: under ``pytest -n auto`` every worker
    shares one ``.cache``, so a shared ``sec_13f_latest.html`` would be written and removed by two tests at once."""
    from RevenueChaser.app import runners
    from RevenueChaser.core import config
    from RevenueChaser.rcdata import ops
    monkeypatch.setattr(runners, "SEC_13F_LATEST", tmp_path / "sec_13f_latest.html")
    monkeypatch.setattr(config, "MARKET_DIR", tmp_path / "market")
    monkeypatch.setattr(config, "MARKET_RAW_DIR", tmp_path / "market_raw")
    monkeypatch.setattr(ops, "read_log", ops.ReadLog(process="test"))
    (tmp_path / "market").mkdir()
    return tmp_path / "market"


def _row(ticker, period, shares, deadline, **kw):
    base = {"ticker": ticker, "period": period, "shares": shares, "value": shares * 10.0, "holders": 3,
            "top10_pct": 80.0, "hhi": 3000.0, "new_holders": 1, "exited_holders": 0, "exited_confidential": 0,
            "exited_no_filing": 0, "buy_flow": 5.0, "sell_flow": 1.0, "breadth": 1.5, "breadth_chg": 0.1,
            "prev_period": None, "filers": 200, "cusips": 1, "filing_deadline": deadline,
            "filed_through": deadline - timedelta(days=1), "source": "sec/13f"}
    base.update(kw)
    return base


def _write_ownership(market, rows, periods=(), shorts=()):
    con = duckdb.connect(str(market / "ownership.duckdb"))
    try:
        con.execute("CREATE TABLE sec13f_ticker_period (ticker TEXT, period DATE, shares DOUBLE, value DOUBLE, "
                    "holders INTEGER, top10_pct DOUBLE, hhi DOUBLE, new_holders INTEGER, exited_holders INTEGER, "
                    "exited_confidential INTEGER, exited_no_filing INTEGER, buy_flow DOUBLE, sell_flow DOUBLE, "
                    "breadth DOUBLE, breadth_chg DOUBLE, prev_period DATE, filers INTEGER, cusips INTEGER, "
                    "filing_deadline DATE, filed_through DATE, source TEXT, fetched_at DOUBLE)")
        for r in rows:
            con.execute(f"INSERT INTO sec13f_ticker_period VALUES ({', '.join('?' * (len(TICKER_COLS) + 1))})",
                        [r[c] for c in TICKER_COLS] + [1_760_000_000.0])
        con.execute("CREATE TABLE sec13f_periods (period DATE, filers INTEGER, filings_active INTEGER, "
                    "value_total DOUBLE, value_mapped DOUBLE, coverage DOUBLE, unmapped_cusips INTEGER, "
                    "refused_value DOUBLE, rescaled_filers INTEGER, filing_deadline DATE, filed_through DATE)")
        for p in periods:
            con.execute("INSERT INTO sec13f_periods VALUES (?, 200, 210, ?, ?, ?, ?, 0, 0, ?, ?)",
                        [p["period"], p["value_total"], p["value_mapped"], p["value_mapped"] / p["value_total"],
                         p["unmapped"], p["deadline"], p["deadline"] - timedelta(days=1)])
        con.execute("CREATE TABLE finra_short_interest (settlement_date DATE, symbol TEXT, ticker TEXT, "
                    "current_short BIGINT, days_to_cover DOUBLE, avg_daily_volume BIGINT, source TEXT)")
        con.executemany("INSERT INTO finra_short_interest VALUES (?, ?, ?, ?, ?, 1000, 'finra')",
                        [[d, t, t, n, dtc] for t, d, n, dtc in shorts])
    finally:
        con.close()


def _write_spine(market, names):
    con = duckdb.connect(str(market / "spine.duckdb"))
    try:
        con.execute("CREATE TABLE security (ticker TEXT, name TEXT, sector TEXT, tier TEXT)")
        con.executemany("INSERT INTO security VALUES (?, ?, ?, 'A')", [[t, n, s] for t, (n, s) in names.items()])
    finally:
        con.close()


def _past(days=60):
    return calendar.today_ny() - timedelta(days=days)


def _future(days=20):
    return calendar.today_ny() + timedelta(days=days)


def _store(market):
    """AAA grows Q1 -> Q2 (complete) and has a smaller, still-filing Q3; BBB holds only Q1 and Q2."""
    rows = [_row("AAA", Q1, 100.0, _past(200)), _row("AAA", Q2, 200.0, _past(100), prev_period=Q1,
                                                         exited_confidential=2, exited_no_filing=1),
            _row("AAA", Q3, 50.0, _future(), prev_period=Q2),
            _row("BBB", Q1, 300.0, _past(200)), _row("BBB", Q2, 150.0, _past(100), prev_period=Q1)]
    periods = [{"period": Q1, "value_total": 4e12, "value_mapped": 3.9e12, "unmapped": 120, "deadline": _past(200)},
               {"period": Q2, "value_total": 5e12, "value_mapped": 4.95e12, "unmapped": 80, "deadline": _past(100)},
               {"period": Q3, "value_total": 1e12, "value_mapped": 0.9e12, "unmapped": 40, "deadline": _future()}]
    shorts = [("AAA", _past(30), 1_000, 1.0), ("AAA", _past(15), 2_500_000, 2.25)]
    _write_ownership(market, rows, periods, shorts)
    _write_spine(market, {"AAA": ("Alpha Inc", "Technology"), "BBB": ("Bravo Co", "Energy")})
