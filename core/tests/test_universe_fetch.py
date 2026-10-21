"""``core/universe_fetch.py``: the universe writer lives in core, not in a board module (#146, from #217)."""
from __future__ import annotations

import ast
from pathlib import Path

from RevenueChaser.core import universe_fetch as uf

# Names the Asymmetric Growth engine still exposes (callers and tests import them from there).
MOVED = ("fetch_universe_stats", "fetch_universe", "backfill_universe_caps", "_save_universe",
         "_save_universe_meta", "_load_stats_meta", "US_EXCHANGES", "_UNIVERSE_META_KEY")
# Names that moved and are not re-exported: a patch at the old place would silently do nothing, so the engine
# must not carry them any more (the tests patch them on ``core.universe_fetch``).
MOVED_ONLY = ("_apply_identity_master", "_save_stats_meta", "_sec_member_list", "_join_members", "_screener_query",
              "MC_CHUNKS", "MIN_FULL_UNIVERSE", "SEC_LIST_EXCHANGES", "UNIVERSE_MIN_MC", "UNIVERSE_MAX_MC",
              "_CAP_BACKFILL_SLEEP", "_CAP_BACKFILL_WORKERS", "_CAP_BACKFILL_ATTEMPTS", "_CAP_BACKFILL_BACKOFF",
              "_RATE_LIMIT_STREAK_STOP", "_UNIVERSE_STATS_KEY")


def test_the_engine_re_exports_the_moved_names_unchanged():
    from RevenueChaser.module_asymmetric_growth import engine
    for name in MOVED:
        assert getattr(engine, name) is getattr(uf, name), name


def test_the_moved_only_names_exist_in_core_and_not_in_the_engine():
    from RevenueChaser.module_asymmetric_growth import engine
    for name in MOVED_ONLY:
        assert hasattr(uf, name), f"core.universe_fetch lacks {name}"
        assert not hasattr(engine, name), f"the engine still carries {name}"


def test_the_swing_and_fundamentals_wrappers_call_the_core_writer(monkeypatch):
    from RevenueChaser.module_fundamentals import pipeline as fp
    from RevenueChaser.module_swing_potential import pipeline as sp
    calls = []
    monkeypatch.setattr(uf, "fetch_universe_stats", lambda **kw: calls.append(kw) or ([], {}))
    for mod in (fp, sp):
        mod.fetch_universe_stats(refreshed_by="X", refresh=True, no_fetch=False)
    assert [c["refreshed_by"] for c in calls] == ["X", "X"]


def test_the_core_writer_imports_no_board_module():
    """Core never depends on a ``module_*`` package (core -> app -> module_* is the one direction)."""
    tree = ast.parse(Path(uf.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in imported if ".module_" in m or m.startswith("module_") or ".app" in m], imported


def test_the_engine_no_longer_defines_the_writer():
    """The functions exist once, in core: the engine's names are imports, not copies."""
    from RevenueChaser.module_asymmetric_growth import engine
    tree = ast.parse(Path(engine.__file__).read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not defined & (set(MOVED) | set(MOVED_ONLY)), defined & (set(MOVED) | set(MOVED_ONLY))
    assigned = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not assigned & (set(MOVED) | set(MOVED_ONLY)), assigned & (set(MOVED) | set(MOVED_ONLY))


def test_the_app_runner_imports_the_backfill_from_core():
    src = Path(__file__).resolve().parents[2].joinpath("app", "runners.py").read_text(encoding="utf-8")
    assert "from RevenueChaser.core import universe_fetch" in src
    assert "module_asymmetric_growth.engine import backfill_universe_caps" not in src
