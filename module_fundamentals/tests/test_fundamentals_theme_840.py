"""The Fundamentals board follows the shell's theme (#840).

Before this, ``module_fundamentals/report.py`` kept its own dark palette on ``:root`` and never rendered
``THEME_CSS``, so choosing Light turned the shell light and left every card dark: one page, two themes. These
tests pin the three things that keep it from coming back -- the board ships the shared theme, it defines no
colour of its own, and the names it reads are names a theme defines.
"""
from __future__ import annotations

import re
from pathlib import Path

from RevenueChaser.core import reporting_common as rc
from RevenueChaser.module_fundamentals import report as fundamentals

DEFINED = set(rc.THEME_TOKENS["dark"]) | set(rc.THEME_SCALE)
# The board's own aliases, defined on ``:root`` in its first rule from the shared names.
LOCAL = {"panel", "panel2", "text", "green", "red", "neon"}
RCF = re.compile(r"^rcf-")


def test_the_board_ships_the_shared_theme():
    # The head is an f-string, so the theme has to be interpolated there for the rules below to resolve at all.
    src = Path(fundamentals.__file__).read_text(encoding="utf-8")
    assert "{THEME_CSS}" in src and "{THEME_JS}" in src, "the board head must carry the shared theme"


def test_the_board_has_no_palette_of_its_own():
    leftovers = re.findall(r"#[0-9a-fA-F]{6}|rgba?\(", fundamentals.CSS)
    assert not leftovers, f"{sorted(set(leftovers))} should be theme tokens, not literals"


def test_every_token_the_board_reads_is_defined():
    names = {n for n in re.findall(r"var\(--([a-z0-9-]+)", fundamentals.CSS) if not RCF.match(n)}
    unknown = names - DEFINED - LOCAL
    assert not unknown, f"{sorted(unknown)} is read but no theme defines it"


def test_both_schemes_define_every_surface_the_board_aliases():
    # ``--panel`` / ``--panel2`` are the card surfaces: the issue's point was that a light page had none.
    for scheme in ("dark", "light"):
        for token in ("card", "card-2", "fg", "pos", "neg", "accent", "accent-soft", "neg-soft",
                      "input-bg", "input-border", "scrim", "focus"):
            assert token in rc.THEME_TOKENS[scheme], f"{token} missing from the {scheme} theme"


def test_the_rendered_page_carries_the_light_block():
    html = fundamentals.render_fundamental_dashboard({"rows": []})
    assert 'data-theme="light"' in html, "the page must carry the light redefinition of the tokens"
    assert "--card:#FFFFFF" in html.replace(" ", ""), "the light palette must reach the page"
