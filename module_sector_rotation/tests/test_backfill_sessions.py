"""Rotation backfill counts sessions and weeks, not rows (plan MB4 / review X2; D01 since M-ROT-2, #374).

UW's multi-session daily rows (pre-market, regular, after-hours) used to reach the backfill; it now reads D01, which
holds one regular-session bar per (ticker, session), and resamples it to ISO weeks.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.module_sector_rotation import engine, store
from RevenueChaser.module_sector_rotation.tests.bars_seed import ny, pin_clock, seed, sessions


def test_weekly_closes_count_weeks_not_sessions():
    days = sessions(date(2026, 1, 5), date(2026, 3, 13))           # ten whole weeks
    points = [(d, 100.0 + i) for i, d in enumerate(days)]
    weeks = engine.weekly_closes(points, date(2027, 1, 1))
    assert len(weeks) == 10
    fridays = [d for d in days if d.weekday() == 4]
    assert weeks == [dict(points)[f] for f in fridays]              # each week closes on its last session
    assert engine.weekly_closes(points, fridays[-1] - timedelta(days=1))[-1] == dict(points)[fridays[-1] -
                                                                                            timedelta(days=1)]


def test_force_recomputes_a_stored_day(rawstore_tmp_db, monkeypatch):
    pin_clock(monkeypatch, ny(2026, 3, 13, 18))
    seed("SPY", date(2026, 3, 13), closes=lambda i: 100.0 + i)
    seed("XLK", date(2026, 3, 13), closes=lambda i: 100.0 + i)
    calls = []
    real = engine.cross_section

    def _counting(closes, spy, as_of):
        calls.append(as_of)
        return real(closes, spy, as_of)
    monkeypatch.setattr(engine, "cross_section", _counting)
    store.backfill_weekly(None, date(2026, 3, 2), date(2026, 3, 10))
    first = len(calls)
    store.backfill_weekly(None, date(2026, 3, 2), date(2026, 3, 10))
    assert len(calls) == first                                 # stored weeks are skipped
    store.backfill_weekly(None, date(2026, 3, 2), date(2026, 3, 10), force=True)
    assert len(calls) == 2 * first and first > 0               # force recomputes them
