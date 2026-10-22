"""#12 (and the first half of #13): the rotation reference data has a neutral home.

``ROTATION_ETFS`` and ``MACRO_SERIES`` were defined by the Sector Rotation board, so
``core/``, ``module_relatedness/`` and ``module_earnings_radar/`` imported a board module to
read two reference dicts. They now live in ``core/rotation_reference.py``; the board's
``module_sector_rotation/universe.py`` re-exports the same objects.

What this file pins, and why each half matters:

- **Identity.** ``universe.ROTATION_ETFS is rotation_reference.ROTATION_ETFS``. Tests patch
  in place (``monkeypatch.setitem(rd.ROTATION_ETFS, ...)`` in ``core/tests/board_fixtures.py``
  and ``module_sector_rotation/tests/``), which works only if every reader holds one dict.
- **The board still binds the names.** ``universe`` keeps binding both names, because tests
  rebind the attribute (``monkeypatch.setattr(engine, "ROTATION_ETFS", UNIVERSE)`` in
  ``test_rotation_a5_374.py``) — a call-time lookup through another module would miss it.
- **The edges are gone.** No non-test source under ``core/``, ``module_relatedness/`` or
  ``module_earnings_radar/`` reaches into the board module for either dict.
- **No lazy import is left for that reason.** ``module_earnings_radar/report.py`` imports the
  reference at module level, which it could not do while the cycle existed.
- **Content parity.** Both key sequences are the ones ``universe.py`` held at the base commit,
  so the move cannot silently drop a symbol.
"""
from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# The key sequences as they stood in ``module_sector_rotation/universe.py`` before the move.
_ROTATION_KEYS = (
    "XLC", "XLE", "XLF", "XLI", "XLB", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "AIQ", "ARKQ", "IGV", "ROBO", "SKYY", "SMH", "SOXX", "WCLD", "AIRR", "ITA", "PAVE",
    "GLD", "LIT", "NLR", "SLV", "URA", "USO", "XME", "XOP", "IBB", "XBI", "JETS", "KRE",
    "EEM", "IWM", "HYG", "LQD", "TLT",
    "EWZ", "EWY", "FXI", "INDA", "EWT", "EWC", "EWA", "EWJ", "EWU", "MCHI", "EWG", "EWW",
    "VNM", "EWS", "EIDO", "ECH", "EWP", "KSA", "EWM", "EPOL", "THD", "EEMV", "EZA", "TUR",
    "ARGT", "EPHE", "IDX", "EPU",
    "US02Y", "US05Y", "US10Y", "US20Y", "US30Y", "US10Y-US02Y", "USIRYY", "CL1!", "MOVE",
    "VVIX", "WHEATF",
)
_MACRO_KEYS = ("US02Y", "US05Y", "US10Y", "US20Y", "US30Y", "US10Y-US02Y", "USIRYY",
               "CL1!", "MOVE", "VVIX", "WHEATF")

# The packages that used to import the board module for these two names. ``app/`` is the
# composition root and may name any area by design (#12), so it is not scanned.
_READER_PACKAGES = ("core", "module_relatedness", "module_earnings_radar")
_NAMES = ("ROTATION_ETFS", "MACRO_SERIES")
# The reference module itself: its docstring names the board module the data moved out of.
_HOME = _ROOT / "core" / "rotation_reference.py"


def _reader_sources():
    for package in _READER_PACKAGES:
        for path in sorted((_ROOT / package).rglob("*.py")):
            parts = path.relative_to(_ROOT).parts[:-1]
            # A test imports the board's ``universe`` on purpose: it patches the attribute that
            # lives there (``test_macro_watch.py``, ``test_cleanup_199.py``).
            if "tests" in parts or "__pycache__" in parts or path == _HOME:
                continue
            yield path


def test_the_board_reexports_the_reference_objects():
    from RevenueChaser.core import rotation_reference
    from RevenueChaser.module_sector_rotation import universe

    assert universe.ROTATION_ETFS is rotation_reference.ROTATION_ETFS
    assert universe.MACRO_SERIES is rotation_reference.MACRO_SERIES


def test_no_reader_package_imports_the_board_for_the_reference_data():
    offenders = {}
    for path in _reader_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "module_sector_rotation" not in text:
            continue
        hits = [name for name in _NAMES if name in text]
        if hits:
            offenders[path.relative_to(_ROOT).as_posix()] = hits
    assert not offenders, (
        "these files read rotation reference data through the board module; import "
        f"RevenueChaser.core.rotation_reference instead: {offenders}")


def test_earnings_radar_report_imports_the_reference_at_module_level():
    src = (_ROOT / "module_earnings_radar" / "report.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "RevenueChaser.core.rotation_reference"
        and any(alias.name == "ROTATION_ETFS" for alias in node.names)
        for node in tree.body), "report.py must import ROTATION_ETFS at module level"

    nested = [node.lineno for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom)
              and (node.module or "").endswith("rotation_reference")
              and node not in tree.body]
    assert not nested, f"no function-level reference import may remain (lines {nested})"


def test_the_reference_keeps_every_symbol_the_board_defined():
    from RevenueChaser.core import rotation_reference

    assert tuple(rotation_reference.ROTATION_ETFS) == _ROTATION_KEYS
    assert tuple(rotation_reference.MACRO_SERIES) == _MACRO_KEYS
