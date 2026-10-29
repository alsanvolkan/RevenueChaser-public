"""M-ROT-3 (#374): holdings RS and money-flow volume read D01; the kept-leaderboard guard reads result history.

- ROT-8: a holding D01 holds no bar of keeps its row with "n/a" relative strength (``None``, drawn as a dash, DF67),
  instead of being dropped silently; the holdings RS asks UW for nothing.
- ROT-13: Money Flow ranks D01 closes and volumes (no UW candle); an ETF with no stored series is missing (partial).
- ROT-9: the "kept the stored leaderboard" guard reads the newest complete run in ``money_flow_history`` (K2:
  ``core.history.sessions``), not the regenerable ``.cache`` JSON; a deleted JSON is rebuilt from it.
Offline: temp stores, the calendar clock pinned, UW fakes that fail on any call.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from RevenueChaser.app import runners
from RevenueChaser.app.jobs import JobMessage
from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_sector_rotation.models import ETFRotationSnapshot
from RevenueChaser.module_sector_rotation import engine, money_flow
from RevenueChaser.module_sector_rotation.tests.bars_seed import ny, pin_clock, seed, sessions
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

SESSION = date(2026, 10, 7)
HOLDINGS = [{"ticker": "AAA", "name": "Alpha", "weight": 9.0},
            {"ticker": "ZZZ.L", "name": "Foreign listing", "weight": 7.0},
            {"ticker": "BBB", "name": "Beta", "weight": 5.0}]


class _NoUW:
    """Any call means a D01 input still goes to the provider."""

    def __getattr__(self, name):
        if name == "health":
            return lambda: {"calls": 0, "failed": 0, "auth_error": None, "failures": []}
        raise AssertionError(f"D01 inputs must not call uw.{name}")


def _snap(ticker, group="theme"):
    return ETFRotationSnapshot(as_of_date=SESSION, ticker=ticker, group_name=group, close_price=1.0, rs_1w=0,
                               rs_1m=0, rs_3m=0, rs_ratio=0, rs_momentum=0, rs_ratio_z=0, rs_momentum_z=0,
                               rrg_quadrant="LEADING")


@pytest.fixture
def clock(rawstore_tmp_db, monkeypatch):
    pin_clock(monkeypatch, ny(2026, 10, 7, 18))            # Wednesday after the close: the session is complete
    return rawstore_tmp_db


# ── holdings RS (ROT-8) ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_holding_with_no_d01_bars_shows_n_a_and_is_not_dropped(clock):
    seed("SPY", SESSION, closes=lambda i: 100.0 + i * 0.1)
    seed("AAA", SESSION, closes=lambda i: 50.0 + i * 0.2)
    seed("BBB", SESSION, closes=lambda i: 80.0)
    rawstore.write_rows("etf_holdings", [{"ticker": "AIQ", "entry": HOLDINGS}])
    out = engine.fetch_all_holdings(_NoUW(), [_snap("AIQ")])
    rows = {r["ticker"]: r for r in out["AIQ"]}
    assert [r["ticker"] for r in out["AIQ"]] == ["AAA", "ZZZ.L", "BBB"]          # weight order, nothing dropped
    assert rows["ZZZ.L"]["close"] is None
    assert (rows["ZZZ.L"]["rs_1w"], rows["ZZZ.L"]["rs_1m"], rows["ZZZ.L"]["rs_3m"]) == (None, None, None)
    days = sessions(date(2025, 3, 3), SESSION)
    aaa = {d: 50.0 + i * 0.2 for i, d in enumerate(days)}
    spy = {d: 100.0 + i * 0.1 for i, d in enumerate(days)}
    start = days[-1 - 21]
    want = round(((aaa[SESSION] / aaa[start] - 1) - (spy[SESSION] / spy[start] - 1)) * 100, 2)
    assert rows["AAA"]["rs_1m"] == want and rows["AAA"]["close"] == aaa[SESSION]
    assert rows["BBB"]["rs_3m"] is not None


def test_holdings_rs_is_empty_when_spy_is_short_in_d01(clock):
    seed("SPY", SESSION, start=date(2026, 9, 1), closes=lambda i: 100.0)
    seed("AAA", SESSION)
    rawstore.write_rows("etf_holdings", [{"ticker": "AIQ", "entry": HOLDINGS}])
    assert engine.fetch_all_holdings(_NoUW(), [_snap("AIQ")]) == {}


# ── money flow volume (ROT-13) ───────────────────────────────────────────────────────────────────────────────────────

RANKED = [t for t, i in ROTATION_ETFS.items() if i.get("group") not in ("region", "bond", "yield", "commodity")]


def test_money_flow_ranks_d01_closes_and_volumes_without_uw(clock):
    for k, t in enumerate(RANKED):
        step = (k - len(RANKED) / 2) / 10.0                   # half rise, half fall
        seed(t, SESSION, start=date(2026, 7, 1), closes=lambda i, s=step: 100.0 + i * s)
    result = money_flow.compute_money_flow(days=30, window=21, top_n=10)
    assert result["as_of"] == SESSION.isoformat()
    assert "partial" not in result
    rising = {t for k, t in enumerate(RANKED) if k > len(RANKED) / 2}
    falling = {t for k, t in enumerate(RANKED) if k < len(RANKED) / 2}
    assert len(result["inflows"]) == len(result["outflows"]) == 10
    assert {r["ticker"] for r in result["inflows"]} <= rising and {r["ticker"] for r in result["outflows"]} <= falling
    assert all(r["flow_score"] > 0 for r in result["inflows"])


def test_an_etf_with_no_d01_series_is_missing(clock):
    for t in RANKED[1:]:
        seed(t, SESSION, start=date(2026, 7, 1), closes=lambda i: 100.0 + i)
    result = money_flow.compute_money_flow(days=30, window=21, top_n=10)
    assert result["partial"] is True and result["missing"] == [RANKED[0]]


# ── the kept-leaderboard guard (ROT-9) ───────────────────────────────────────────────────────────────────────────────

FLOW = {"inflows": [{"ticker": "XLK", "name": "Technology", "flow_score": 12.5}],
        "outflows": [{"ticker": "XLE", "name": "Energy", "flow_score": -8.0}], "generated_at": None,
        "as_of": "2026-10-06"}


@pytest.fixture
def board(rawstore_tmp_db, monkeypatch, tmp_path):
    ledger.take_notes()
    latest = tmp_path / "mf.json"
    monkeypatch.setattr(runners, "MONEY_FLOW_LATEST", latest)
    monkeypatch.setattr(runners, "_write_output",
                        lambda m, content, s, ext="html": latest.write_text(content, encoding="utf-8"))
    state = SimpleNamespace(result=None, at=datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc), n=0, latest=latest)
    monkeypatch.setattr(money_flow, "compute_money_flow", lambda **k: json.loads(json.dumps(state.result)))

    def _run_id():
        state.n += 1
        return f"job-{state.n}"
    monkeypatch.setattr(runners, "run_id", _run_id)
    monkeypatch.setattr(runners, "time", SimpleNamespace(time=lambda: state.at.timestamp()))
    return state


def test_the_guard_keeps_the_history_leaderboard_when_the_cache_file_is_gone(board):
    board.result = FLOW
    runners.run_money_flow_latest(_NoUW())                    # Tuesday evening: a complete, recorded leaderboard
    board.latest.unlink()                                     # the regenerable .cache file is gone
    board.at = datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc)
    board.result = {"inflows": [], "outflows": [], "generated_at": None, "as_of": None}
    with pytest.raises(JobMessage, match="kept the stored leaderboard from 2026-10-06"):
        runners.run_money_flow_latest(_NoUW())
    shown = json.loads(board.latest.read_text(encoding="utf-8"))
    assert [r["ticker"] for r in shown["inflows"]] == ["XLK"] and shown["outflows"][0]["flow_score"] == -8.0


def test_a_partial_pull_does_not_replace_a_complete_history_leaderboard(board):
    board.result = FLOW
    runners.run_money_flow_latest(_NoUW())
    board.latest.unlink()
    board.at = datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc)
    board.result = {**FLOW, "inflows": [{"ticker": "SMH", "name": "Semis", "flow_score": 3.0}], "partial": True,
                    "missing": ["XLK"], "as_of": "2026-10-07"}
    with pytest.raises(JobMessage, match="kept the stored leaderboard"):
        runners.run_money_flow_latest(_NoUW())
    assert json.loads(board.latest.read_text(encoding="utf-8"))["inflows"][0]["ticker"] == "XLK"


def test_with_no_complete_run_in_history_a_partial_pull_is_saved_flagged(board):
    board.result = {**FLOW, "partial": True, "missing": ["XLK"]}
    board.latest.write_text(json.dumps(FLOW), encoding="utf-8")   # a JSON that history does not back
    with pytest.raises(JobMessage, match="saved flagged"):
        runners.run_money_flow_latest(_NoUW())
    assert json.loads(board.latest.read_text(encoding="utf-8"))["partial"] is True
