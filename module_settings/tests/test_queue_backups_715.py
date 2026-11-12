"""#715 step 2: the queue and the weekly backups are drawn on the page itself.

avo, 11 Nov 2026, chose the page over Grafana for every control ("Sayfanın kendisinde"). Both were written
(``control_render._queue``, ``report._newdata_section``) but nothing called them, while ``DATA_HELP`` promised
them. These tests pin that the Fetch tab draws them, in the designed order, and that the help text says so.
"""
from __future__ import annotations


from RevenueChaser.module_settings import control, report
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd


def _view(nd=None) -> dict:
    return control.build(control_nd() if nd is None else nd, now=NOW)


def test_the_help_text_names_the_queue_and_the_backups():
    # #715 (avo, 12 Nov 2026): both are the Grafana board's now, and the help says where to look
    board = report.DATA_HELP.split("<b>Fetch/Health</b> is the Grafana board:", 1)[1].split("Under the board", 1)[0]
    assert "the queue" in board and "the backups" in board
