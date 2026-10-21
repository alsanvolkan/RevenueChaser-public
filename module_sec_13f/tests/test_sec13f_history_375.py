"""SEC 13F board -> result history (#375, A3). Offline; the store is a temp DuckDB, no SEC or UW request.

``record`` hands ``core/history.record_run`` one ``sec13f_metrics_history`` row per board row, built from the same
entries the board renders. Quarter board: every run keeps its rows; ``sec13f_metrics_by_quarter`` takes each
quarter's last run without an error. Names not refreshed make the run partial; unmatched CUSIPs are a warning.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_sec_13f import engine, history as sec13f_history, report

BOARD, TABLE = "module_sec_13f", "sec13f_metrics_history"
Q1, Q2, Q3 = "2025-12-31", "2026-03-31", "2026-06-30"
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()


def _cell(shares, **kw):
    return {"shares": shares, "value": shares * 10.0, "holders": 5, "new_holders": 2, "exited_holders": 1,
            "top10_pct": 40.0, "hhi": 900.0, "buy_flow": 30.0, "sell_flow": 10.0, **kw}


TOTALS = {"AAA": {Q1: _cell(100.0), Q2: _cell(150.0), Q3: _cell(300.0, holders=9)},
          "BBB": {Q1: _cell(500.0), Q2: _cell(400.0)}}
META = {"AAA": {"name": "Aaa Corp", "sector": "Technology"}, "BBB": {"name": "Bbb Inc", "sector": "Energy"}}
ENRICH = {"AAA": {"price": {Q1: 10.0, Q3: 15.0}, "shares_outstanding": 1000.0, "float_shares": 900.0,
                  "short_pct": 2.5, "insider_pct": 1.5}}
CUSIPS = {"111111111": "aaa", "222222222": {"ticker": "AAA"}, "333333333": {"ticker": "BBB"}, "444": {"ticker": ""}}
SHOWN = {"totals": TOTALS, "meta": META, "enrichment": ENRICH}


@pytest.fixture
def store(rawstore_tmp_db):
    engine.save_cusip_ticker(CUSIPS)
    return rawstore_tmp_db


def _headers():
    return rawstore.query("SELECT * FROM result_runs WHERE board = ? ORDER BY run_at, run_id", [BOARD], db="history")


def _rows(run_id):
    return rawstore.query(f"SELECT * FROM {TABLE} WHERE run_id = ? ORDER BY ticker", [run_id], db="history")


def test_metric_rows_are_the_board_entries_in_board_order():
    quarter, rows = sec13f_history.metric_rows(TOTALS, META, ENRICH, CUSIPS)
    assert quarter == Q3
    _, entries = report.board_entries(TOTALS, META, ENRICH)
    assert [r["ticker"] for r in rows] == [e["ticker"] for e in entries] == ["AAA", "BBB"]
    aaa = rows[0]
    assert aaa["quarter"] == Q3 and aaa["shares"] == 300.0 and aaa["holders"] == 9 and aaa["value"] == 3000.0
    assert aaa["name"] == "Aaa Corp" and aaa["sector"] == "Technology"
    assert aaa["streak"] == 2 and aaa["accel"] == pytest.approx(100.0 - 50.0)
    assert aaa["pct_out"] == pytest.approx(30.0) and aaa["price"] == 15.0 and aaa["price_delta"] == pytest.approx(50.0)
    assert (aaa["shares_outstanding"], aaa["float_shares"], aaa["short_pct"], aaa["insider_pct"]) == \
        (1000.0, 900.0, 2.5, 1.5)
    assert aaa["shares_by_quarter"] == {Q1: 100.0, Q2: 150.0, Q3: 300.0}
    assert aaa["chg_by_quarter"] == {Q2: pytest.approx(50.0), Q3: pytest.approx(100.0)}
    assert aaa["cusips"] == ["111111111", "222222222"]


def test_a_ticker_missing_from_the_latest_quarter_keeps_its_last_snapshot_under_the_board_quarter():
    _, rows = sec13f_history.metric_rows(TOTALS, META, ENRICH, CUSIPS)
    bbb = rows[1]
    assert bbb["quarter"] == Q3 and bbb["shares"] == 400.0 and bbb["shares_by_quarter"][Q3] is None
    assert bbb["price"] is None and bbb["pct_out"] is None and bbb["cusips"] == ["333333333"]


def test_cusips_by_ticker_reads_the_map_like_compute_totals():
    assert sec13f_history.cusips_by_ticker(CUSIPS) == {"AAA": ["111111111", "222222222"], "BBB": ["333333333"]}


def test_a_fetch_records_a_header_and_one_row_per_board_row(store):
    assert sec13f_history.record(SHOWN, run_id="r1", run_at=COMPLETE, ingested=1) == {TABLE: 2}
    (head,) = _headers()
    assert head["error"] is None and head["quality_note"] is None and head["warnings"] is None
    context = json.loads(head["context"])
    assert context.pop("map") == engine.map_identity(engine.load_cusip_ticker())     # #207: the map the run used
    assert context == {"quarter": Q3, "tickers": 2, "ingested": 1}
    rows = _rows("r1")
    assert [r["ticker"] for r in rows] == ["AAA", "BBB"] and str(rows[0]["quarter"]) == Q3
    assert json.loads(rows[0]["cusips"]) == ["111111111", "222222222"]
    assert json.loads(rows[0]["shares_by_quarter"])[Q3] == 300.0


def test_the_header_carries_model_version_and_params(store):
    sec13f_history.record(SHOWN, run_id="r1", run_at=COMPLETE)
    (head,) = _headers()
    assert head["model_version"] == sec13f_history.model_version() and head["model_version"].startswith("4+")   # #375 F2
    assert json.loads(head["params"])["START_QUARTER"] == engine.START_QUARTER


@pytest.mark.parametrize("name, value", [("START_QUARTER", "2024-12-31"), ("_SHRINK_RATIO", 0.4),
                                         ("_VALUE_RATIO", 0.8), ("_UNMATCHED_RISE", 0.1)])
def test_model_version_changes_with_a_setting(monkeypatch, name, value):
    before = sec13f_history.model_version()
    monkeypatch.setattr(engine, name, value)
    assert sec13f_history.model_version() != before


def test_every_run_keeps_its_rows_and_the_quarter_view_takes_the_last_error_free_run(store):
    sec13f_history.record(SHOWN, run_id="r1", run_at=COMPLETE)
    sec13f_history.record(SHOWN, run_id="r2", run_at=COMPLETE + 60)
    sec13f_history.record(SHOWN, run_id="r3", run_at=COMPLETE + 120, error="RuntimeError: SEC index down")
    assert len(_rows("r1")) == len(_rows("r2")) == len(_rows("r3")) == 2
    view = rawstore.query("SELECT DISTINCT run_id FROM sec13f_metrics_by_quarter", db="history")
    assert view == [{"run_id": "r2"}]
    assert [h["error"] for h in _headers()] == [None, None, "RuntimeError: SEC index down"]


def test_names_not_refreshed_make_the_run_partial_and_unmatched_cusips_are_a_warning(store):
    sec13f_history.record(SHOWN, run_id="r1", run_at=COMPLETE, names_note="Names and sectors were not refreshed: x",
                          unmatched="3 CUSIPs with no ticker are left out of the totals")
    (head,) = _headers()
    assert head["quality"] == "partial" and head["quality_note"] == "Names and sectors were not refreshed: x"
    assert json.loads(head["warnings"]) == ["3 CUSIPs with no ticker are left out of the totals"]
    assert len(_rows("r1")) == 2


def test_unmatched_cusips_alone_do_not_make_the_run_partial(store):
    sec13f_history.record(SHOWN, run_id="r1", run_at=COMPLETE, unmatched="3 CUSIPs with no ticker")
    (head,) = _headers()
    assert head["quality"] != "partial"


def test_no_stored_totals_record_a_partial_header_without_rows(store):
    assert sec13f_history.record({"totals": {}}, run_id="r1", run_at=COMPLETE) == {TABLE: 0}
    (head,) = _headers()
    assert head["quality"] == "partial" and "no 13F ownership totals are stored" in head["quality_note"]
    assert json.loads(head["context"])["quarter"] is None


def test_the_board_renders_the_same_entries():
    html = report.render_sec13f_dashboard(TOTALS, META, ENRICH)
    assert "AAA" not in html and "BBB" not in html            # rows live in the payload (#429)
    payload = report.board_payload(TOTALS, META, ENRICH)
    assert [row[0] for row in payload["rows"]] == ["AAA", "BBB"]
    aaa = dict(zip(payload["cols"], payload["rows"][0][3]))
    assert aaa["pct_out"] == ["30.0%", 30.0, "zero"]
    assert aaa["price_delta"] == ["+50.0%", 50.0, "pos"]
