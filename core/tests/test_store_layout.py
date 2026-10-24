"""``.store/`` vs ``.cache/`` — the invariant docs/data_architecture.md promises.

- ``.store/`` is DURABLE and holds exactly two DuckDB files: ``raw.duckdb``
  (structured raw + derived data) and ``edgar.duckdb`` (raw 10-K/20-F documents).
- ``.cache/`` (``OUTPUT_DIR``) holds every regenerable artifact: the rendered
  ``*_latest.html`` boards, their JSON payloads, PNGs, downloaded 13F zips.

The LEAPS board silently broke this: ``render_leaps_dashboard`` still built its
path from ``DATA_DIR`` after the split, so the HTML landed in ``.store/`` while
``app/dashboard.py`` looked for it in ``.cache/`` — the board rendered its empty
state forever. The static guard below is what catches the next one, because a
per-board test only covers the boards someone remembered to write.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from RevenueChaser.core import config

ROOT = Path(__file__).resolve().parent.parent.parent

# Modules that may legitimately name DATA_DIR: the two store files live there.
# This repository carries no research scripts, so the four `scripts/research/*` entries this set used to
# exempt (the #207, #429, #375 and #379 dry runs, each of which read DATA_DIR only to refuse running against
# the live store) are gone with them.
_DATA_DIR_ALLOWED = {"core/config.py", "core/rawstore.py", "core/edgar_store.py"}

_MODULE_DIRS = ["app", "core", "main.py", "module_asymmetric_growth", "module_earnings_radar",
                "module_financials", "module_fundamentals",
                "module_leaps", "module_sec_13f", "module_sector_rotation",
                "module_swing_potential", "scripts"]


def _source_files():
    for name in _MODULE_DIRS:
        p = ROOT / name
        if p.is_file():
            yield p
        else:
            for f in p.rglob("*.py"):
                if "tests" not in f.parts:
                    yield f


def test_no_module_builds_paths_from_data_dir():
    """Only the raw store may live under ``.store/``.

    Any other ``DATA_DIR``-derived path is an artifact being written into the
    folder the docs say must never be deleted — and, since the dashboard serves
    from ``OUTPUT_DIR``, one the app will never find again.
    """
    offenders = []
    for f in _source_files():
        rel = f.relative_to(ROOT).as_posix()
        if rel in _DATA_DIR_ALLOWED:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8-sig").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # whole-word: LEGACY_DATA_DIR (the migration script's rawData/) and
            # PATREON_DATA_DIR (an external tree) are unrelated symbols
            if re.search(r"\b(RAW_)?DATA_DIR\b", line):
                offenders.append(f"{rel}:{i}: {stripped}")
    assert not offenders, (
        "artifacts must be written under OUTPUT_DIR (.cache/), not DATA_DIR (.store/):\n"
        + "\n".join(offenders))


def test_store_dir_is_only_ever_the_duckdb_file():
    """``DATA_DIR`` is the parent of ``raw.duckdb`` and holds nothing else."""
    from RevenueChaser.core import rawstore
    assert rawstore.DB_PATH.name == "raw.duckdb"
    assert config.DATA_DIR.name == ".store"
    assert config.OUTPUT_DIR.name == ".cache"
    assert config.CACHE_ROOT == config.OUTPUT_DIR


def test_edgar_docs_dir_is_separate_from_the_store():
    """Raw EDGAR HTML is a durable *file* artifact, deliberately outside .store/
    (which stays DuckDB-only) and outside .cache/ (it is not regenerable)."""
    assert config.EDGAR_DOCS_DIR.name == "edgar_docs"
    assert config.DATA_DIR not in config.EDGAR_DOCS_DIR.parents
    assert config.OUTPUT_DIR not in config.EDGAR_DOCS_DIR.parents


# The durable DuckDB files plus the sidecars DuckDB/the migration create
# beside them: raw.duckdb (structured data), edgar.duckdb (raw EDGAR documents)
# and prices.duckdb (the daily price tables, #156).
_ALLOWED_IN_STORE = {
    "raw.duckdb", "raw.duckdb.wal", "raw.duckdb.tmp", "raw.duckdb.new",
    "edgar.duckdb", "edgar.duckdb.wal", "edgar.duckdb.tmp", "edgar.duckdb.new",
    "prices.duckdb", "prices.duckdb.wal",
}


def test_the_real_store_dir_contains_nothing_but_the_database():
    """Checks the developer's ACTUAL ``.store/``, not a temp one.

    The static guards below catch a bad path in the source; this catches an
    artifact that a past run already dropped in there — which is how the stale
    ``.store/leaps_latest.html`` survived the source fix.

    Resolved from ``BASE_DIR`` rather than ``config.DATA_DIR`` on purpose: the
    suite now redirects ``DATA_DIR`` to a temp root (see conftest), and pointing
    this at the redirected root would make it inspect an empty directory and
    assert nothing.
    """
    store = config.BASE_DIR / ".store"
    if not store.exists():
        pytest.skip("no .store/ yet")
    stray = sorted(p.name for p in store.iterdir()
                   if p.name not in _ALLOWED_IN_STORE and not p.name.startswith("."))
    assert not stray, (
        f"{store} must hold only raw.duckdb — found {stray}. Regenerable artifacts "
        f"belong in {config.OUTPUT_DIR}; delete these (they are not durable data).")


def test_the_suite_never_points_at_the_developers_real_edgar_docs():
    """conftest must redirect RC_EDGAR_DOCS_DIR away from the real tree: backup tests zip it."""
    assert config.EDGAR_DOCS_DIR != config.BASE_DIR / "edgar_docs"
    assert config.EDGAR_DOCS_DIR.parent == config.DATA_DIR.parent


# Every rendered board: the renderer's own return value must land under .cache/,
# and it must be the exact path app/dashboard.py serves for that tab.
def _render_leaps():
    from RevenueChaser.module_leaps.report import render_leaps_dashboard
    snap = {"ticker": "NOK", "name": "Nokia", "label": "Quiet", "score": 10,
            "frequency": 1, "notional": 1000.0, "net_notional": 100.0,
            "ask_share": 0.5, "near_term_event": False}
    return render_leaps_dashboard([], [snap], filename="leaps_latest.html")


class TestRenderedBoardsLandInCache:
    def test_leaps_dashboard_is_written_under_cache(self, tmp_path, monkeypatch):
        import RevenueChaser.module_leaps.report as lr
        monkeypatch.setattr(lr, "OUTPUT_DIR", tmp_path)
        path = _render_leaps()
        assert path.exists()
        assert path.parent == tmp_path, f"LEAPS artifact escaped OUTPUT_DIR: {path}"

    def test_leaps_dashboard_path_matches_what_the_dashboard_serves(self):
        """Regression: the renderer wrote ``.store/leaps_latest.html`` while
        ``_module_file("module_leaps")`` looked in ``.cache/``."""
        from RevenueChaser.app import dashboard, runners
        import RevenueChaser.module_leaps.report as lr
        assert lr.OUTPUT_DIR == runners.OUTPUT_DIR
        assert dashboard._module_file("module_leaps") == runners.LEAPS_LATEST
        assert runners.LEAPS_LATEST.parent == config.OUTPUT_DIR

    @pytest.mark.parametrize("mod", [
        "module_earnings_radar", "module_leaps",
        "module_sector_rotation", "module_sec_13f",
        "module_financials", "module_asymmetric_growth", "module_swing_potential",
        "module_fundamentals",
    ])
    def test_every_served_board_path_is_under_cache(self, mod):
        from RevenueChaser.app import dashboard
        path = dashboard._module_file(mod)
        assert path is not None, f"{mod} has no served artifact path"
        assert config.OUTPUT_DIR in path.parents, f"{mod} is served from {path}, not .cache/"


def test_runner_artifact_constants_are_all_under_cache():
    from RevenueChaser.app import runners
    for name in dir(runners):
        if not (name.endswith("_LATEST") or name.endswith("_JSON") or name.endswith("_CACHE")):
            continue
        value = getattr(runners, name)
        if isinstance(value, Path):
            assert config.OUTPUT_DIR in value.parents, f"runners.{name} = {value} is not under .cache/"


def test_no_module_writes_a_raw_file_where_the_store_should_be_used():
    """Guard: every raw cache goes through rawstore, not back to a JSON/CSV file.

    Resolves path VARIABLES (the old version only matched when the path and the
    write were on the same source line, which is why it missed LEAPS).
    """
    offenders = []
    for f in _source_files():
        rel = f.relative_to(ROOT).as_posix()
        if rel in _DATA_DIR_ALLOWED:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        tainted = set()
        for node in ast.walk(tree):
            # name = <expr mentioning DATA_DIR>
            if isinstance(node, ast.Assign):
                names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                if "DATA_DIR" in names or "RAW_DATA_DIR" in names or tainted & names:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            tainted.add(t.id)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("write_text", "write_bytes", "to_csv", "mkdir"):
                    base = node.func.value
                    if isinstance(base, ast.Name) and base.id in tainted:
                        offenders.append(f"{rel}:{node.lineno}: {base.id}.{node.func.attr}(...)")
    assert not offenders, "raw-file writes under .store/ remain:\n" + "\n".join(offenders)


def test_data_roots_are_redirected_even_when_a_conftest_imports_core_config_first():
    """Order bug: a package conftest passed on the command line (module_comovement's imports
    ``RevenueChaser.core.config``) was imported before the root conftest's ``pytest_configure``
    set the RC_* roots, so ``config`` froze on the developer's real ``edgar_docs`` / ``.store``.
    The redirect must happen when the root conftest is imported, not in ``pytest_configure``."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.startswith("RC_")}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "module_comovement",
         "core/tests/test_store_layout.py", "-k", "the_suite_never_points_at_the_developers_real_edgar_docs"],
        cwd=root, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-500:]
