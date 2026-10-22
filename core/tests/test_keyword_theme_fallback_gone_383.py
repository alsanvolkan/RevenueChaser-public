"""#383 C21: the keyword theme fallback is gone; a theme label is measured (the pinned NMF run) or absent.

The sector guess and the keyword matcher are what this slice removed: a guessed theme is indistinguishable
on a board from a measured one. This walks every ``.py`` of the package (tests included) so a stray import
or reference cannot survive the deletion. The banned names are built from parts so this file does not
itself hold them and can be scanned too.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {".git", ".cache", ".store", ".claude", ".hermes", "node_modules",
              "__pycache__", ".venv", "venv"}
_BANNED = ("SECTOR_THEME_" + "FALLBACK", "classify_" + "themes", "resolve_sector_" + "theme",
           "SORTED_" + "THEMES", "THEME_" + "KEYWORDS")


def _python_files():
    for path in sorted(_ROOT.rglob("*.py")):
        if set(path.relative_to(_ROOT).parts[:-1]) & _SKIP_DIRS:
            continue
        yield path


def test_no_module_references_the_deleted_keyword_taxonomy():
    offenders = {}
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [name for name in _BANNED if name in text]
        if hits:
            offenders[path.relative_to(_ROOT).as_posix()] = hits
    assert not offenders, f"the keyword theme fallback must be gone; still referenced in: {offenders}"


def test_taxonomy_no_longer_offers_a_keyword_or_sector_theme_label():
    from RevenueChaser.core import taxonomy
    for name in _BANNED:
        assert not hasattr(taxonomy, name), f"core.taxonomy still defines {name}"
    assert "THEMES" not in taxonomy.__all__
