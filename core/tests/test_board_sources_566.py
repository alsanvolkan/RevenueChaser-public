"""#566: every board run records which build of which source DuckDB it read, and Data/Health says which boards
are behind.

- ``serving.live_stamp``: the live file's own ``build_id`` / ``built_at``; None with no file.
- ``board_sources.snapshot``: one entry per builder file the board reads, with the build, the newest day of the
  board's need in it, the old-store fallback, and a build that landed after ``run_at``.
- ``history.record_run`` keeps it in ``context["source_builds"]`` and never fails a run over it.
- ``history.last_runs``: the newest header per board.
- ``board_sources.freshness``: current / behind / old store / not recorded, against the build live now.
- ``freshness_tab.section``: the Health tab part, the boards to look at first.
Temp stores only.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import pytest

from RevenueChaser.core import board_sources, history, prices
from RevenueChaser.module_settings import freshness_tab
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.tests.conftest import market_roots  # noqa: F401  (fixture)

BUILT = datetime(2026, 11, 9, 8, 52, tzinfo=timezone.utc).timestamp()      # 09:52 Amsterdam
RUN = datetime(2026, 11, 9, 9, 10, tzinfo=timezone.utc).timestamp()
LATER = datetime(2026, 11, 9, 15, 1, tzinfo=timezone.utc).timestamp()
BOARD = "module_swing_potential"


def _prices_file(build_id: str = "b1", built_at: float = BUILT, last: date = date(2026, 11, 6)):
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                "build_id TEXT, build_mode TEXT)")
    con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('prices', 1, ?, ?, 'live')", [built_at, build_id])
    con.execute("CREATE TABLE price_bar_daily (security_id VARCHAR, session_date DATE)")
    con.execute("INSERT INTO price_bar_daily VALUES ('s1', ?), ('s1', DATE '2026-01-02')", [last])
    con.close()


@pytest.fixture
def roots(monkeypatch, market_roots, rawstore_tmp_db):  # noqa: F811
    monkeypatch.setattr(board_sources, "board_needs", lambda board: ("prices.bars",))
    monkeypatch.setattr(prices, "source_state", lambda: {"effective": prices.LAYER, "reason": None,
                                                          "variable": None})
    return market_roots


def test_live_stamp_reads_the_files_own_build(roots):
    assert serving.live_stamp("prices") is None
    _prices_file()
    stamp = serving.live_stamp("prices")
    assert stamp["build_id"] == "b1" and stamp["built_at"] == BUILT and stamp["file"] == "prices.duckdb"
    assert stamp["build_mode"] == "live"


def test_snapshot_names_the_build_and_the_newest_day(roots):
    _prices_file()
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry == {"kind": "prices", "file": "prices.duckdb", "reads": "file", "build_id": "b1", "built_at": BUILT,
                     "build_mode": "live", "newest_day": "2026-11-06", "day_of": "prices.bars"}


def test_a_build_after_the_run_is_flagged_not_claimed(roots):
    _prices_file(built_at=LATER)
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry["rebuilt_during_run"] is True


def test_the_old_store_fallback_is_said(roots, monkeypatch):
    monkeypatch.setattr(prices, "source_state", lambda: {"effective": prices.STORE, "reason": None,
                                                          "variable": "store"})
    _prices_file()
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry == {"kind": "prices", "file": "prices.duckdb", "reads": "old_store", "reason": "set to store"}


def test_no_file_is_missing(roots):
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry["reads"] == "missing"


def test_the_real_declarations_map_to_builder_files():
    kinds = [k for k, _ in board_sources.board_kinds("module_sec_13f")]
    assert kinds == ["ownership", "prices"]
    assert board_sources.board_kinds("module_money_flow") == board_sources.board_kinds("module_sector_rotation")


def test_record_run_keeps_the_sources_with_the_run(roots):
    _prices_file()
    history.record_run(BOARD, run_id="r1", run_at=RUN, model_version="m", context={"n": 1})
    ctx = history.last_run(BOARD)["context"]
    assert ctx["n"] == 1
    assert ctx["source_builds"][0]["build_id"] == "b1"


def test_a_failing_record_never_fails_the_run(roots, monkeypatch):
    def boom(board, run_at):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(board_sources, "snapshot", boom)
    history.record_run(BOARD, run_id="r1", run_at=RUN, model_version="m")
    assert history.last_run(BOARD)["context"]["source_builds"] == [{"error": "RuntimeError: unreadable"}]


def test_last_runs_is_the_newest_run_of_every_board(roots):
    history.record_run(BOARD, run_id="old", run_at=RUN, model_version="m")
    history.record_run(BOARD, run_id="new", run_at=LATER, model_version="m")
    history.record_run("module_comovement", run_id="c", run_at=RUN, model_version="m")
    assert [(r["board"], r["run_id"]) for r in history.last_runs()] == [("module_comovement", "c"), (BOARD, "new")]


def _run(sources):
    return {"board": BOARD, "run_id": "r", "run_at": RUN, "quality": "complete",
            "context": None if sources is None else {"source_builds": sources}}


REC = {"kind": "prices", "file": "prices.duckdb", "reads": "file", "build_id": "b1", "built_at": BUILT}


def test_freshness_states():
    def live(build_id, built_at):
        return lambda kind: {"build_id": build_id, "built_at": built_at}
    assert board_sources.freshness([_run([REC])], live("b1", BUILT))[0]["state"] == "current"
    behind = board_sources.freshness([_run([REC])], live("b2", LATER))[0]
    assert behind["state"] == "behind" and behind["sources"][0]["live_built_at"] == LATER
    assert board_sources.freshness([_run([{**REC, "rebuilt_during_run": True}])], live("b1", BUILT))[0]["state"] \
        == "behind"
    assert board_sources.freshness([_run([{"kind": "prices", "reads": "old_store"}])], live("b1", BUILT))[0][
        "state"] == "old_store"
    assert board_sources.freshness([_run(None)], live("b1", BUILT))[0]["state"] == "not_recorded"
    assert board_sources.freshness([_run([REC])], lambda kind: None)[0]["state"] == "missing"


def test_the_part_lists_the_boards_to_look_at_first():
    boards = [{"board": "module_comovement", "run_at": RUN, "state": "current",
               "sources": [{**REC, "state": "current", "newest_day": "2026-11-06"}]},
              {"board": BOARD, "run_at": RUN, "state": "behind",
               "sources": [{**REC, "state": "behind", "live_built_at": LATER}]},
              {"board": "module_leaps", "run_at": RUN, "state": "not_recorded", "sources": []}]
    html = freshness_tab.section({"boards": boards}, {BOARD: "Swing Potential"})
    assert html.index("Swing Potential") < html.index("Leaps") < html.index("Comovement")
    assert "1 behind · 1 not recorded yet · 1 current" in html
    assert "live build now <b>9 Nov 16:01</b>" in html                  # Amsterdam, not UTC
    assert "read build <b>9 Nov 09:52</b>" in html and "newest day 6 Nov 2026" in html
    assert "The next Fetch of the board records them" in html


def test_the_part_says_what_it_could_not_read():
    assert "Could not be read: OSError: x" in freshness_tab.section({"error": "OSError: x"})
    assert "No board run is recorded yet" in freshness_tab.section({"boards": []})
