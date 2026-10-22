"""#18: ``sector_rules`` lives in ``module_asymmetric_growth/``, its one production importer.

The rule is AG's own (S6 flags the names, ``engine.py`` is the only production reader), so the module sits beside
it instead of in ``core/``. Three things this holds: the old home is gone (no re-export shim in ``core/``), the
public surface did not change on the way, and no other production code grew an import of it.

Base commit of the move: ``fa3fd6b`` (the ``dir()`` snapshot below was taken there, before the move).
"""
from __future__ import annotations

import re
import sys
from importlib import import_module
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# ``dir()`` of ``RevenueChaser.core.sector_rules`` at fa3fd6b, minus dunders.
PUBLIC_NAMES = [
    "COMPARABLE", "GicsPath", "NOT_COMPARABLE", "NOT_COMPARABLE_SECTORS", "annotations", "canonical_sector",
    "not_comparable", "sector_status", "ticker_status",
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
    sys.modules.pop("RevenueChaser.core.sector_rules", None)     # a stale import would hide the gap
    with pytest.raises(ModuleNotFoundError):
        import_module("RevenueChaser.core.sector_rules")


def test_the_new_home_imports_and_keeps_its_public_names():
    mod = import_module("RevenueChaser.module_asymmetric_growth.sector_rules")
    assert sorted(n for n in dir(mod) if not n.startswith("__")) == PUBLIC_NAMES


def test_only_asymmetric_growth_imports_it():
    importers = _importers("sector_rules")
    assert "module_asymmetric_growth/engine.py" in importers, importers   # the scan can see an importer
    assert [p for p in importers if not p.startswith("module_asymmetric_growth/")] == [], importers
