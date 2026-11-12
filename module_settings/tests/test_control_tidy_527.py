"""#527 step 8b (avo 6 Nov 2026): the Data page says less at once. The Fill legend and the Fetch / Pause / Resume
text are one visible line each with the rest behind a "?"; Pause shows on an idle dataset too (decision A); the
Cross-check table wears the journey's badges and pills; the tree's glow drops when the pointer leaves and its lines
keep the provider's colour on a light page."""
from __future__ import annotations

import re

from RevenueChaser.module_settings import control_render


def test_the_tree_glow_drops_on_leave_and_on_a_redraw():
    js = control_render.TREE_JS
    enter = js.split("mouseenter", 1)[1].split("});", 1)[0]
    assert "svg.appendChild(g)" not in enter, "moving the hovered node loses its mouseleave"
    draw = js.split("function draw(j)", 1)[1].split("while (svg.firstChild)", 1)[0]
    assert "unlit(svg)" in draw
    assert "classList.remove('dtree-ring')" in js and "classList.remove('dtree-lit')" in js


def test_the_tree_lines_keep_their_colour_on_a_light_page():
    js = control_render.TREE_JS
    assert "if (!dark()) return" in js
    assert re.search(r"svg\.dtree \.vis\{stroke-width:1\.6;opacity:\.9", control_render.CTL_CSS)
