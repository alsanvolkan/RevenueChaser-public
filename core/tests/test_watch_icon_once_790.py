"""One glyph per watch-list half (#790).

Two threads solved "OPT and STK take too much room" on the same day: the shared component put an SVG inside
each button (#770), and the 13F board drew a mask icon in a ``::before`` (#762's follow-up). Both landed, so
every half carried two glyphs. The component owns the glyph; a board may only size it."""
from __future__ import annotations

import re

from RevenueChaser.core import reporting_common as rc
from RevenueChaser.module_sec_13f import report as sec13f

BOARDS = {"sec_13f": sec13f.CSS}


def test_the_component_draws_the_glyph():
    for b in re.findall(r"<button\b[^>]*data-rc-watch[^>]*>.*?</button>", rc.watch_buttons_html("NVDA"), re.S):
        assert "<svg" in b


def test_no_board_draws_a_second_one():
    for name, css in BOARDS.items():
        assert "mask:var(--ico)" not in css, f"{name} still paints its own watch glyph"
        assert "--ico:url(" not in css, f"{name} still carries a watch icon of its own"


def test_the_board_may_still_size_the_halves():
    assert "min-width:44px" in sec13f.CSS or "width:44px" in sec13f.CSS
