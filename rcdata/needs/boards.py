"""Which board reads which need, declared by the board itself.

The Settings / Data screen draws a flow chart of what feeds what. Drawing it needs an answer to "which board asks
for this need", and the only place that answer can be kept honest is the board itself: a board declares ``READS``
in its package ``__init__``, and ``UNDECLARED`` names the boards that have not migrated to the API yet. A board
package that is in neither raises in CI, so a new board cannot slip in without saying what it reads.

The declaration is checked against the board's own source rather than trusted: ``undeclared_uses`` reads the
package's ``.py`` files for need names appearing as string literals. That match is exact — a quoted string either is
a name in the catalog or it is not — which is why the check is done this way and not by looking for table names,
where a search gives false positives (a board reading an old table name, a word that is also a column).

This PR lands the mechanism and its test only (avo's plan, 29 Oct 2026). ``UNDECLARED`` shrinks by one entry per
board as the migration reaches it, and goes away with the last one.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from . import catalog
from .serve import UnknownNeed

#: A dashboard lives in a top-level package with this prefix.
BOARD_PREFIX = "module_"
#: Packages that carry the prefix without being a dashboard.
#: ``module_testing`` is the e2e tier. ``module_settings`` is the control screen: it names needs to say what the
#: catalog holds and what a cross-check covers, which is not a board reading a need's rows, so the declaration
#: would say something untrue about it.
NOT_A_BOARD = frozenset({"module_testing", "module_settings"})
#: Boards whose reads are not declared yet, because they have not migrated to the API. Each entry goes away in the
#: PR that migrates that board, which is also the PR that adds its ``READS``.
UNDECLARED = frozenset({
    "module_asymmetric_growth", "module_financials", "module_fundamentals", "module_leaps",
    "module_news", "module_oipd", "module_relatedness", "module_sec_13f", "module_sector_rotation",
    "module_swing_potential", "module_uw_interaction", "module_value_chain",
})

#: What each board not migrated yet will read: first from the catalog's need list of 29 Oct 2026
#: (``DataPlan/2026-10-29_katalog-need-listesi.md``, its "which board reads it" column), since corrected from the
#: boards' code (#489, #490, #491). The control board draws
#: these as dashed lines, so a reader can tell a board's own declaration from the plan. An entry goes away in the
#: PR that migrates the board, the one that adds its ``READS``. A board whose data has no need in the catalog yet
#: is left out rather than given a need it does not read. A test compares each entry with the ``rcdata.read``
#: calls of the board's own package (``unplanned_reads``, #491), so a read the code makes cannot go missing again.
PLANNED_READS: dict[str, tuple[str, ...]] = {
    "module_oipd": ("macro.dgs10", "options.chain", "options.strikes", "options.expiries"),
    "module_financials": ("identity.universe", "prices.bars", "prices.shares", "prices.market_cap", "prices.latest",
                          "macro.dgs10", "fundamentals.statements", "fundamentals.segments", "fundamentals.peers"),
    "module_uw_interaction": ("options.prints",),
    # #400 WP5: the XBRL customers are named by the spine's ``security`` names (``identity.security``).
    "module_value_chain": ("identity.universe", "identity.security", "fundamentals.companies",
                           "fundamentals.filings", "fundamentals.segments", "fundamentals.customers"),
    # #642 PR 4: the industry tree reads the shared universe (spine names/sectors, prices-store caps) and the theme
    # rows the spine's whole ETF holdings.
    "module_sector_rotation": ("identity.membership", "identity.universe", "identity.etf_holdings", "prices.bars",
                               "prices.latest", "market.series"),
    # #481: the 13F share's split factor reads ``prices.security_ids`` and ``prices.bars`` (basis "split").
    "module_asymmetric_growth": ("identity.universe", "prices.bars", "prices.security_ids",
                                 "fundamentals.statements", "fundamentals.companies", "fundamentals.earnings_event",
                                 "ownership.beneficial", "earnings.next"),
    "module_swing_potential": ("identity.universe", "prices.bars", "earnings.next"),
    "module_fundamentals": ("identity.universe", "prices.bars", "fundamentals.statements", "fundamentals.companies",
                            "fundamentals.filings", "earnings.next"),
    "module_sec_13f": ("ownership.filer_breaks_13f", "ownership.holders_13f", "ownership.periods_13f",
                       "ownership.type_holdings_13f", "prices.actions"),
    "module_leaps": ("options.daily",),
    # 11 Nov 2026: the calendar build reads the builder's economic calendar, not raw.economic_calendar.
    "module_news": ("market.economic_calendar",),
    # #490: ``engine.py`` reads the company records and the served filing texts.
    # #400 WP6: the B input adds the filing's XBRL major customers (``fundamentals.customers``).
    # ``identity.etf_holdings`` (#642 PR 2b): the theme-to-ETF ranks read the spine's holdings (D4, 9 Nov 2026).
    "module_relatedness": ("identity.universe", "identity.security", "identity.etf_holdings", "fundamentals.companies",
                           "fundamentals.filings", "fundamentals.segments", "fundamentals.customers"),
}


def root() -> Path:
    """The package root, the directory the board packages sit in."""
    return Path(__file__).resolve().parents[2]


def board_packages() -> tuple[str, ...]:
    return tuple(sorted(p.name for p in root().iterdir()
                        if p.is_dir() and p.name.startswith(BOARD_PREFIX) and p.name not in NOT_A_BOARD
                        and (p / "__init__.py").exists()))


def declared_reads(board: str) -> tuple[str, ...] | None:
    """The board's own declaration, or None when it has not got one.

    Read from the source rather than by importing the package: collecting the record must not pull sixteen boards'
    imports into whatever asked, and a declaration is a literal tuple of strings by construction.
    """
    text = (root() / board / "__init__.py").read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == "READS" for t in targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            return tuple(str(v) for v in value)
    return None


def all_reads() -> dict[str, tuple[str, ...]]:
    found = {b: declared_reads(b) for b in board_packages()}
    return {b: reads for b, reads in found.items() if reads is not None}


def readers_of(need: str) -> tuple[str, ...]:
    if need not in catalog.NEEDS:
        raise UnknownNeed(need)
    return tuple(b for b, reads in sorted(all_reads().items()) if need in reads)


def _quoted_names(text: str) -> set[str]:
    """Every string the module's code evaluates (the review's m6).

    Parsed rather than matched: the old regex took any quoted run of characters anywhere in the file, so a need
    named inside a comment or inside a sentence of a docstring read as a use, and the CI guard then asked for a
    declaration of something the board does not read. A comment is not in the tree at all, and a docstring is one
    string whose whole text is never a need name.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def need_literals(board: str) -> tuple[str, ...]:
    """Every catalog need name quoted anywhere in the board's own source, tests left out.

    A test names a need to pin behaviour, which is not the board reading it, so ``tests`` directories are skipped.
    """
    found: set[str] = set()
    for path in sorted((root() / board).rglob("*.py")):
        if "tests" in path.relative_to(root() / board).parts:
            continue
        found.update(n for n in _quoted_names(path.read_text(encoding="utf-8")) if n in catalog.NEEDS)
    return tuple(sorted(found))


