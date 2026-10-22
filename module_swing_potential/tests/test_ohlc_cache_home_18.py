"""#18: ``ohlc_cache`` lives in ``module_swing_potential/``, its one production importer.

``indicators._write_ohlc`` is the only production caller (a function-level import: the SPY fallback path), so the
one writer of ``swing_ohlc`` sits beside it instead of in ``core/``. Same three things as the AG move: no old
home, same public surface, no new importer outside the package.

Base commit of the move: ``fa3fd6b`` (the ``dir()`` snapshot below was taken there, before the move).
"""
from __future__ import annotations

import re
import sys
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# ``dir()`` of ``RevenueChaser.core.ohlc_cache`` at fa3fd6b, minus dunders.
PUBLIC_NAMES = [
    "NEW_LISTING_GAP_DAYS", "SHORT_BARS", "TABLE", "_f", "_stored_days", "annotations", "canon_ts", "date",
    "logger", "logging", "pd", "rawstore", "rows_for", "rows_from_download", "short_series_refused", "time",
    "write",
]


def _importers(name: str) -> list[str]:
    """Production ``.py`` files (no tests, no scripts) with an import statement naming ``name``."""
    found = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or rel.startswith(("scripts/", "module_testing/", ".")):
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith(("from ", "import ")) and re.search(rf"\b{name}\b", s):
                found.append(rel)
                break
    return sorted(found)


def test_the_old_home_in_core_is_gone():
    sys.modules.pop("RevenueChaser.core.ohlc_cache", None)       # a stale import would hide the gap
    with pytest.raises(ModuleNotFoundError):
        import_module("RevenueChaser.core.ohlc_cache")


def test_the_new_home_imports_and_keeps_its_public_names():
    mod = import_module("RevenueChaser.module_swing_potential.ohlc_cache")
    assert sorted(n for n in dir(mod) if not n.startswith("__")) == PUBLIC_NAMES


def test_only_swing_potential_imports_it():
    importers = _importers("ohlc_cache")
    assert "module_swing_potential/indicators.py" in importers, importers    # the scan can see an importer
    assert [p for p in importers if not p.startswith("module_swing_potential/")] == [], importers
