"""The root conftest must remove the scratch root it made, and only the root it owns.

``_redirect_data_roots()`` points RC_STORE_DIR, RC_CACHE_DIR, RC_EDGAR_DOCS_DIR
and SPX_GAMMA_DATA_ROOT at a ``tempfile.mkdtemp(prefix="rc-pytest-")`` root at
import time. Nothing used to remove it, so one directory leaked per pytest process.

The module under test is reached by file path, never as ``import conftest``: this repo has eight
conftest.py files and the bare name resolves to whichever one sys.path happens to favour. Under
``-n auto`` on CI it picked ``core/identity/tests/conftest.py``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import RevenueChaser

PKG_ROOT = Path(RevenueChaser.__file__).resolve().parent
PARENT = PKG_ROOT.parent          # pytest puts this on sys.path, so `RevenueChaser.*` imports work
CONFTEST = PKG_ROOT / "conftest.py"


def _live_conftest():
    """The already-imported root conftest module, whichever name it was registered under.

    pytest loads it as ``conftest`` and the package loads it as ``RevenueChaser.conftest``; both
    objects carry the same state, so the first match is enough. Matching on ``__file__`` is what
    keeps this off the other seven conftest.py files.
    """
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f and Path(f).resolve() == CONFTEST:
            return mod
    raise AssertionError(f"the root conftest ({CONFTEST}) is not in sys.modules")


def _child(env: dict, code: str) -> subprocess.CompletedProcess:
    """Run `code` in a fresh interpreter with the root conftest imported as ``conftest``.

    A faithful stand-in for an xdist worker, which is a subprocess inheriting the controller's
    environment. The import goes through the package so it cannot land on another conftest.py.
    """
    preamble = ("import sys; sys.path.insert(0, r'" + str(PARENT) + "')\n"
                "from RevenueChaser import conftest\n")
    return subprocess.run([sys.executable, "-c", preamble + code],
                          capture_output=True, text=True, env=env, cwd=str(PARENT), timeout=180)


def _clean_env() -> dict:
    env = dict(os.environ)
    for k in ("RC_STORE_DIR", "RC_CACHE_DIR", "RC_EDGAR_DOCS_DIR",
              "SPX_GAMMA_DATA_ROOT", "RC_PYTEST_SCRATCH_OWNER"):
        env.pop(k, None)
    return env


# ── in-process: what this session's own conftest recorded ─────────────────────

def _inherited() -> bool:
    """True when this process was handed a root someone else created — every xdist worker, so this is
    the normal case in CI (`-n auto`) and the other case in a plain local run."""
    return os.environ.get("RC_PYTEST_SCRATCH_OWNER") != str(os.getpid())


def test_the_scratch_root_is_recorded_when_this_process_created_it():
    """Either this process made the root and remembers it, or it inherited one and records None.
    Both are asserted, because which one holds depends on whether xdist is in play."""
    conftest = _live_conftest()
    if _inherited():
        assert conftest.SCRATCH_ROOT is None, "an inherited root must not be claimed for removal"
        if os.environ.get("RC_PYTEST_SCRATCH_OWNER"):
            # Inherited from another conftest (an xdist worker), so it is one of ours.
            assert Path(os.environ["RC_STORE_DIR"]).parent.name.startswith("rc-pytest-")
        # No marker means a developer pointed RC_STORE_DIR at their own directory. It can be named
        # anything; all that matters is that the conftest did not claim it above.
    else:
        assert conftest.SCRATCH_ROOT is not None, "the conftest must remember the root it created"
        assert Path(conftest.SCRATCH_ROOT).name.startswith("rc-pytest-")


def test_the_recorded_root_is_the_one_the_env_points_at():
    """A root that is not the one in use would leave the real scratch behind and delete a stranger's."""
    conftest = _live_conftest()
    if _inherited():
        assert conftest.SCRATCH_ROOT is None
        return
    assert Path(os.environ["RC_STORE_DIR"]).parent == Path(conftest.SCRATCH_ROOT)
    assert Path(os.environ["RC_CACHE_DIR"]).parent == Path(conftest.SCRATCH_ROOT)


