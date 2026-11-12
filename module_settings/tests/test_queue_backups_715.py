"""#715 step 2: the queue and the weekly backups are drawn on the page itself.

avo, 11 Nov 2026, chose the page over Grafana for every control ("Sayfanın kendisinde"). Both were written
(``control_render._queue``, ``report._newdata_section``) but nothing called them, while ``DATA_HELP`` promised
them. These tests pin that the Fetch tab draws them, in the designed order, and that the help text says so.
"""
from __future__ import annotations

import copy

from RevenueChaser.module_settings import control, control_render, report
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd
from RevenueChaser.module_settings.tests.legacy_fetch import old_fetch


def _view(nd=None) -> dict:
    return control.build(control_nd() if nd is None else nd, now=NOW)


def _fetch(nd=None) -> str:
    return old_fetch(_view(nd))


def test_the_fetch_tab_draws_the_queue_and_the_backups():
    html = _fetch()
    assert 'id="sec-queue"' in html, "the queue is on the page, not only in Grafana"
    assert 'id="sec-backups"' in html, "the weekly backups are on the page, not only in Grafana"


def test_they_sit_after_the_file_groups_and_around_the_cross_check():
    html = _fetch()
    at = [html.index(x) for x in ('class="fgrps"', 'id="sec-queue"', 'id="sec-crosscheck"', 'id="sec-backups"')]
    assert at == sorted(at), "groups, then the queue, the cross-check and the backups last"


def test_a_waiting_request_opens_the_queue():
    assert '<details class="sec" id="sec-queue" open>' in _fetch(), "a queued request is in sight"
    nd = copy.deepcopy(control_nd())
    nd["queue"] = {"active": [], "finished": []}
    assert '<details class="sec" id="sec-queue">' in _fetch(nd), "an empty queue stays closed"


def test_a_copy_past_its_limit_opens_the_backups_and_is_red():
    nd = copy.deepcopy(control_nd())
    nd["backups"]["kinds"] = [{"kind": "ops", "newest": "2026-11-10", "age_days": 1, "copies": 4,
                               "warn": False, "alert": False},
                              {"kind": "prices", "newest": None, "age_days": None, "copies": 0,
                               "warn": True, "alert": True}]
    html = _fetch(nd)
    assert '<details class="sec" id="sec-backups" open>' in html
    assert 'class="s-bad">no copy' in html, "a file with no copy is red and says so"
    assert "1 past its limit" in html
    assert 'class="s-ok">1 d old' in html, "a copy inside its limit is green"


def test_a_backup_folder_that_is_not_set_is_a_red_row():
    nd = copy.deepcopy(control_nd())
    nd["backups"]["configured"] = False
    html = _fetch(nd)
    assert "RAW_STORE_BACKUP_DIR is empty" in html and 'id="sec-backups" open' in html


def test_a_step_that_never_ran_says_so_and_a_failed_one_names_its_error():
    nd = copy.deepcopy(control_nd())
    nd["backups"]["steps"] = [{"step": "duckdb", "last_ok": None, "age_days": None, "warn": True, "alert": True,
                               "error": "disk full"}]
    html = _fetch(nd)
    assert "never ran" in html and "last run failed: disk full" in html


def test_the_board_says_where_the_backup_values_come_from():
    html = _fetch()
    assert f"keeps the newest {control_render.backup.KEEP} copies" in html
    assert "it copies nothing" in html, "the page reads the backup job's record and starts nothing"


def test_the_help_text_names_the_queue_and_the_backups():
    assert "<b>queue</b> with the last runs" in report.DATA_HELP
    assert "<b>weekly backups</b>" in report.DATA_HELP


def test_the_view_carries_the_backups_it_was_read_with():
    assert _view()["backups"]["configured"] is True
    nd = copy.deepcopy(control_nd())
    nd.pop("backups")
    assert _view(nd)["backups"] == {} and "sec-backups" not in _fetch(nd), \
        "nothing read, nothing drawn, no raise"
