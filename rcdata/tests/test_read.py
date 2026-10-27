"""``rcdata/read.py``: rows with their provenance and the dataset status; every read is counted."""
from __future__ import annotations

from datetime import date

from RevenueChaser.rcdata import ops, read, serving


def _serve():
    with serving.build("market") as b:
        b.con.execute("""CREATE TABLE market_series (series_id TEXT, obs_date DATE, source TEXT, value DOUBLE,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, frequency TEXT, session_date DATE,
            source_as_of DATE, fetched_at DOUBLE, run_id TEXT)""")
        b.con.execute("""INSERT INTO market_series VALUES
            ('DGS10', '2026-10-23', 'fred', 4.1, NULL, NULL, NULL, NULL, 'daily', '2026-10-23', '2026-10-26', 1.0, 'r1'),
            ('DGS10', '2026-10-26', 'fred', 4.2, NULL, NULL, NULL, NULL, 'daily', '2026-10-26', '2026-10-26', 1.0, 'r1'),
            ('VIX', '2026-10-26', 'cboe', 17.0, 16.0, 18.0, 15.5, 17.0, 'daily', '2026-10-26', '2026-10-26', 1.0, 'r1')""")
        b.con.execute("""CREATE TABLE spx_gamma_expiry_strike (session_date DATE, expiry DATE, strike DOUBLE,
            dte INTEGER, gamma_per_one_percent_move_oi DOUBLE, snapshot_time TEXT, source TEXT, source_as_of DATE,
            fetched_at DOUBLE, run_id TEXT, observed BOOLEAN)""")
        b.con.execute("""INSERT INTO spx_gamma_expiry_strike VALUES
            ('2026-10-26', '2026-10-26', 6800, 0, 1.5, '16:15', 'uw', '2026-10-26', 2.0, 'r2', true),
            ('2026-10-26', '2026-10-27', 6800, 1, 2.5, '16:15', 'uw', '2026-10-26', 2.0, 'r2', true),
            ('2026-10-26', '2026-10-30', 6800, 4, 9.9, '16:15', 'uw', '2026-10-26', 2.0, 'r2', true)""")
        b.swap({"market_series": 3, "spx_gamma_expiry_strike": 3})


def test_market_series_returns_rows_with_provenance_and_status():
    _serve()
    ops.record_check("market_series", "changed", run_id="r1")
    rows = read.market_series(["DGS10"], start=date(2026, 10, 24))
    assert len(rows) == 1
    row = rows[0]
    assert row["series_id"] == "DGS10" and row["value"] == 4.2 and row["obs_date"] == date(2026, 10, 26)
    for col in ("session_date", "source", "fetched_at", "source_as_of", "run_id"):
        assert col in row
    assert row["status"] == "changed"
    assert {r["series_id"] for r in read.market_series(["DGS10", "VIX"])} == {"DGS10", "VIX"}
    assert read.market_series([]) == []


def test_spx_expiry_strike_filters_by_dte_and_says_never_checked():
    _serve()
    rows = read.spx_expiry_strike(date(2026, 10, 26))
    assert sorted(r["dte"] for r in rows) == [0, 1]
    assert all(r["status"] == read.NEVER_CHECKED for r in rows)
    assert rows[0]["observed"] is True and rows[0]["source"] == "uw"
    assert len(read.spx_expiry_strike("2026-10-26", max_dte=10)) == 3


def test_no_serving_file_yet_is_empty_not_an_error():
    assert read.market_series(["DGS10"]) == []
    assert read.dataset_status("market_series") == read.NEVER_CHECKED


def test_every_read_bumps_the_read_log(monkeypatch):
    _serve()
    seen = []
    monkeypatch.setattr(ops.read_log, "bump", seen.append)
    read.market_series(["VIX"])
    read.spx_expiry_strike(date(2026, 10, 26))
    assert seen == ["market_series", "spx_gamma_expiry_strike"]


def test_as_frame_keeps_the_columns():
    _serve()
    df = read.as_frame(read.market_series(["VIX"]))
    assert list(df["close"]) == [17.0] and "status" in df.columns
