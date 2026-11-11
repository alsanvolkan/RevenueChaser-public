"""The boards' links to the old store and to providers may only shrink (#642 PR 1, F6 of the group-1 review).

Group 1 cuts the boards off the old ``.store`` tables (``raw.duckdb``, ``edgar.duckdb``, ``history.duckdb``,
``prices.duckdb``) and off live provider calls, board by board. Nothing stopped a board from gaining a new link while
that happens. Three ceilings do, each read from the boards' own source (tests left out), never trusted:

- ``boards.UNDECLARED`` can lose a board, never gain one.
- **Old tables:** every SQL text in a board package (and ``module_settings``, which ``NOT_A_BOARD`` hides from the
  need checks) that names an old-store table must be listed in ``OLD_TABLES`` for that package.
- **Providers:** every file in a board package that imports ``core.providers`` must be listed in ``PROVIDER_FILES``.

Both lists are exact, so a PR that removes a link also removes its line here and the list shrinks with the work.
Limit (the review's F6): a board that reaches an old table through ``core/`` is not followed; ``core`` is #645's.
"""
from __future__ import annotations

import ast
import re

from RevenueChaser.core import rawstore
from RevenueChaser.rcdata.needs import boards

#: ``boards.UNDECLARED`` on 11 Nov 2026. Remove a board here when it migrates; never add one.
UNDECLARED_CEILING = frozenset({
    "module_asymmetric_growth", "module_comovement", "module_financials", "module_fundamentals", "module_leaps",
    "module_news", "module_oipd", "module_relatedness", "module_sec_13f", "module_sector_rotation",
    "module_swing_potential", "module_uw_interaction", "module_value_chain", "module_welcome",
})

#: ``{package: old-store tables its SQL names}`` on 11 Nov 2026 (main 34ac3e55).
OLD_TABLES = {
    "module_asymmetric_growth": {"ag_scores_history"},
    "module_earnings_radar": {"earnings_calendar"},
    "module_financials": {"macro_series", "result_runs"},
    "module_leaps": {"leaps_footprint_history"},
    "module_relatedness": {"chain_mentions"},
    "module_sec_13f": {"price_actions", "price_bars_daily"},
    "module_settings": {"edgar_documents", "equity_universe", "equity_universe_provenance", "identity_conflicts",
                        "identity_runs", "identity_source_cache", "index_constituents", "index_constituents_changes",
                        "job_runs", "price_bars_daily", "sec_exchange_file", "security_master", "swing_ohlc"},
    "module_swing_potential": {"swing_ohlc"},
    "module_value_chain": {"chain_meta", "edgar_documents", "equity_universe", "sec_company_tickers"},
    "module_welcome": {"earnings_calendar", "index_levels_daily", "job_runs", "macro_series", "vol_index_daily"},
}

#: ``{package: files (relative to it) that import core.providers}`` on 11 Nov 2026 (main 34ac3e55).
PROVIDER_FILES = {
    "module_asymmetric_growth": {"engine.py"},
    "module_earnings_radar": {"history.py", "inputs/cftc_cot.py", "inputs/estimates.py", "inputs/short_interest.py",
                              "macro_watch.py", "pipeline.py"},
    "module_financials": {"engines/comps_engine.py", "engines/credit_capacity_engine.py", "engines/dcf_engine.py",
                          "engines/quick_remarks_engine.py", "engines/three_statement_engine.py", "run.py"},
    "module_fundamentals": {"history.py", "pipeline.py"},
    "module_leaps": {"scan.py"},
    "module_news": {"headlines.py", "tape.py"},
    "module_oipd": {"engine.py"},
    "module_relatedness": {"__main__.py", "compare.py", "engine.py", "history.py", "naming.py", "profiles.py",
                           "store.py"},
    "module_sector_rotation": {"__main__.py", "engine.py"},
    "module_spx_gamma": {"run.py"},
    "module_swing_potential": {"indicators.py", "pipeline.py"},
    "module_uw_interaction": {"engine.py"},
    "module_value_chain": {"edgar_text.py", "engine.py", "sync.py"},
}

#: A table an SQL text reads or writes, with an optional ``raw.`` / ``main.`` schema in front.
_SQL = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:raw\.|main\.)?([a-z_][a-z0-9_]*)", re.IGNORECASE)


def _packages() -> list[str]:
    return [*boards.board_packages(), "module_settings"]


def _sources(package: str):
    for path in boards._board_sources(package):
        yield path, str(path.relative_to(boards.root() / package)).replace("\\", "/"), ast.parse(
            path.read_text(encoding="utf-8"))


def _old_tables() -> set[str]:
    """Every MANIFEST table that lives in an old ``.store`` file, not in a board's own result file."""
    return {name for name in rawstore.MANIFEST if rawstore._db_of(name) not in rawstore.BOARD_DBS}


def _imports_providers(node) -> bool:
    if isinstance(node, ast.ImportFrom) and node.module:
        parts = node.module.split(".")
        return "providers" in parts or (parts[-1] == "core" and any(a.name == "providers" for a in node.names))
    if isinstance(node, ast.Import):
        return any("providers" in a.name.split(".") for a in node.names)
    return False


def _scan() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    old = _old_tables()
    tables: dict[str, set[str]] = {}
    providers: dict[str, set[str]] = {}
    for package in _packages():
        for _path, rel, tree in _sources(package):
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    tables.setdefault(package, set()).update(t for t in _SQL.findall(node.value) if t in old)
                elif _imports_providers(node):
                    providers.setdefault(package, set()).add(rel)
    return ({p: t for p, t in tables.items() if t}, {p: f for p, f in providers.items() if f})


def test_undeclared_only_shrinks():
    grew = boards.UNDECLARED - UNDECLARED_CEILING
    assert not grew, f"boards added to UNDECLARED: {sorted(grew)}; a new board declares READS instead"


def test_the_ceiling_names_no_board_that_has_migrated():
    stale = UNDECLARED_CEILING - boards.UNDECLARED
    assert not stale, f"migrated, remove from UNDECLARED_CEILING: {sorted(stale)}"


def test_the_scan_sees_the_boards():
    old = _old_tables()
    assert {"job_runs", "equity_universe", "price_bars_daily"} <= old, "the old-table list is broken, not the tree"
    assert "module_settings" in _packages()


def test_no_board_gains_an_old_table_and_a_removed_one_leaves_the_list():
    found, _ = _scan()
    new = {p: sorted(t - OLD_TABLES.get(p, set())) for p, t in found.items() if t - OLD_TABLES.get(p, set())}
    assert not new, f"new reads or writes of old-store tables: {new}; read through rcdata.read instead"
    gone = {p: sorted(t - found.get(p, set())) for p, t in OLD_TABLES.items() if t - found.get(p, set())}
    assert not gone, f"no longer named, remove from OLD_TABLES: {gone}"


def test_no_board_file_gains_a_provider_import_and_a_removed_one_leaves_the_list():
    _, found = _scan()
    new = {p: sorted(f - PROVIDER_FILES.get(p, set())) for p, f in found.items() if f - PROVIDER_FILES.get(p, set())}
    assert not new, f"new provider imports in boards: {new}; a board reads stored data, the builder fetches"
    gone = {p: sorted(f - found.get(p, set())) for p, f in PROVIDER_FILES.items() if f - found.get(p, set())}
    assert not gone, f"no longer importing providers, remove from PROVIDER_FILES: {gone}"
