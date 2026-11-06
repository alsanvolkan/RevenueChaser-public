"""#492 PR (ii) on the board: Pause stops for real, the queue has Stop, and Fetch has a ▾ for a partial run."""
from __future__ import annotations

from RevenueChaser.module_settings import control_render, report
from RevenueChaser.module_settings.tests.test_control_render_c1 import _box, _sources, _view


def test_fetch_carries_a_menu_for_raw_and_raw_plus_parquet():
    v = _view()
    v["queue"] = {"active": [], "finished": []}
    idle = _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    assert 'data-job="fmenu" aria-haspopup="menu"' in idle
    assert 'data-stage="raw" title=' in idle and ">Fetch raw</button>" in idle
    assert 'data-stage="parquet"' in idle and ">Fetch raw + Parquet</button>" in idle
    v["queue"] = {"active": [{"id": 4, "dataset": "alpaca/bars", "status": "running"}], "finished": []}
    busy = _box(_sources(v, "prices.duckdb"), "alpaca/bars")
    assert 'data-job="fmenu"' in busy
    assert 'aria-label="Fetch only part of the run: alpaca/bars" hidden>' in busy, "drawn but hidden beside Pause"
    assert "X.setState = setState" in control_render.CTL_JS, "a finished run brings the ▾ back through setState"


def test_the_queue_has_stop_on_queued_and_running_rows_only():
    v = _view()
    v["queue"] = {"active": [{"id": 1, "dataset": "market_series", "status": "queued"},
                             {"id": 2, "dataset": "alpaca/bars", "status": "running"}],
                  "finished": [{"id": 3, "dataset": "sec/13f", "status": "done"}]}
    q = control_render._queue(v)
    assert q.count('data-job="stop"') == 2
    assert 'data-job="stop" data-dataset="market_series" data-status="queued">Stop' in q
    assert 'data-dataset="sec/13f" data-status' not in q


def test_the_words_say_pause_stops_and_the_old_ones_are_gone():
    html = control_render.render_data(_view()) + control_render._queue(_view())
    for gone in ("cannot be cancelled", "cannot be taken back", "no cancel route"):
        assert gone not in html and gone not in report.ND_PAUSE_STOPS, gone
    assert "<b>Pause</b>: stop &amp; hold" in html, "the legend's one line (Q19)"
    assert "Pause cancels a queued run, stops a running one before its next provider call" in html, "its ⓘ"
    js = control_render.CTL_JS
    assert "bd.stage = sg.getAttribute('data-stage')" in js and "C.routes.schedule + ds + '/pause'" in js


# ── Board tidy review (#533): a running row keeps its Stop unless its own pid is known to be gone ──────────────

def _qview(rows, jobs, unread=()):
    v = _view()
    v["queue"] = {"active": rows, "finished": []}
    v["health"] = {**v["health"], "jobs": jobs, "unread": list(unread)}
    return v


JOB = {"pid": 41, "name": "sec/13dg from 2022", "dataset": "sec/13dg", "datasets": ["sec/13dg"], "minutes": 5,
       "limit": None, "who": "Claude session B", "cls": "none"}


def test_a_cli_row_is_matched_to_its_process_by_dataset_and_keeps_stop():
    q = control_render._queue(_qview([{"id": 1, "dataset": "sec/13dg", "status": "running", "requested_by": "cli"}],
                                     [JOB]))
    assert ">Claude session B<" in q and 'data-job="stop" data-dataset="sec/13dg"' in q


def test_a_recorded_pid_matches_before_the_dataset():
    other = {**JOB, "pid": 42, "datasets": ["sec/form4"], "dataset": "sec/form4", "who": "Claude session A"}
    q = control_render._queue(_qview([{"id": 1, "dataset": "sec/13dg", "status": "running", "requested_by": "cli",
                                       "pid": 42}], [JOB, other]))
    assert ">Claude session A<" in q


def test_no_matching_process_is_not_a_claim_that_none_runs():
    for jobs, unread in (([], ()), ([], ("the running jobs",))):
        q = control_render._queue(_qview([{"id": 1, "dataset": "sec/13dg", "status": "running",
                                           "requested_by": "cli"}], jobs, unread))
        assert "no live process" not in q and 'data-job="stop" data-dataset="sec/13dg"' in q


def test_only_a_pid_known_gone_drops_stop():
    q = control_render._queue(_qview([{"id": 1, "dataset": "sec/13dg", "status": "running", "requested_by": "cli",
                                       "pid": 99, "pid_gone": True}], []))
    assert "running · no live process" in q and 'data-job="stop"' not in q


def test_asked_at_is_the_request_and_started_is_its_own_value():
    rows = [{"id": 1, "dataset": "a/b", "status": "running", "requested_by": "dashboard",
             "requested_at": 1_700_000_000.0, "started_at": 1_700_000_600.0},
            {"id": 2, "dataset": "c/d", "status": "queued", "requested_by": "dashboard", "requested_at": "soon"}]
    q = control_render._queue(_qview(rows, []))
    assert ">23:13 · started 23:23 · " in q, "requested 22:13 UTC = 23:13 Amsterdam; started ten minutes later"
    assert 'data-dataset="c/d"' in q and '<td data-label="Asked at" class="nw">—</td>' in q, "a bad time is one cell"


def test_the_cross_check_notes_open_below_the_table_not_inside_its_scroll_box():
    v = _view()
    v["cross_check"] = [{"field": "interest rates", "need": "macro.dgs10", "second_source": None,
                         "why": "FRED is the only one that answers for it"}]
    html = control_render._cross(v)
    table_end = html.index("</table></div>")
    assert 'class="info"' not in html[:table_end], "a tip inside .scroll is clipped by its overflow"
    assert "FRED is the only one that answers for it" in html[table_end:]
