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
    "module_asymmetric_growth", "module_comovement", "module_financials", "module_fundamentals", "module_leaps",
    "module_oipd", "module_relatedness", "module_sec_13f", "module_sector_rotation",
    "module_spx_gamma", "module_swing_potential", "module_uw_interaction", "module_value_chain",
})


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
