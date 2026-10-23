"""Result history (schema 11, history redesign C): ``core/history.record_run`` and the ``history.duckdb`` file.

One header row per board run in ``result_runs`` plus the run-keyed rows, in ONE transaction on the history file. A
session board keeps one whole run per session; a run board keeps every run. Temp stores only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import history, rawstore

# 2026-10-06 is a Tuesday: 22:00 UTC is 18:00 ET (the session is complete), 14:00 UTC is 10:00 ET (intraday).
COMPLETE_1 = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
COMPLETE_2 = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc).timestamp()
INTRADAY = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp()
NEXT_DAY = datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc).timestamp()

SESSION_BOARD, SESSION_TABLE = "module_money_flow", "money_flow_history"
RUN_BOARD, RUN_TABLE = "module_earnings_radar", "earnings_cards_history"


def _flow(*ranks):
    return [{"side": "in", "rank": r, "etf": f"E{r}", "name": "n", "flow_score": float(r)} for r in ranks]


def _run(run_id, run_at, rows, board=SESSION_BOARD, table=SESSION_TABLE, **kw):
    return history.record_run(board, run_id=run_id, run_at=run_at, model_version="m1", rows={table: rows}, **kw)


def _headers(board=SESSION_BOARD):
    return rawstore.query("SELECT run_id, session_date::TEXT AS session_date, quality, session_snapshot, quality_note, "
                          "row_counts FROM result_runs WHERE board = ? ORDER BY run_at", [board], db="history")


def _rows(table, run_id):
    return rawstore.query(f"SELECT * FROM {table} WHERE run_id = ?", [run_id], db="history")


@pytest.fixture
def store(rawstore_tmp_db):
    return rawstore_tmp_db


def test_the_manifest_puts_18_tables_in_the_history_file_and_none_in_raw(store):
    names = [n for n, s in rawstore.MANIFEST.items() if s.get("db") == "history"]
    assert len(names) == 18 and "result_runs" in names
    assert all(rawstore._db_of(n) == "history" for n in names)
    rawstore.ensure_history()
    assert {r["table_name"] for r in rawstore.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='BASE TABLE'",
        db="history")} == set(names) | {"store_schema"}   # #26 PL7: the file's own stamp, not a history table
    raw_tables = {r["table_name"] for r in rawstore.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'")}
    assert not raw_tables & set(names)


def test_the_history_file_appears_with_the_first_recorded_run_and_not_before(store):
    assert not rawstore.history_db_path().exists()                   # the first store call did not make it
    _run("r1", COMPLETE_1, _flow(1, 2))
    assert rawstore.history_db_path().exists()
    views = {r["view_name"] for r in rawstore.query("SELECT view_name FROM duckdb_views() WHERE NOT internal",
                                                    db="history")}
    assert "result_sessions" in views and "money_flow_by_session" in views and "sec13f_metrics_by_quarter" in views


def test_a_session_board_keeps_one_whole_run_per_session(store):
    _run("r1", COMPLETE_1, _flow(1, 2))
    _run("r2", COMPLETE_2, _flow(1, 2, 3, 4))                        # a second complete run of the same session
    assert [h["session_snapshot"] for h in _headers()] == [True, False]
    assert "already recorded by run r1" in _headers()[1]["quality_note"]
    assert len(_rows(SESSION_TABLE, "r1")) == 2 and _rows(SESSION_TABLE, "r2") == []      # never a mix of two runs
    snap = rawstore.query("SELECT count(*) AS n FROM money_flow_by_session", db="history")[0]["n"]
    assert snap == 2


def test_an_intraday_or_failed_run_is_never_a_snapshot(store):
    _run("early", INTRADAY, _flow(1))
    _run("bad", COMPLETE_1, _flow(1), error="provider down")
    _run("late", COMPLETE_2, _flow(1, 2, 3))                          # the first complete, error-free run wins
    assert {h["run_id"]: h["session_snapshot"] for h in _headers()} == {"early": False, "bad": False, "late": True}
    assert _rows(SESSION_TABLE, "early") == [] and _rows(SESSION_TABLE, "bad") == []
    assert len(_rows(SESSION_TABLE, "late")) == 3


def test_a_next_session_gets_its_own_snapshot(store):
    _run("r1", COMPLETE_1, _flow(1))
    _run("r2", NEXT_DAY, _flow(1, 2))
    assert [h["session_snapshot"] for h in _headers()] == [True, True]
    assert sorted(h["session_date"] for h in _headers()) == ["2026-10-06", "2026-10-07"]


def test_a_run_board_keeps_every_run(store):
    for i, at in enumerate((COMPLETE_1, COMPLETE_2, INTRADAY)):
        _run(f"e{i}", at, [{"ticker": "AAA"}, {"ticker": "BBB"}], board=RUN_BOARD, table=RUN_TABLE)
    assert [len(_rows(RUN_TABLE, f"e{i}")) for i in range(3)] == [2, 2, 2]
    assert rawstore.query("SELECT count(*) AS n FROM result_runs WHERE board = ?", [RUN_BOARD], db="history")[0]["n"] == 3


def test_recording_the_same_run_twice_writes_nothing_the_second_time(store):
    _run("r1", COMPLETE_1, _flow(1, 2))
    again = _run("r1", COMPLETE_1, _flow(1, 2, 3))
    assert again == {} and len(_rows(SESSION_TABLE, "r1")) == 2 and len(_headers()) == 1


def test_a_null_key_fails_the_whole_run_and_leaves_no_header(store):
    with pytest.raises(Exception):
        _run("bad", COMPLETE_1, [{"side": "in", "rank": None, "etf": "E"}])
    assert _headers() == [] and _rows(SESSION_TABLE, "bad") == []


def test_a_board_cannot_write_another_boards_table(store):
    with pytest.raises(rawstore.RawStoreError, match="does not own"):
        history.record_run(SESSION_BOARD, run_id="r1", run_at=COMPLETE_1, model_version=None,
                           rows={RUN_TABLE: [{"ticker": "AAA"}]})
    assert not rawstore.history_db_path().exists()


def test_a_partial_run_is_kept_as_partial_and_says_why(store):
    _run("p1", COMPLETE_1, [{"ticker": "AAA"}], board=RUN_BOARD, table=RUN_TABLE, partial="degraded pull")
    h = _headers(RUN_BOARD)[0]
    assert h["quality"] == "partial" and h["quality_note"] == "degraded pull"
    assert len(_rows(RUN_TABLE, "p1")) == 1                           # a run board keeps its rows


def test_a_run_outside_the_holiday_table_is_partial_not_a_snapshot(store):
    far = datetime(2031, 1, 6, 22, 0, tzinfo=timezone.utc).timestamp()
    _run("far", far, _flow(1))
    h = _headers()[0]
    assert h["quality"] == "partial" and h["session_snapshot"] is False and h["session_date"] is None


def test_a_key_less_latest_table_keeps_a_repeated_key_in_its_history(store):
    """chain_mentions has no PRIMARY KEY and keeps repeats; its history is keyed (run_id, row_no) (report F12)."""
    rep = [{"src": "AAA", "dst_name": "Supplier Co", "dst_ticker": "SUP", "relation": "supplier", "snippet": s,
            "filed": "2026-02-01"} for s in ("first mention", "second mention")]
    history.record_run("module_value_chain", run_id="vc1", run_at=COMPLETE_1, model_version=None,
                       rows={"value_chain_edges_history": rep})
    got = rawstore.query("SELECT row_no, snippet FROM value_chain_edges_history ORDER BY row_no", db="history")
    assert got == [{"row_no": 0, "snippet": "first mention"}, {"row_no": 1, "snippet": "second mention"}]


def test_the_history_file_refuses_rewrites(store):
    _run("r1", COMPLETE_1, _flow(1, 2))
    with pytest.raises(rawstore.RawStoreError, match="history"):
        rawstore.replace_all(SESSION_TABLE, [])
    with pytest.raises(rawstore.RawStoreError):
        rawstore.execute(f"DELETE FROM {SESSION_TABLE}", db="history")
    assert len(_rows(SESSION_TABLE, "r1")) == 2


def test_each_mirror_has_its_latest_tables_columns_and_types(store):
    """A history row matches the latest row column for column, so the two cannot drift (``_mirror_entry``)."""
    for name, spec in rawstore.HISTORY.items():
        latest = spec.get("mirror_of")
        if not latest:
            continue
        def name(c):                    # a quoted column ("group" in rotation_snapshots) is typed by its bare name
            return c.split()[0].strip('"')
        want = {name(c): rawstore._spec_base_type(rawstore.MANIFEST[latest], name(c))
                for c in rawstore.MANIFEST[latest]["columns"]
                if c.split()[0].upper() not in rawstore._CONSTRAINT_KEYWORDS}
        have = {name(c): rawstore._spec_base_type(spec, name(c))
                for c in spec["columns"] if c.split()[0].upper() not in rawstore._CONSTRAINT_KEYWORDS}
        have.pop("run_id")
        have.pop("row_no", None)
        assert have == want, name


def test_a_run_with_two_tables_is_all_or_nothing(store):
    """Rotation writes two tables; a NULL key in the second must leave neither rows of the first nor a header (S1)."""
    good = [{"etf": "XLK", "holding": "AAPL", "weight": 0.2}]
    bad = [{"sector": "Tech", "industry": None, "ticker": "AAPL"}]       # NULL key part
    with pytest.raises(Exception):
        history.record_run("module_sector_rotation", run_id="rot1", run_at=COMPLETE_1, model_version=None,
                           rows={"rotation_holdings_history": good, "sector_tree_history": bad})
    if rawstore.history_db_path().exists():
        assert rawstore.query("SELECT * FROM result_runs", db="history") == []
        assert rawstore.query("SELECT * FROM rotation_holdings_history", db="history") == []


def _sample(base_type: str, i: int):
    t = base_type.upper()
    if t.startswith("DATE"):
        return "2026-10-06"
    if t == "DOUBLE":
        return 1.5 + i
    if t in ("INTEGER", "BIGINT"):
        return 7 + i
    if t == "BOOLEAN":
        return True
    if t == "JSON":
        return {"k": [1, 2.5, "x"]}
    if t == "BLOB":
        return {"blob": i}
    return f"v{i}"


@pytest.mark.parametrize("board", sorted({s["board"] for s in rawstore.HISTORY.values() if "board" in s}))
def test_every_history_table_takes_a_full_row_through_record_run(store, board):
    """N6: each of the 17 row tables accepts a row with every column filled, compressed columns included."""
    tables = history.tables_of(board)
    rows = {}
    for t in tables:
        spec = rawstore.MANIFEST[t]
        cols = [c for c in rawstore._columns_of(spec) if c not in ("run_id", "row_no")]
        rows[t] = [{c: _sample(rawstore._spec_base_type(spec, c), i) for c in cols} for i in range(2)]
    history.record_run(board, run_id=f"{board}-1", run_at=COMPLETE_1, model_version="m", rows=rows,
                       params={"p": 1}, funnel={"f": 2}, payload={"big": list(range(5))})
    for t in tables:
        assert len(_rows(t, f"{board}-1")) == 2, t
