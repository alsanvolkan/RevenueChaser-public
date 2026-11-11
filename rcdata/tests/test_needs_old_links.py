"""The boards' links to the old store and to providers may only shrink (#642 PR 1, F6 of the group-1 review).

Group 1 cuts the boards off the old ``.store`` tables (``raw.duckdb``, ``edgar.duckdb``, ``history.duckdb``,
``prices.duckdb``) and off live provider calls, board by board. Nothing stopped a board from gaining a new link while
that happens. Three ceilings do, each read from the boards' own source (tests left out), never trusted:

- ``boards.UNDECLARED`` can lose a board, never gain one.
- **Old tables:** every old-store table a board package names (and ``module_settings``, which ``NOT_A_BOARD``
  hides from the need checks) must be listed in ``OLD_TABLES`` for that package.
- **Shared code:** the same for every file of ``core/`` and ``app/``, in ``SHARED_TABLES``.
- **Providers:** every file in a board package that imports ``core.providers`` must be listed in ``PROVIDER_FILES``.

Every list is exact, so a PR that removes a link also removes its line here and the lists shrink with the work.

**A table is named two ways, and both count** (#642 PR 1b, the 11 Nov 2026 review's R2): inside an SQL text, and
as the first argument of a ``rawstore`` helper that takes a table name (``_TABLE_ARG_FNS``). Only the first was
checked before, and the second is where most of the remaining links turned out to be: SPX Gamma declares its
``READS`` and still reads ``economic_calendar`` through ``rawstore.get_all``; the 13F board reads
``sec13f_enrichment`` and ``sec13f_security_meta``; Relatedness and Value Chain reach ``chain_filings``. A read
through ``core/`` is now followed too, by listing ``core/`` itself, which is what the earlier limit ("``core`` is
#645's") left out: a board can be clean and still reach an old table one call away.

Still not followed: a table whose name is built at run time (an f-string, a variable, a dict lookup). The lists
are a floor, not a proof.
"""
from __future__ import annotations

import ast
import re

from RevenueChaser.core import rawstore
from RevenueChaser.rcdata.needs import boards

#: ``boards.UNDECLARED`` on 11 Nov 2026. Remove a board here when it migrates; never add one.
UNDECLARED_CEILING = frozenset({
    "module_asymmetric_growth", "module_financials", "module_fundamentals", "module_leaps",
    "module_news", "module_oipd", "module_relatedness", "module_sec_13f", "module_sector_rotation",
    "module_swing_potential", "module_uw_interaction", "module_value_chain",
})

#: ``{package: old-store tables it names}`` on 11 Nov 2026 (main b7ae5819), in SQL or as a helper's table
#: argument. Remove a table here in the PR that stops reading it; the list may only shrink.
OLD_TABLES = {
    "module_asymmetric_growth": {"ag_scores_history"},
    "module_earnings_radar": {"earnings_calendar", "earnings_history", "watchlist"},
    "module_financials": {"macro_series", "result_runs", "ticker_payload_cache"},
    "module_leaps": {"leaps_footprint_history"},
    "module_relatedness": {"chain_filings", "chain_mentions", "run_registry"},
    "module_sec_13f": {"price_actions", "price_bars_daily", "sec13f_enrichment", "sec13f_security_meta"},
    "module_settings": {"chain_filings", "edgar_documents", "edgar_financials", "equity_universe",
                        "equity_universe_provenance", "identity_conflicts", "identity_runs",
                        "identity_source_cache", "index_constituents", "index_constituents_changes", "job_runs",
                        "price_bars_daily", "sec_company_sic", "sec_exchange_file", "security_master",
                        "swing_ohlc"},
    # A declared board is not a cut-loose board: SPX Gamma reads the old economic calendar through a helper.
    "module_spx_gamma": {"economic_calendar"},
    "module_swing_potential": {"swing_ohlc"},
    "module_value_chain": {"chain_filings", "chain_mentions", "chain_meta", "edgar_documents", "equity_universe",
                           "sec_company_tickers"},
    # #643 PR 5 took the four market tiles off ``index_levels_daily``, ``vol_index_daily`` and ``macro_series``;
    # the earnings card (``earnings_calendar``) and the freshness card (``job_runs``) are its PR 5b and PR 6.
    "module_welcome": {"earnings_calendar", "job_runs"},
}

