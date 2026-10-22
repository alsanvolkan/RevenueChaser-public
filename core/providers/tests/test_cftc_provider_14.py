"""#14: the CFTC TFF parse is a provider reader in ``core/providers``, not a board's module.

``module_spx_gamma/cftc.py`` was a Socrata reader end to end — base URL, retry loop, row
mapper — and the only thing board-specific about it was where it lived. Earnings Radar's
D29 writer reached across into it for two private names (``_map_row``, ``_market_where``),
which made an ``earnings`` area depend on a ``market`` board module. The module now lives in
``core/providers/cftc.py`` with public names, and these tests hold that seam shut:

- the parse is importable from ``core.providers.cftc`` under public names;
- ``module_spx_gamma.cftc`` no longer imports (no shim was left behind);
- no source of the earnings area imports an underscore name out of ``module_spx_gamma``,
  and no non-test source of ``core`` imports from it at all — a source scan, so the next
  such import fails here rather than being found by reading;
- ``map_row`` still returns exactly the dict the old ``_map_row`` returned for a recorded
  Socrata row (copied from ``module_earnings_radar/tests/test_cftc_cot_d29.py``).
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from RevenueChaser.core.providers import cftc

ROOT = Path(__file__).resolve().parents[3]

# A recorded TFF row, verbatim from module_earnings_radar/tests/test_cftc_cot_d29.py::ROW.
ROW = {
    "market_and_exchange_names": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
    "report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000",
    "open_interest_all": "2000000",
    "asset_mgr_positions_long": "500000", "asset_mgr_positions_short": "120000",
    "change_in_asset_mgr_long": "1500",
    "lev_money_positions_long": "300000", "lev_money_positions_short": "260000",
    "change_in_lev_money_long": "-2200",
    "dealer_positions_long_all": "90000", "dealer_positions_short_all": "420000",
    "change_in_dealer_long_all": "300",
}


# --- the public seam ------------------------------------------------------------------------------------------------

def test_the_parse_is_public_in_the_provider():
    for name in ("fetch_cftc_tff", "map_row", "market_where"):
        assert hasattr(cftc, name), f"core.providers.cftc.{name} is missing"
        assert not name.startswith("_")
        assert callable(getattr(cftc, name))


def test_module_spx_gamma_cftc_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("RevenueChaser.module_spx_gamma.cftc")


def test_market_where_names_the_e_mini_market():
    assert cftc.market_where() == (
        "market_and_exchange_names like 'E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE'")


# --- the parse did not change ---------------------------------------------------------------------------------------

def test_map_row_returns_the_dict_the_old_private_mapper_returned():
    assert cftc.map_row(ROW) == {
        "report_date": "2026-10-06T00:00:00.000",
        "open_interest": 2000000.0,
        "asset_manager": {"long": 500000.0, "short": 120000.0, "net": 380000.0, "change": 1500.0},
        "leveraged_funds": {"long": 300000.0, "short": 260000.0, "net": 40000.0, "change": -2200.0},
        "dealer": {"long": 90000.0, "short": 420000.0, "net": -330000.0, "change": 300.0},
    }


def test_map_row_of_an_empty_row_is_none():
    assert cftc.map_row({}) is None


# --- the seam stays shut --------------------------------------------------------------------------------------------

def _imports(path: Path):
    """``(lineno, module, [imported names])`` for every import statement in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield node.lineno, node.module or "", [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, [alias.name]


def _sources(*parts: str, skip_tests: bool = False):
    for path in sorted(ROOT.joinpath(*parts).rglob("*.py")):
        rel = path.relative_to(ROOT)
        if "__pycache__" in rel.parts or (skip_tests and "tests" in rel.parts):
            continue
        yield rel.as_posix(), path


def _violations(check, *parts: str, skip_tests: bool = False) -> list[str]:
    found = []
    for rel, path in _sources(*parts, skip_tests=skip_tests):
        for lineno, module, names in _imports(path):
            bad = check(module, names)
            if bad:
                found.append(f"{rel}:{lineno}: {module} imports {', '.join(bad)}")
    return found


_EARNINGS = "module_earnings_radar"


def test_no_earnings_source_imports_an_underscore_name_from_spx_gamma():
    def check(module, names):
        if module.split(".")[:2] != ["RevenueChaser", "module_spx_gamma"]:
            return []
        return [n for n in names if n.startswith("_")]

    assert _violations(check, _EARNINGS) == []


def test_no_core_source_imports_from_spx_gamma():
    # Non-test sources only: the board guard (core/tests/board_fixtures.py) renders boards
    # on purpose, and that import predates this change (reported on #14, not fixed here).
    def check(module, names):
        return names if module.split(".")[:2] == ["RevenueChaser", "module_spx_gamma"] else []

    assert _violations(check, "core", skip_tests=True) == []