def undeclared_uses(board: str) -> tuple[str, ...]:
    declared = set(declared_reads(board) or ())
    return tuple(name for name in need_literals(board) if name not in declared)


# ── The plan checked against the code (#491) ───────────────────────────────────────────────────────────────────
# ``PLANNED_READS`` is written by hand, and #483 found it had drifted: three boards read data the diagram drew no
# line for. These read the board's own source instead, for the ``rcdata.read`` functions it calls, and map each
# function to the catalog tables it queries, so a test can name a read the plan is missing.

#: The ``rcdata.read`` helpers a reader passes its table to, as the first argument.
_ROW_HELPERS = frozenset({"_rows", "_frame", "_option_rows"})
#: A table a reader's SQL names. A CTE's name matches too, and is dropped because it is no catalog table.
_SQL_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)")


def reader_tables() -> dict[str, set[str]]:
    """``{function: catalog tables it queries}`` for every top-level function of ``rcdata/read.py``.

    A table counts when the function hands it to a row helper or names it in its own SQL text: ``changed_at``
    passes ``price_bar_daily`` to ``_rows`` but also reads ``corporate_action`` inside the query (#489). Only
    tables some need is over are kept; a helper function's own SQL (``_now_cte``'s symbol lookup) is the helper's.
    """
    tables = {n.table for n in catalog.NEEDS.values()}
    tree = ast.parse((Path(__file__).resolve().parents[1] / "read.py").read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        found: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                found.update(_SQL_TABLE.findall(sub.value))
            elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in _ROW_HELPERS
                  and sub.args and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str)):
                found.add(sub.args[0].value)
        out[node.name] = found & tables
    return out


