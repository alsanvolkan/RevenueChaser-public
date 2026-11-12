"""One glyph per watch-list half (#796).

Two threads solved "OPT and STK take too much room" on the same day: the shared component put an SVG inside
each button (#770), and the 13F board drew a mask icon in a ``::before`` (the board rework). Both landed, so
every half carried two glyphs. The component owns the glyph; a board may only size it."""
from __future__ import annotations

import re

from RevenueChaser.core import reporting_common as rc
from RevenueChaser.module_sec_13f import report as sec13f

BOARDS = {"sec_13f": sec13f.CSS}


def test_only_the_component_draws_the_glyph():
    for b in re.findall(r"<button\b[^>]*data-rc-watch[^>]*>.*?</button>", rc.watch_buttons_html("NVDA"), re.S):
        assert "<svg" in b, "the component is where the glyph comes from"
    for name, css in BOARDS.items():
        assert "mask:var(--ico)" not in css, f"{name} paints a second glyph over the component's"
        assert "--ico:url(" not in css, f"{name} still carries a watch icon of its own"
        assert "min-width:44px" in css or "width:44px" in css, f"{name} should still size the halves"
