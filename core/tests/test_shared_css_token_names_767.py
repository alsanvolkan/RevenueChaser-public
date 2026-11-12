"""Shared CSS reads token names that exist (#767).

``HELP_CSS``, ``HISTORY_CSS`` and the filter bar are written as ``var(--name, <fallback>)``, so a name that no
theme defines fails silently: the fallback is used, the board looks dark inside a light page, and nothing errors.
Before this, every one of them named a token the palette does not have (``--panel``, ``--accent``, ``--text``,
``--neon``, ``--input``), which is why the help box and the filter bar stayed dark once a board adopted the
palette. These tests pin the names to the two dictionaries that define them."""
from __future__ import annotations

import re
from pathlib import Path

from RevenueChaser.core import reporting_common as rc
from RevenueChaser.module_sec_13f import report as sec13f

DEFINED = set(rc.THEME_TOKENS["dark"]) | set(rc.THEME_SCALE)
# ``--rcf-*`` are the filter bar's own names, defined in its stylesheet from the ones above.
LOCAL = re.compile(r"^rcf-")


def _outer_names(css: str) -> set[str]:
    return {n for n in re.findall(r"var\(--([a-z0-9-]+)\s*,", css) if not LOCAL.match(n)}


def test_help_and_history_name_real_tokens():
    for css in (rc.HELP_CSS, rc.HISTORY_CSS):
        unknown = _outer_names(css) - DEFINED
        assert not unknown, f"{sorted(unknown)} is read but no theme defines it"


def test_filter_bar_names_real_tokens():
    css = (Path(rc.__file__).resolve().parent / "static" / "rc_filters.css").read_text(encoding="utf-8")
    unknown = _outer_names(css) - DEFINED
    assert not unknown, f"{sorted(unknown)} is read but no theme defines it"


def test_the_13f_board_has_no_palette_of_its_own():
    leftovers = re.findall(r"#[0-9a-fA-F]{6}", sec13f.CSS)
    assert not leftovers, f"{sorted(set(leftovers))} should be theme tokens, not literals"


def test_the_13f_board_ships_the_theme():
    # The head is built in an f-string, so the tokens have to be interpolated there for the board's own
    # rules to resolve at all.
    src = Path(sec13f.__file__).read_text(encoding="utf-8")
    assert "{THEME_CSS}" in src and "{THEME_JS}" in src, "the board head must carry the shared theme"
