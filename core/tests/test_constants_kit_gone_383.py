"""#383 C1d: the per-board constants kit is gone; every board is on the shared filter kit.

The old per-board filter snippets in ``core/reporting_common`` were the filters the shared kit
(``FILTERS_JS`` / ``FILTERS_CSS``) replaced. This walks every ``.py`` of the package (tests
included) so a stray import or reference cannot survive the deletion. The banned names are built
from parts so this file does not itself hold them and can be scanned too.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SKIP_DIRS = {".git", ".cache", ".store", ".claude", ".hermes", "node_modules",
              "__pycache__", ".venv", "venv"}
_BANNED = ("MC_" + "FILTER_JS", "TICKER_" + "COMBO_")


def _python_files():
    for path in sorted(_ROOT.rglob("*.py")):
        if set(path.relative_to(_ROOT).parts[:-1]) & _SKIP_DIRS:
            continue
        yield path


def test_no_module_references_the_deleted_constants_kit():
    offenders = {}
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [name for name in _BANNED if name in text]
        if hits:
            offenders[path.relative_to(_ROOT).as_posix()] = hits
    assert not offenders, f"the constants kit must be gone; still referenced in: {offenders}"


def test_reporting_common_has_no_mc_filter_attribute():
    from RevenueChaser.core import reporting_common
    assert not hasattr(reporting_common, "MC_" + "FILTER_JS")
