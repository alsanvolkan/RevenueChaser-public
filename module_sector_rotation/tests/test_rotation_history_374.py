"""Rotation and Money Flow boards -> result history (#374, A3). Offline; the store is a temp DuckDB, no UW call.

Rotation records the holdings and the sector tree the board embeds (``rotation_holdings_history``,
``sector_tree_history``); Money Flow records its leaderboard (``money_flow_history``). Both are session boards: the
first complete run of a session keeps its rows, a partial, intraday or later run its header only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_sector_rotation import engine, history as rot_history
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS

COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()   # Tuesday 18:00 ET
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp()
SECTOR = next(t for t, i in ROTATION_ETFS.items() if i.get("group") == "sector")
THEME = next(t for t, i in ROTATION_ETFS.items() if i.get("group") == "theme")
SNAPS = [SimpleNamespace(ticker=t, group_name=i.get("group", "")) for t, i in ROTATION_ETFS.items()]   # every ETF
DETAILED = sorted(t for t, i in ROTATION_ETFS.items() if i.get("group") in ("sector", "theme"))
EXPANDABLE = sorted(t for t, i in ROTATION_ETFS.items() if i.get("group") == "theme")    # ROT-4: holdings RS rows


def _held(t, w):
    return {"ticker": t, "name": t + " Inc", "weight": w, "close": 10.0, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}


HOLDINGS = {SECTOR: [_held("AAA", 9.0), _held("BBB", 5.0)], THEME: [_held("CCC", 7.0)]}
TREE = {SECTOR: [{"industry": "Software", "holdings": [{"ticker": "AAA", "name": "A", "market_cap": 3e9, "weight": 75.0},
                                                       {"ticker": "DDD", "name": "D", "market_cap": 1e9, "weight": 25.0}]},
                 {"industry": "Unclassified", "holdings": [{"ticker": "EEE", "name": "E", "market_cap": 1e8,
                                                            "weight": 100.0}]}]}
FLOW = {"inflows": [{"ticker": "XLK", "name": "Tech", "flow_score": 12.5}, {"ticker": "SMH", "name": "Semis",
                                                                           "flow_score": 8.0}],
        "outflows": [{"ticker": "XLE", "name": "Energy", "flow_score": -9.0}], "as_of": "2026-10-06"}
OK_HEALTH = {"calls": 40, "failed": 0, "auth_error": None, "failures": []}


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    return rawstore_tmp_db


def _headers(board):
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [board], db="history")


def _rows(table, run_id):
    return rawstore.query(f"SELECT * FROM {table} WHERE run_id = ?", [run_id], db="history")


def _rotation(run_id="r1", run_at=COMPLETE, **kw):
    args = {"snapshots": SNAPS, "holdings": HOLDINGS, "sector_holdings": TREE, **kw}
    return rot_history.record_rotation(args.pop("snapshots"), args.pop("holdings"), args.pop("sector_holdings"),
                                       run_id=run_id, run_at=run_at, **{"uw_health": OK_HEALTH, **args})


def _flow(result=FLOW, run_id="m1", run_at=COMPLETE, **kw):
    return rot_history.record_money_flow(result, run_id=run_id, run_at=run_at, days=30, window=21, top_n=10,
                                         **{"uw_health": OK_HEALTH, **kw})


# ── Rotation ──

def test_holdings_rows_keep_board_order_and_the_etf_group():
    rows, dup = rot_history.holdings_rows(HOLDINGS)
    assert dup == 0 and [(r["etf"], r["holding"], r["position"]) for r in rows] == \
        [(SECTOR, "AAA", 1), (SECTOR, "BBB", 2), (THEME, "CCC", 1)]
    assert rows[0]["group_name"] == "sector" and rows[2]["group_name"] == "theme"
    assert {k: rows[0][k] for k in ("name", "weight", "close", "rs_1w", "rs_1m", "rs_3m")} == \
        {"name": "AAA Inc", "weight": 9.0, "close": 10.0, "rs_1w": 1.0, "rs_1m": 2.0, "rs_3m": 3.0}


def test_a_holding_listed_twice_keeps_its_first_row():
    rows, dup = rot_history.holdings_rows({SECTOR: [_held("AAA", 9.0), _held("AAA", 1.0)]})
    assert dup == 1 and [r["weight"] for r in rows] == [9.0]


def test_tree_rows_carry_the_sector_name_of_the_etf():
    rows = rot_history.tree_rows(TREE)
    assert [(r["industry"], r["ticker"]) for r in rows] == [("Software", "AAA"), ("Software", "DDD"),
                                                            ("Unclassified", "EEE")]
    assert {r["sector"] for r in rows} == {ROTATION_ETFS[SECTOR]["name"]} and rows[0]["etf"] == SECTOR
    assert (rows[0]["market_cap"], rows[0]["weight"], rows[0]["name"]) == (3e9, 75.0, "A")


def test_a_complete_rotation_run_keeps_its_holdings_and_tree(store):
    assert _rotation() == {"rotation_holdings_history": 3, "sector_tree_history": 3, "rotation_snapshots_history": 0}
    (head,) = _headers("module_sector_rotation")
    assert head["quality"] == "complete" and head["session_snapshot"] and head["quality_note"] is None
    ctx = json.loads(head["context"])
    assert ctx["snapshots"] == len(SNAPS) and ctx["expandable"] == len(EXPANDABLE) and ctx["window"] is None
    assert ctx["without_holdings"] == [t for t in DETAILED if t not in (SECTOR, THEME)] and ctx["missing"] == []
    assert len(_rows("rotation_holdings_history", "r1")) == 3 and len(_rows("sector_tree_history", "r1")) == 3


def test_the_rotation_header_carries_model_version_and_params(store):
    _rotation(benchmark="QQQ")
    (head,) = _headers("module_sector_rotation")
    params = json.loads(head["params"])
    assert params["benchmark"] == "QQQ" and params["HOLDING_LOOKBACKS"] == engine.HOLDING_LOOKBACKS
    assert head["model_version"] == rot_history.model_version(rot_history.rotation_config("QQQ"))


def test_rotation_model_version_changes_with_a_lookback(monkeypatch):
    before = rot_history.model_version(rot_history.rotation_config())
    monkeypatch.setattr(engine, "HOLDING_LOOKBACKS", {"rs_1w": 5, "rs_1m": 21, "rs_3m": 126})
    assert rot_history.model_version(rot_history.rotation_config()) != before


def test_one_rotation_run_per_session(store):
    _rotation("r1")
    _rotation("r2", run_at=COMPLETE + 600)
    assert len(_rows("rotation_holdings_history", "r2")) == 0
    second = _headers("module_sector_rotation")[1]
    assert "already recorded by run r1" in second["quality_note"]


def test_an_intraday_rotation_run_keeps_its_header_only(store):
    _rotation(run_at=INTRADAY)
    (head,) = _headers("module_sector_rotation")
    assert head["quality"] == "intraday" and not head["session_snapshot"] and _rows("sector_tree_history", "r1") == []


@pytest.mark.parametrize("kw, reason", [
    ({"snapshots": []}, "the RRG pull returned no ETF"),
    ({"holdings": {}}, f"none of the {len(EXPANDABLE)} theme ETFs got its holdings' relative strength"),
    ({"snapshots": [s for s in SNAPS if s.ticker != THEME]},
     f"1 of {sum(t != 'SPY' for t in ROTATION_ETFS)} rotation ETFs are missing from the RRG (no stored series up to the session, or a macro read failed): {THEME}"),
    ({"uw_health": {"calls": 40, "failed": 3, "auth_error": None}}, "3 of 40 Unusual Whales calls failed"),
])
def test_a_degraded_rotation_run_is_partial_with_its_header_only(store, kw, reason):
    _rotation(**kw)
    (head,) = _headers("module_sector_rotation")
    assert head["quality"] == "partial" and reason in head["quality_note"]
    assert _rows("rotation_holdings_history", "r1") == []


def test_one_etf_without_holdings_is_context_not_a_reason(store):
    _rotation(holdings={SECTOR: HOLDINGS[SECTOR]})
    (head,) = _headers("module_sector_rotation")
    without = json.loads(head["context"])["without_holdings"]
    assert head["quality"] == "complete" and THEME in without and SECTOR not in without


def test_a_range_fetch_names_its_window_and_the_backfill_note_is_a_warning(store):
    _rotation(window=("2026-08-01", "2026-08-31"), warnings=["2 days were not written"])
    (head,) = _headers("module_sector_rotation")
    assert json.loads(head["context"])["window"] == ["2026-08-01", "2026-08-31"]
    assert json.loads(head["warnings"]) == ["2 days were not written"]


# ── Money Flow ──

def test_money_flow_rows_rank_each_side_from_one():
    rows = rot_history.money_flow_rows(FLOW)
    assert [(r["side"], r["rank"], r["etf"]) for r in rows] == [("inflow", 1, "XLK"), ("inflow", 2, "SMH"),
                                                                ("outflow", 1, "XLE")]
    assert rows[0]["flow_score"] == 12.5 and rows[0]["name"] == "Tech" and rows[0]["as_of"] == "2026-10-06"


def test_a_complete_money_flow_run_keeps_its_leaderboard(store):
    assert _flow() == {"money_flow_history": 3}
    (head,) = _headers("module_money_flow")
    assert head["quality"] == "complete" and head["session_snapshot"]
    assert json.loads(head["params"])["window"] == 21 and json.loads(head["context"])["inflows"] == 2
    assert {r["etf"] for r in _rows("money_flow_history", "m1")} == {"XLK", "SMH", "XLE"}


def test_money_flow_model_version_changes_with_the_window():
    assert rot_history.model_version(rot_history.money_flow_config(30, 21, 10)) != \
        rot_history.model_version(rot_history.money_flow_config(30, 63, 10))


@pytest.mark.parametrize("result, kw, reason", [
    ({**FLOW, "partial": True, "missing": ["XLB", "XLU"]}, {}, "2 ETFs have no stored daily bars up to the session: XLB, XLU"),
    ({"inflows": [], "outflows": [], "as_of": None}, {}, "no ETF was ranked"),
    (FLOW, {"kept": "2026-10-05"}, "the board kept the stored leaderboard from 2026-10-05"),
    (FLOW, {"uw_health": {"calls": 0, "failed": 0, "auth_error": {"status": 401}}},
     "Unusual Whales rejected the API key (HTTP 401)"),
])
def test_a_degraded_money_flow_run_is_partial_with_its_header_only(store, result, kw, reason):
    _flow(result, **kw)
    (head,) = _headers("module_money_flow")
    assert head["quality"] == "partial" and reason in head["quality_note"] and _rows("money_flow_history", "m1") == []
