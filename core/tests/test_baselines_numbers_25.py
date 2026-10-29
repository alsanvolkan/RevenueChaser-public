"""``core.baselines`` (#25 HR-1) end to end: a run fills ``forecast_baselines`` from the store alone.

Kept apart from ``core/tests/test_baselines.py`` on purpose — a second, differently shaped fixture. That file's bars
put the MFE on the first window session with a pinned amplitude; here the amplitude and the MFE are both parameters
of the same builder, so a rule that only holds for one arrangement of bars fails in one of the two.

``forecast_baselines`` is derived and keyed on ``(forecast_date, horizon_days, basis)``; these tests check the number
a hit rate is compared against, not the writer's bookkeeping. Since PR 10 (G4) the writer runs on the builder's build
file of the ledger kind (an in-memory ``con`` here) and reads the prices store; the day's universe is what traded.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from RevenueChaser.core import baselines, calendar as cal
from RevenueChaser.core.tests import ledger_build

D = date(2026, 9, 1)                      # the freeze date (a Tuesday)
LAST_BAR = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)     # 2026-10-09 is complete
TABLE = "forecast_baselines"


def _sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if cal.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _series(ticker, *, mfe, mae=-0.02, amp=0.002, start=None, last=LAST_BAR):
    """Bars whose open is 10 every session, whose close oscillates by ``amp`` (the trailing-vol signal), and whose
    high/low after ``D`` are exactly ``10 * (1 + mfe)`` / ``10 * (1 + mae)`` — so the MFE and MAE an outcome computes
    are the arguments, whatever the amplitude."""
    rows = []
    for i, d in enumerate(_sessions(start or D - timedelta(days=40), last)):
        close = 10.0 * (1 + (amp if i % 2 else -amp))
        high = max(10.0 * (1 + (mfe if d > D else amp)), close)
        low = min(10.0 * (1 + (mae if d > D else -amp)), close)
        rows.append({"ticker": ticker, "session_date": d, "open": 10.0, "high": high, "low": low, "close": close,
                     "adj_close": None, "volume": 1000.0, "split_ratio": None, "adj_source": None,
                     "source": "yfinance", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0})
    return rows


_STORE: dict = {}


@pytest.fixture(autouse=True)
def store():
    con = ledger_build.connect()
    _STORE["con"] = con
    yield con
    con.close()
    _STORE.clear()


def _q(sql, params=None):
    return ledger_build.query(_STORE["con"], sql, params)


def _run(**kw):
    return baselines.run(_STORE["con"], **kw)


def _seed(series):
    """The bars (whatever has a bar on ``D`` is that day's universe) and one pick on ``D`` — the ledger is what names
    a date to score."""
    ledger_build.Bars().seed([r for s in series for r in s])
    ledger_build.freeze(_STORE["con"], [series[0][0]["ticker"]], day=D)


def _row(basis="universe", horizon=30):
    got = _q(f"SELECT * FROM {TABLE} WHERE forecast_date = ? AND basis = ? AND horizon_days = ?", [D, basis, horizon])
    assert len(got) == 1, f"{basis}/{horizon}: {got}"
    return got[0]


def test_a_baselines_run_populates_forecast_baselines(rawstore_tmp_db):
    """The population test ``core/tests/test_db_persistence.py`` maps this table to: one run, rows in the store."""
    _seed([_series(f"N{i}", mfe=0.03 + 0.01 * i, amp=0.0005 * (i + 1)) for i in range(20)])
    out = _run(now=NOW, run_id="r1")
    assert out["written"] == ledger_build.count(_STORE["con"], TABLE) > 0
    assert [r["horizon_days"] for r in _q(f"SELECT horizon_days FROM {TABLE} WHERE basis = ? ORDER BY horizon_days",
                                          ["universe"])] == sorted(baselines.HORIZONS)


def test_the_universe_row_is_the_mean_of_the_whole_universe_not_of_the_picks(rawstore_tmp_db):
    """Four names in that day's universe, one pick: the baseline is the universe's four outcomes, and the ge20/ge10/ge5
    counts are cumulative and inclusive at the boundary."""
    _seed([_series("A", mfe=0.25, mae=-0.02), _series("B", mfe=0.12, mae=-0.30),
           _series("C", mfe=0.06, mae=-0.08), _series("D", mfe=0.01, mae=-0.04)])
    _run(now=NOW, run_id="r1")
    row = _row()
    assert row["n"] == 4
    assert row["mfe_mean"] == pytest.approx((0.25 + 0.12 + 0.06 + 0.01) / 4)
    assert row["mae_mean"] == pytest.approx((-0.02 - 0.30 - 0.08 - 0.04) / 4)
    assert (row["ge20"], row["ge10"], row["ge5"]) == (1, 2, 3)     # counts, not shares
    assert (row["vol_lo"], row["vol_hi"]) == (None, None)          # the universe row is not a decile


def test_the_deciles_partition_the_members_they_can_place(rawstore_tmp_db):
    """Every member with a trailing vol lands in exactly one decile: the counts add up to the universe's, and the
    deciles' stored ranges tile the volatility axis without a hole."""
    _seed([_series(f"N{i}", mfe=0.03 + 0.01 * i, amp=0.0005 * (i + 1)) for i in range(20)])
    _run(now=NOW, run_id="r1")
    rows = [r for r in _q(f"SELECT * FROM {TABLE} WHERE forecast_date = ? AND horizon_days = 30 "
                          "AND basis <> ? ORDER BY vol_lo NULLS FIRST, basis", [D, "universe"])
            if r["n"]]
    assert len(rows) >= 2
    assert sum(r["n"] for r in rows) == _row()["n"] == 20
    assert sum(r["ge10"] for r in rows) == _row()["ge10"]       # a count is additive over a partition
    assert rows[0]["vol_lo"] is None and rows[-1]["vol_hi"] is None
    for lower, upper in zip(rows, rows[1:]):
        assert lower["vol_hi"] == pytest.approx(upper["vol_lo"])   # no gap, no overlap: lo inclusive, hi exclusive


def test_a_run_writes_the_baselines_table_and_nothing_else(rawstore_tmp_db, monkeypatch):
    """The baseline is derived from the ledger and the bars: it never rewrites either."""
    _seed([_series("A", mfe=0.10), _series("B", mfe=0.20)])
    from RevenueChaser.core import outcomes
    written = []
    real = outcomes.upsert
    monkeypatch.setattr(outcomes, "upsert", lambda con, table, *a: written.append(table) or real(con, table, *a))
    _run(now=NOW, run_id="r1")
    assert set(written) == {TABLE}
    con = _STORE["con"]
    assert ledger_build.count(con, "forecast_outcomes") == 0 and ledger_build.count(con, "forecast_ledger") == 1
