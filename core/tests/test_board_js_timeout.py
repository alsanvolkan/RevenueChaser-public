"""The board-JS harness fails on a page whose scripts leave a live timer, rather than hanging.

Found 2026-10-29: the Data screen's new "this page is old" ribbon was checked on a ``setInterval``, and
``module_settings/tests/test_inventory.py::test_df98_drill_message_by_status`` stopped ending at all - the harness
runs a page's inline scripts under node, and a repeating timer keeps that process alive after the tail has logged.
``subprocess.run`` had no timeout, so the test waited for ever. A hang tells the reader nothing; this says what is
wrong and names the cause.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.tests import board_js


def test_a_page_that_leaves_a_live_timer_fails_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(board_js, "NODE_TIMEOUT_SECONDS", 2)
    html = "<script>setInterval(function(){}, 1000);</script>"
    with pytest.raises(AssertionError) as exc:
        board_js.run_board_js(html, 'console.log(JSON.stringify({ok: 1}));', tmp_path)
    assert "did not let node exit" in str(exc.value) and "setInterval" in str(exc.value)


def test_a_page_that_leaves_no_timer_still_answers(tmp_path):
    assert board_js.run_board_js("<script>var x = 1;</script>",
                                 'console.log(JSON.stringify({ok: x}));', tmp_path) == {"ok": 1}