def _board_sources(board: str):
    for path in sorted((root() / board).rglob("*.py")):
        if "tests" not in path.relative_to(root() / board).parts:
            yield path


def read_calls(board: str) -> tuple[str, ...]:
    """The ``rcdata.read`` functions the board's own package uses, tests left out.

    Found by the imports: ``from ...rcdata import read [as x]`` then ``x.<function>``, or
    ``from ...rcdata.read import <function>``. A name ``read`` that was not imported from ``rcdata`` is not one.
    """
    found: set[str] = set()
    for path in _board_sources(board):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "rcdata" or node.module.endswith(".rcdata"):
                    aliases.update(a.asname or a.name for a in node.names if a.name == "read")
                elif node.module == "rcdata.read" or node.module.endswith(".rcdata.read"):
                    found.update(a.name for a in node.names if a.name != "*")
            elif isinstance(node, ast.Import):
                aliases.update(a.asname for a in node.names if a.asname and a.name.endswith("rcdata.read"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases
                    and not node.attr.startswith("_")):
                found.add(node.attr)
    return tuple(sorted(found))


def _needs_over(table: str) -> tuple[str, ...]:
    return tuple(sorted(n.name for n in catalog.NEEDS.values() if n.table == table))


def unplanned_reads(board: str) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """``(function, table, needs over that table)`` for every table the board's code reads with no planned need.

    When the function is a need's own ``reader_name``, only that need covers it. Otherwise several needs can share
    the table (``corporate_action`` read inside ``changed_at``), and any one of them planned covers the read. A
    reader shared by several needs (``market_series``) is covered by any of them, because the code cannot say
    which series it means without running.
    """
    planned = set(PLANNED_READS.get(board, ()))
    tables = reader_tables()
    out = []
    for fn in read_calls(board):
        # A function that is a need's own reader serves that need, not any need over its table:
        # ``read.latest_market`` is ``prices.latest``, and ``prices.market_cap`` over the same table must not
        # cover it (the review's 1).
        own: dict[str, set[str]] = {}
        for n in catalog.NEEDS.values():
            if n.reader_name == fn:
                own.setdefault(n.table, set()).add(n.name)
        for table in sorted(tables.get(fn, set()) | set(own)):
            candidates = tuple(sorted(own[table])) if table in own else _needs_over(table)
            if not planned & set(candidates):
                out.append((fn, table, candidates))
    return tuple(out)


def unread_plans(board: str) -> tuple[str, ...]:
    """Planned needs whose table no ``rcdata.read`` call in the board's own package queries.

    A warning, not a fault: a board can read a need through a shared ``core`` module, which this does not follow.
    """
    tables = reader_tables()
    read = {t for fn in read_calls(board) for t in tables.get(fn, ())}
    return tuple(n for n in PLANNED_READS.get(board, ()) if catalog.NEEDS[n].table not in read)


def flow_rows() -> tuple[dict, ...]:
    """One row per need, with what the flow chart draws: who reads it, who writes it, and its freshness limit."""
    reads = {b: set(r) for b, r in all_reads().items()}
    return tuple({"need": name,
                  "boards": tuple(b for b in sorted(reads) if name in reads[b]),
                  "dataset": need.dataset,
                  "kind": need.kind,
                  "table": need.table,
                  "max_lag": need.max_lag,
                  "max_lag_unit": need.max_lag_unit}
                 for name, need in sorted(catalog.NEEDS.items()))


def reads_of(board: str) -> dict:
    """``{"declared": bool, "needs": (...)}``: the board's own ``READS`` when it has them, else its planned reads."""
    own = declared_reads(board) if (root() / board / "__init__.py").exists() else None
    if own is not None:
        return {"declared": True, "needs": own}
    return {"declared": False, "needs": PLANNED_READS.get(board, ())}
