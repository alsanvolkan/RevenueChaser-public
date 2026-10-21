"""The shared "Recorded sessions" block (#383 C7) on the K2 readers (``core/history`` sessions/runs).

- A session board (MANIFEST ``"snapshot": "session"``: the screeners, LEAPS, Rotation) lists its session snapshots,
  newest first, with every Fetch of the session counted and the rows the snapshot kept.
- A run board (Earnings, OIPD, Relatedness, Value Chain, Financials, 13F) lists its runs, newest first, with their
  quality, rows, params and notes.
- Nothing recorded: one line saying what records the first entry, and the history file is still not created.
- A failed read is shown on the board, never a failed render; every recorded string is escaped.
- Every board that writes history shows the block once (Asymmetric Growth keeps its own panel, #380, same id).
Offline; temp store.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import history, ledger, rawstore
from RevenueChaser.core import reporting_common as rc
from RevenueChaser.core.tests.board_fixtures import BOARDS

# Tue-Wed 2026-10-06..07: 22:00 UTC = 18:00 ET (session complete).
TUE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
TUE_LATER = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc).timestamp()
WED = datetime(2026, 10, 7, 22, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def store(rawstore_tmp_db):
    ledger.take_notes()
    return rawstore_tmp_db


def _swing(run_id, when, n):
    rows = {"swing_scores_history": [{"ticker": f"T{i}", "quality": 50} for i in range(n)]}
    return history.record_run("module_swing_potential", run_id=run_id, run_at=when, model_version=None, rows=rows)


def _chain(run_id, when, warnings=None, params=None):
    return history.record_run("module_value_chain", run_id=run_id, run_at=when, model_version=None,
                              warnings=warnings, params=params)


def test_session_and_run_boards_come_from_the_manifest():
    assert history.is_session_board("module_swing_potential")
    assert history.is_session_board("module_leaps")
    assert not history.is_session_board("module_value_chain")
    assert not history.is_session_board("module_earnings_radar")


def test_nothing_recorded_says_so_and_creates_no_file(store):
    assert not rawstore.history_db_path().exists()
    s = rc.recorded_sessions_html("module_swing_potential")
    r = rc.recorded_sessions_html("module_value_chain")
    assert "Recorded sessions" in s and "No session recorded yet" in s
    assert "Recorded runs" in r and "No run recorded yet" in r
    assert not rawstore.history_db_path().exists()


def test_a_session_board_lists_its_sessions_newest_first(store):
    _swing("tue", TUE, 3)
    _swing("tue-2", TUE_LATER, 5)          # same session: counted, rows not kept (rule B)
    _swing("wed", WED, 2)
    html = rc.recorded_sessions_html("module_swing_potential")
    body = html.split("<table", 1)[1]
    assert body.index("2026-10-07") < body.index("2026-10-06")
    tue = body.split("2026-10-06", 1)[1].split("</tr>", 1)[0]
    assert "<td>2</td>" in tue, "both Tuesday Fetches are counted"
    assert "<td>3</td>" in tue, "the snapshot's 3 rows, not the later Fetch's 5"
    assert "2026-10-06 22:00 UTC" in tue


def test_a_run_board_lists_every_run_with_its_notes(store):
    _chain("a", TUE, params={"ticker": "NVDA"})
    _chain("b", WED, warnings=["2 filings <b>unparsed</b>", "x", "y"])
    html = rc.recorded_sessions_html("module_value_chain")
    body = html.split("<table", 1)[1]
    assert body.index("2026-10-07 22:00 UTC") < body.index("2026-10-06 22:00 UTC")
    assert "ticker=NVDA" in body
    assert "2 filings &lt;b&gt;unparsed&lt;/b&gt;" in body and "<b>unparsed" not in body
    assert "and 1 more" in body


def test_a_failed_read_is_shown_not_raised(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("locked <now>\nsecond line")
    monkeypatch.setattr(history, "runs", boom)
    html = rc.recorded_sessions_html("module_value_chain")
    assert "History could not be read: RuntimeError: locked &lt;now&gt;" in html
    assert "second line" not in html


HISTORY_BOARDS = sorted({s["board"] for s in rawstore.MANIFEST.values()
                         if s.get("db") == "history" and s.get("board")} & set(BOARDS))


def test_every_history_board_is_covered():
    # Money Flow is a panel of the Rotation tab (its own board id, no page of its own): its block is on Rotation.
    boards = {s["board"] for s in rawstore.MANIFEST.values() if s.get("db") == "history" and s.get("board")}
    assert boards - set(BOARDS) == {"module_money_flow"}
    assert len(HISTORY_BOARDS) == 11


def test_the_rotation_tab_also_shows_the_money_flow_block(tmp_path, monkeypatch, store):
    """#383 C7 review (N5): Money Flow records its own sessions, so its panel shows its own block, with its own id."""
    history.record_run("module_money_flow", run_id="mf", run_at=TUE, model_version=None,
                       rows={"money_flow_history": [{"run_id": "mf", "side": "in", "rank": 1, "etf": "XLK"}]})
    html = BOARDS["module_sector_rotation"].render(tmp_path, monkeypatch)
    assert html.count('id="history-panel"') == 1 and html.count('id="mf-history-panel"') == 1
    mf = html.split('id="mf-panel"', 1)[1].split('id="mf-history-panel"', 1)[1].split("</details>", 1)[0]
    assert "2026-10-06" in mf and "next Rotation Refresh" in mf


def test_the_earnings_block_says_it_shows_the_previous_fetch(tmp_path, monkeypatch, store):
    """#383 C7 review (N1): the Earnings page is written by its Fetch before that Fetch is recorded, and its Refresh
    serves the same file, so "a Fetch shows itself from the next Refresh" is false there."""
    html = BOARDS["module_earnings_radar"].render(tmp_path, monkeypatch)
    block = html.split('id="history-panel"', 1)[1].split("</details>", 1)[0]
    assert "from the next Refresh" not in block
    assert "shows up here from the next Fetch" in block
    history.record_run("module_earnings_radar", run_id="e1", run_at=TUE, model_version=None)
    block = BOARDS["module_earnings_radar"].render(tmp_path, monkeypatch).split('id="history-panel"', 1)[1]
    block = block.split("</details>", 1)[0]
    assert "from the next Refresh" not in block and "shows up here from the next Fetch" in block


@pytest.mark.parametrize("key", HISTORY_BOARDS)
def test_every_board_that_writes_history_shows_the_block_once(key, tmp_path, monkeypatch, store):
    html = BOARDS[key].render(tmp_path, monkeypatch)
    assert html.count('id="history-panel"') == 1


def test_the_render_time_read_waits_only_briefly_for_another_process(store, monkeypatch):
    """#383 C7 review (N4): another RevenueChaser process holding ``history.duckdb`` (a nightly job, the backup)
    holds up the render for ~0.35 s, not the ~29 s read budget: the read runs inside ``rawstore.brief_wait()``."""
    seen = []

    def runs(*a, **k):
        seen.append(getattr(rawstore._LOCAL, "brief", False))
        return []
    monkeypatch.setattr(history, "runs", runs)
    rc.recorded_sessions_html("module_value_chain")
    assert seen == [True]
    assert getattr(rawstore._LOCAL, "brief", False) is False
