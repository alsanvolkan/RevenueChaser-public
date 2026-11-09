"""#566: every board run records which build of which source DuckDB it actually opened, and Data/Health says which
boards are behind.

- ``serving.open_read`` notes each open in ``rcdata/opens``: the build, and the job (or thread) that opened it.
- ``board_sources.snapshot``: the files the run opened with their build; a declared file it did not open is
  ``not_opened`` (with the old-store reason when the boards fell back); two builds in one run is flagged.
- ``history.record_run`` keeps it in ``context["source_builds"]`` and never fails a run over it.
- ``history.last_runs``: the newest header per board.
- ``board_sources.freshness``: current / behind / rolled back / missing / old store / not opened / not recorded.
- ``freshness_tab.section``: the Health tab part, the boards to look at first.
Temp stores only.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import duckdb
import pytest

from RevenueChaser.core import board_sources, history, prices
from RevenueChaser.module_settings import freshness_tab
from RevenueChaser.rcdata import opens, serving
from RevenueChaser.rcdata.tests.conftest import market_roots  # noqa: F401  (fixture)

BUILT = datetime(2026, 11, 9, 8, 52, tzinfo=timezone.utc).timestamp()      # 09:52 Amsterdam
RUN = datetime(2026, 11, 9, 9, 10, tzinfo=timezone.utc).timestamp()
LATER = datetime(2026, 11, 9, 15, 1, tzinfo=timezone.utc).timestamp()
BOARD = "module_swing_potential"


def _prices_file(build_id: str = "b1", built_at: float = BUILT, last: date = date(2026, 11, 6)):
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("prices.tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute(f"CREATE TABLE {serving.VERSION_TABLE} (kind TEXT, version INTEGER, built_at DOUBLE, "
                "build_id TEXT, build_mode TEXT)")
    con.execute(f"INSERT INTO {serving.VERSION_TABLE} VALUES ('prices', 1, ?, ?, 'live')", [built_at, build_id])
    con.execute("CREATE TABLE price_bar_daily (security_id VARCHAR, session_date DATE)")
    con.execute("INSERT INTO price_bar_daily VALUES ('s1', ?), ('s1', DATE '2026-01-02')", [last])
    con.close()
    tmp.replace(path)                                    # a swap, as the builder does


def _read_prices():
    with serving.open_read("prices") as con:
        con.execute("SELECT count(*) FROM price_bar_daily").fetchone()


@pytest.fixture
def roots(monkeypatch, market_roots, rawstore_tmp_db):  # noqa: F811
    opens.clear()
    monkeypatch.setattr(board_sources, "board_needs", lambda board: ("prices.bars",))
    monkeypatch.setattr(prices, "source_state", lambda: {"effective": prices.LAYER, "reason": None,
                                                          "variable": None})
    yield market_roots
    opens.clear()


def test_live_stamp_reads_the_files_own_build_and_is_not_a_read(roots):
    assert serving.live_stamp("prices") is None
    _prices_file()
    stamp = serving.live_stamp("prices")
    assert stamp == {"kind": "prices", "file": "prices.duckdb", "build_id": "b1", "built_at": BUILT}
    assert opens.reads() == {}


def test_only_an_opened_file_is_recorded_as_read(roots):
    _prices_file()
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry == {"kind": "prices", "file": "prices.duckdb", "reads": "not_opened"}
    _read_prices()
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry == {"kind": "prices", "file": "prices.duckdb", "reads": "file", "build_id": "b1", "built_at": BUILT}


def test_an_undeclared_file_the_run_opened_is_recorded_too(roots, monkeypatch):
    monkeypatch.setattr(board_sources, "board_needs", lambda board: ())
    _prices_file()
    _read_prices()
    assert [e["kind"] for e in board_sources.snapshot(BOARD, RUN)] == ["prices"]


def test_a_thread_starts_empty_after_its_run_is_recorded(roots):
    _prices_file()
    _read_prices()
    board_sources.snapshot(BOARD, RUN)
    assert board_sources.snapshot(BOARD, RUN)[0]["reads"] == "not_opened"


def test_another_threads_reads_are_not_this_runs(roots, monkeypatch):
    _prices_file()
    monkeypatch.setattr(opens, "job_started_at", lambda: RUN)        # a page render during the job
    t = threading.Thread(target=_read_prices, name="Thread-render")
    t.start()
    t.join()
    assert board_sources.snapshot(BOARD, RUN)[0]["reads"] == "not_opened"


def test_a_pool_workers_reads_count_after_the_job_started(roots, monkeypatch):
    _prices_file()
    monkeypatch.setattr(opens, "job_started_at", lambda: RUN)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: _read_prices(), range(2)))
    assert board_sources.snapshot(BOARD, RUN)[0]["build_id"] == "b1"


def test_two_builds_in_one_run_are_flagged(roots):
    _prices_file()
    _read_prices()
    _prices_file(build_id="b2", built_at=LATER)
    _read_prices()
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry["rebuilt_during_run"] is True and entry["build_ids"] == ["b1", "b2"] and entry["build_id"] == "b2"


def test_the_old_store_fallback_is_said(roots, monkeypatch):
    monkeypatch.setattr(prices, "source_state", lambda: {"effective": prices.STORE, "reason": None,
                                                          "variable": "store"})
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry == {"kind": "prices", "file": "prices.duckdb", "reads": "not_opened", "old_store": "set to store"}


def test_one_files_error_stays_in_its_own_entry(roots, monkeypatch):
    def boom():
        raise RuntimeError("ops locked")
    monkeypatch.setattr(prices, "source_state", boom)
    [entry] = board_sources.snapshot(BOARD, RUN)
    assert entry["error"] == "RuntimeError: ops locked" and entry["kind"] == "prices"


def test_the_real_declarations_map_to_builder_files():
    assert board_sources.board_kinds("module_sec_13f") == ["ownership", "prices"]
    assert board_sources.board_kinds("module_money_flow") == board_sources.board_kinds("module_sector_rotation")


def test_record_run_keeps_what_the_run_opened(roots):
    _prices_file()
    _read_prices()
    history.record_run(BOARD, run_id="r1", run_at=RUN, model_version="m", context={"n": 1})
    ctx = history.last_run(BOARD)["context"]
    assert ctx["n"] == 1 and ctx["source_builds"][0]["build_id"] == "b1"


def test_a_failing_record_never_fails_the_run(roots, monkeypatch):
    def boom(board, run_at=None):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(board_sources, "snapshot", boom)
    history.record_run(BOARD, run_id="r1", run_at=RUN, model_version="m")
    assert history.last_run(BOARD)["context"]["source_builds"] == [{"error": "RuntimeError: unreadable"}]


def test_last_runs_is_the_newest_run_of_every_board(roots):
    history.record_run(BOARD, run_id="old", run_at=RUN, model_version="m")
    history.record_run(BOARD, run_id="new", run_at=LATER, model_version="m")
    history.record_run("module_comovement", run_id="c", run_at=RUN, model_version="m")
    assert [(r["board"], r["run_id"]) for r in history.last_runs()] == [("module_comovement", "c"), (BOARD, "new")]


def test_the_newest_day_is_the_live_files(roots):
    _prices_file()
    assert board_sources.newest_day("prices", "b1") == "2026-11-06"
    assert opens.reads() == {}


def _run(sources):
    return {"board": BOARD, "run_id": "r", "run_at": RUN, "quality": "complete",
            "context": None if sources is None else {"source_builds": sources}}


REC = {"kind": "prices", "file": "prices.duckdb", "reads": "file", "build_id": "b1", "built_at": BUILT}


def _state(sources, build_id="b1", built_at=BUILT, live=True):
    stamp = (lambda kind: {"build_id": build_id, "built_at": built_at}) if live else (lambda kind: None)
    return board_sources.freshness([_run(sources)], stamp, lambda kind, b: "2026-11-06")[0]


def test_freshness_states():
    assert _state([REC])["state"] == "current"
    behind = _state([REC], "b2", LATER)
    assert behind["state"] == "behind" and behind["sources"][0]["live_built_at"] == LATER
    assert behind["sources"][0]["newest_day"] == "2026-11-06"
    assert _state([REC], "b0", BUILT - 60)["state"] == "rolled_back"
    assert _state([{**REC, "rebuilt_during_run": True}])["state"] == "behind"
    assert _state([REC], live=False)["state"] == "missing"
    assert _state([{"kind": "prices", "reads": "not_opened", "old_store": "x"}])["state"] == "old_store"
    assert _state([{"kind": "prices", "reads": "not_opened"}])["state"] == "not_opened"
    assert _state([{"kind": "options", "reads": "not_opened"}, REC])["state"] == "current"
    assert _state([{"kind": "prices", "error": "E"}])["state"] == "error"
    assert _state(None)["state"] == "not_recorded"


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
