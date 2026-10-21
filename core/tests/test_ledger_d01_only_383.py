"""K8b (#383 R3, with C12): a store with only D01 bars freezes Swing and Fundamentals.

C12 scores every pick from D01 (``price_bars_daily``), so the freeze and the history snapshot check the picks' bars
there too (``ledger.ohlc_gaps``): the bars a board scored from are the bars the freeze checks. Before, ``ohlc_gaps``
read ``swing_ohlc``, which the boards no longer write, so every D01-only Fetch was refused (the plan's "biggest
risk", screeners_implementation_plan.md:84). Option A (avo 2026-10-03) is unchanged on D01: a pick without one
complete session bar refuses the day and is named. Offline: the scenario of ``core/tests/screen_parity.py`` (D01 and
D04 only, no ``swing_ohlc`` row), the clock at 14 Oct 21:45Z.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import calendar as cal
from RevenueChaser.core import ledger, rawstore, screen_common
from RevenueChaser.core.tests import screen_parity as scn
from RevenueChaser.module_fundamentals import freeze as fund_freeze
from RevenueChaser.module_fundamentals import history as fund_history
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import freeze as swing_freeze
from RevenueChaser.module_swing_potential import history as swing_history
from RevenueChaser.module_swing_potential import pipeline as sp

BOARDS = [pytest.param(sp, swing_freeze, swing_history, id="swing"),
          pytest.param(fp, fund_freeze, fund_history, id="fundamentals")]


@pytest.fixture
def d01_only(rawstore_tmp_db, monkeypatch):
    scn.seed()
    scn.patch(monkeypatch)
    assert rawstore.count("swing_ohlc") == 0


@pytest.mark.parametrize("pipe,frz,hist", BOARDS)
def test_a_d01_only_store_freezes(d01_only, pipe, frz, hist):
    payload = pipe.run_scan()
    out = frz.freeze_payload(payload, run_id="k8b", now=scn.NOW)
    assert out.frozen == len(payload["candidates"]) > 0, out.message
    assert out.forecast_date == scn.SESSION
    assert hist._bar_problem(payload, scn.SESSION) is None


@pytest.mark.parametrize("pipe,frz,hist", BOARDS)
def test_a_name_whose_d01_bars_stop_the_day_before_drops_and_the_day_freezes(d01_only, pipe, frz, hist, monkeypatch):
    """UP1's D01 series ends on 13 Oct, written that evening, while SPY has 14 Oct: Daily Bars ran and UP1 got no
    bar (a halt or a ``failed_both`` name). Since the #434 review (S1) it drops at the scan as ``stale_bars`` instead
    of becoming a pick that refuses the whole day (one gap among this scenario's nine names is 11%, so the drop share
    is raised; ``core/tests/test_screen_r3_review_434.py`` pins the share)."""
    monkeypatch.setattr(screen_common, "STALE_DROP_MAX_SHARE", 0.2)
    with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
        conn.execute("DELETE FROM price_bars_daily WHERE ticker = 'UP1'")
    night_before = datetime(2026, 10, 13, 21, 0, tzinfo=timezone.utc).timestamp()
    kind, drift = scn.BARS["UP1"]
    scn.seed_d01("UP1", scn._bars(kind, drift, scn.LONG_BARS, end=date(2026, 10, 13)), fetched_at=night_before)
    payload = pipe.run_scan()
    assert "UP1" not in {c["ticker"] for c in payload["candidates"]}
    assert {s["key"]: s for s in payload["funnel_steps"]}["stale_bars"]["tickers"] == ["UP1"]
    out = frz.freeze_payload(payload, run_id="k8b", now=scn.NOW)
    assert out.frozen == len(payload["candidates"]) > 0, out.message
    assert hist._bar_problem(payload, scn.SESSION) is None
    # the gap check alone still names such a pick, with the cause a later Fetch cannot fix (S2)
    assert ledger.ohlc_gaps(scn.SESSION, ["UP1", "UP3"]) == {"UP1": ledger.NO_SESSION_BAR}


def test_the_gap_check_reads_d01_not_the_old_cache(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(cal, "_now", lambda: scn.NOW)
    scn.seed_d01("AAA", scn._bars("up", 0.001, 30))
    rawstore.write_rows("swing_ohlc", [{"ticker": "AAA", "ts": "2026-10-13T00:00:00", "open": 1.0, "high": 1.0,
                                        "low": 1.0, "close": 1.0, "volume": 1.0, "cached_at": 0.0}])
    assert ledger.ohlc_gaps(scn.SESSION, ["AAA"]) == {}
    assert ledger.ohlc_gaps(scn.SESSION, ["BBB"]) == {"BBB": ledger.NO_SESSION_BAR}
