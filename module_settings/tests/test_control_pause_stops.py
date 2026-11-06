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