def test_removing_the_root_removes_the_whole_tree(tmp_path):
    root = tmp_path / "rc-pytest-fake"
    (root / ".store").mkdir(parents=True)
    (root / ".store" / "raw.duckdb").write_bytes(b"x")
    _live_conftest()._remove_scratch_root(root)
    assert not root.exists()


def test_removing_a_root_that_is_already_gone_is_silent(tmp_path):
    """The session end must never fail the run over scratch: a killed subprocess may have taken it."""
    _live_conftest()._remove_scratch_root(tmp_path / "rc-pytest-never-existed")


def test_only_an_rc_pytest_root_is_ever_removed(tmp_path):
    """A guard against a future edit pointing this at something real."""
    victim = tmp_path / "my-actual-data"
    victim.mkdir()
    (victim / "raw.duckdb").write_bytes(b"x")
    _live_conftest()._remove_scratch_root(victim)
    assert victim.exists(), "a directory not named rc-pytest-* must survive"


def test_the_session_hook_is_registered():
    assert hasattr(_live_conftest(), "pytest_sessionfinish")


# ── across processes: the xdist ownership rule ────────────────────────────────

def test_a_process_that_inherited_the_root_does_not_remove_it():
    """`pytest -n auto` (what CI runs): the xdist controller makes the root and every worker inherits
    RC_STORE_DIR, so a worker removing it at its own session end would pull the scratch out from under
    the workers still running. Only the process that created the root may remove it."""
    env = _clean_env()
    ctrl = _child(env, "import os; print(os.environ['RC_STORE_DIR']); "
                       "print(os.environ['RC_PYTEST_SCRATCH_OWNER'])")
    assert ctrl.returncode == 0, ctrl.stderr[-1500:]
    store_dir, owner_pid = ctrl.stdout.strip().splitlines()[-2:]
    root = Path(store_dir).parent
    root.mkdir(parents=True, exist_ok=True)
    (root / "marker").write_text("other workers are still using this", encoding="utf-8")

    worker = dict(env, RC_STORE_DIR=store_dir, RC_CACHE_DIR=str(root / ".cache"),
                  RC_PYTEST_SCRATCH_OWNER=owner_pid)
    out = _child(worker, "conftest.pytest_sessionfinish(None, 0); print('owns', conftest.SCRATCH_ROOT)")
    assert out.returncode == 0, out.stderr[-1500:]
    assert root.exists(), "a worker removed the root the controller owns"
    assert (root / "marker").exists(), "the other workers' scratch is gone"
    assert "owns None" in out.stdout, "a worker must not claim ownership of an inherited root"
    shutil.rmtree(root, ignore_errors=True)


def test_a_root_handed_over_without_the_owner_marker_is_not_claimed():
    """RC_STORE_DIR set by something that did not leave RC_PYTEST_SCRATCH_OWNER — a hand-rolled
    runner, or a subprocess given a curated environment — belongs to whoever prepared it. Claiming it
    would delete a directory this process did not make."""
    env = _clean_env()
    ctrl = _child(env, "import os; print(os.environ['RC_STORE_DIR'])")
    assert ctrl.returncode == 0, ctrl.stderr[-1500:]
    root = Path(ctrl.stdout.strip().splitlines()[-1]).parent
    root.mkdir(parents=True, exist_ok=True)
    (root / "marker").write_text("prepared by someone else", encoding="utf-8")

    # Note: no RC_PYTEST_SCRATCH_OWNER.
    out = _child(dict(env, RC_STORE_DIR=str(root / ".store")),
                 "conftest.pytest_sessionfinish(None, 0); print('owns', conftest.SCRATCH_ROOT)")
    assert out.returncode == 0, out.stderr[-1500:]
    assert root.exists() and (root / "marker").exists(), "an unmarked root was claimed and removed"
    assert "owns None" in out.stdout
    shutil.rmtree(root, ignore_errors=True)


def test_the_process_that_created_the_root_does_remove_it():
    env = _clean_env()
    out = _child(env, "import os; print(os.environ['RC_STORE_DIR']); "
                      "conftest.pytest_sessionfinish(None, 0)")
    assert out.returncode == 0, out.stderr[-1500:]
    root = Path(out.stdout.strip().splitlines()[0]).parent
    assert not root.exists(), "the creating process must remove its own root"