#: The shared trees scanned the same way: a board can be clean and still reach an old table one call away.
SHARED_TREES = ("core", "app")
#: ``core/rawstore.py`` is the old store itself - its manifest and accessors name every table by construction,
#: so listing it would record the store's own definition as a link and never shrink.
SHARED_SKIP = frozenset({"core/rawstore.py"})
#: ``{file: old-store tables it names}`` for ``SHARED_TREES`` on 11 Nov 2026 (main b7ae5819). Same promise as
#: ``OLD_TABLES``: a file leaves this list in the PR that takes its last old-table read away.
SHARED_TABLES = {
    "core/automations.py": {"job_runs"},
    "core/company_profile.py": {"equity_universe"},
    "core/earnings_dates.py": {"earnings_calendar"},
    "core/edgar_bulk.py": {"edgar_facts", "sec_company_sic"},
    "core/edgar_store.py": {"edgar_documents"},
    "core/history.py": {"result_runs"},
    "core/identity/apply.py": {"identity_cusip", "security_master"},
    "core/identity/refresh.py": {"equity_universe", "identity_conflicts", "identity_runs",
                                 "identity_source_cache", "sec_company_sic", "sec_submissions",
                                 "security_master"},
    "core/index_levels.py": {"price_bars_daily", "price_checks"},
    "core/index_lists.py": {"index_constituents", "index_constituents_changes"},
    "core/ledger.py": {"swing_ohlc"},
    "core/outcomes.py": {"price_bars_daily"},
    "core/prices.py": {"price_actions", "price_bars_daily", "price_checks"},
    "core/provenance.py": {"chain_filings", "sec13f_totals", "ticker_payload_cache"},
    "core/providers/cache.py": {"ticker_payload_cache"},
    "core/providers/sec_edgar.py": {"sec_company_tickers"},
    "core/quotes.py": {"equity_universe", "source_checks"},
    "core/ticker_store.py": {"store_meta"},
    "core/universe.py": {"equity_universe", "equity_universe_provenance", "security_master"},
    "core/universe_fetch.py": {"equity_universe", "equity_universe_provenance"},
    "app/jobs.py": {"job_runs"},
    "app/runview.py": {"job_runs"},
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
#: ``core.rawstore`` helpers whose first argument is a table name. A call to one of these is a read or a write of
#: that table with no SQL text anywhere, which is how ``rawstore.get("equity_universe_provenance", ...)`` and
#: every link in the 11 Nov 2026 review's R2 stayed invisible to the SQL rule alone.
_TABLE_ARG_FNS = frozenset({"write_rows", "get", "get_many", "get_all", "count", "delete"})


def _packages() -> list[str]:
    return [*boards.board_packages(), "module_settings"]


def _named_tables(tree, old: set[str]) -> set[str]:
    """Every old-store table this module names: in an SQL text, or as a helper's first argument."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.update(t for t in _SQL.findall(node.value) if t in old)
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if (name in _TABLE_ARG_FNS and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str) and node.args[0].value in old):
                found.add(node.args[0].value)
    return found


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
            tables.setdefault(package, set()).update(_named_tables(tree, old))
            if any(_imports_providers(node) for node in ast.walk(tree)):
                providers.setdefault(package, set()).add(rel)
    return ({p: t for p, t in tables.items() if t}, {p: f for p, f in providers.items() if f})


def _scan_shared() -> dict[str, set[str]]:
    """``{file: old-store tables it names}`` for ``SHARED_TREES``, tests and the store module itself left out."""
    old = _old_tables()
    root = boards.root()
    found: dict[str, set[str]] = {}
    for tree_name in SHARED_TREES:
        for path in sorted((root / tree_name).rglob("*.py")):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if "tests" in path.relative_to(root).parts or rel in SHARED_SKIP:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:                       # a file this Python cannot parse is not a silent pass
                raise
            named = _named_tables(tree, old)
            if named:
                found[rel] = named
    return found


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


def test_the_scan_sees_a_table_named_only_as_a_helper_argument():
    """The rule that found most of the remaining links: a table with no SQL text anywhere (the review's R2)."""
    tree = ast.parse('from RevenueChaser.core import rawstore\n'
                     'rawstore.get_all("equity_universe")\n'
                     'rawstore.write_rows("job_runs", [])\n'
                     'other.get_all("not_a_table")\n')
    assert _named_tables(tree, {"equity_universe", "job_runs"}) == {"equity_universe", "job_runs"}


def test_no_shared_module_gains_an_old_table_and_a_removed_one_leaves_the_list():
    """``core/`` and ``app/``: the one call between a clean board and an old table (#642 PR 1b)."""
    found = _scan_shared()
    assert found, "the shared scan found nothing; the tree moved, the links did not"
    new = {f: sorted(t - SHARED_TABLES.get(f, set())) for f, t in found.items() if t - SHARED_TABLES.get(f, set())}
    assert not new, f"new reads or writes of old-store tables in shared code: {new}; read through rcdata.read"
    gone = {f: sorted(t - found.get(f, set())) for f, t in SHARED_TABLES.items() if t - found.get(f, set())}
    assert not gone, f"no longer named, remove from SHARED_TABLES: {gone}"
