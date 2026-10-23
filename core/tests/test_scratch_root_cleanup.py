"""The root conftest must remove the scratch root it made (#the 917-directory leak).

``_redirect_data_roots()`` points RC_STORE_DIR, RC_CACHE_DIR, RC_EDGAR_DOCS_DIR,
RC_DISCORD_MEDIA_DIR and SPX_GAMMA_DATA_ROOT at a ``tempfile.mkdtemp(prefix="rc-pytest-")`` root at
import time. Nothing used to remove it, so one directory leaked per pytest process.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import conftest

REPO_ROOT = Path(conftest.__file__).resolve().parent


def test_the_scratch_root_is_recorded_so_it_can_be_removed():
    assert conftest.SCRATCH_ROOT is not None, "the conftest must remember the root it created"
    assert Path(conftest.SCRATCH_ROOT).name.startswith("rc-pytest-")


def test_the_recorded_root_is_the_one_the_env_points_at():
    """A root that is not the one in use would leave the real scratch behind and delete a stranger's."""
    assert Path(os.environ["RC_STORE_DIR"]).parent == Path(conftest.SCRATCH_ROOT)
    assert Path(os.environ["RC_CACHE_DIR"]).parent == Path(conftest.SCRATCH_ROOT)


def test_removing_the_root_removes_the_whole_tree(tmp_path):
    root = tmp_path / "rc-pytest-fake"
    (root / ".store").mkdir(parents=True)
    (root / ".store" / "raw.duckdb").write_bytes(b"x")
    conftest._remove_scratch_root(root)
    assert not root.exists()


def test_removing_a_root_that_is_already_gone_is_silent(tmp_path):
    """The session end must never fail the run over scratch: a killed subprocess may have taken it."""
    conftest._remove_scratch_root(tmp_path / "rc-pytest-never-existed")


def test_only_an_rc_pytest_root_is_ever_removed(tmp_path):
    """A guard against a future edit pointing this at something real."""
    victim = tmp_path / "my-actual-data"
    victim.mkdir()
    (victim / "raw.duckdb").write_bytes(b"x")
    conftest._remove_scratch_root(victim)
    assert victim.exists(), "a directory not named rc-pytest-* must survive"


def test_the_session_hook_is_registered():
    assert hasattr(conftest, "pytest_sessionfinish")


def _child(env: dict, code: str) -> subprocess.CompletedProcess:
    """Run `code` in a fresh interpreter with `env`, with the repo root importable as pytest has it."""
    return subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, r'" + str(REPO_ROOT) + "')\n" + code],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)


def _clean_env() -> dict:
    env = dict(os.environ)
    for k in ("RC_STORE_DIR", "RC_CACHE_DIR", "RC_EDGAR_DOCS_DIR", "RC_DISCORD_MEDIA_DIR",
              "SPX_GAMMA_DATA_ROOT", "RC_PYTEST_SCRATCH_OWNER"):
        env.pop(k, None)
    return env


def test_a_process_that_inherited_the_root_does_not_remove_it():
    """`pytest -n auto` (what CI runs): the xdist controller makes the root and every worker inherits
    RC_STORE_DIR, so a worker removing it at its own session end would pull the scratch out from under
    the workers still running. Only the process that created the root may remove it."""
    env = _clean_env()
    # The controller: it creates the root and exports what a worker would inherit.
    ctrl = _child(env, "import conftest, os; print(os.environ['RC_STORE_DIR']); "
                       "print(os.environ['RC_PYTEST_SCRATCH_OWNER'])")
    assert ctrl.returncode == 0, ctrl.stderr[-800:]
    store_dir, owner_pid = ctrl.stdout.strip().splitlines()[-2:]
    root = Path(store_dir).parent
    root.mkdir(parents=True, exist_ok=True)
    (root / "marker").write_text("other workers are still using this", encoding="utf-8")

    # A worker: same root, same owner, different pid.
    worker = dict(env, RC_STORE_DIR=store_dir, RC_CACHE_DIR=str(root / ".cache"),
                  RC_PYTEST_SCRATCH_OWNER=owner_pid)
    out = _child(worker, "import conftest; conftest.pytest_sessionfinish(None, 0); "
                         "print('owns', conftest.SCRATCH_ROOT)")
    assert out.returncode == 0, out.stderr[-800:]
    assert root.exists(), "a worker removed the root the controller owns"
    assert (root / "marker").exists(), "the other workers' scratch is gone"
    assert "owns None" in out.stdout, "a worker must not claim ownership of an inherited root"
    shutil.rmtree(root, ignore_errors=True)


def test_a_root_handed_over_without_the_owner_marker_is_not_claimed():
    """RC_STORE_DIR set by something that did not leave RC_PYTEST_SCRATCH_OWNER — a hand-rolled
    runner, or a subprocess given a curated environment — belongs to whoever prepared it. Claiming it
    would delete a directory this process did not make."""
    env = _clean_env()
    ctrl = _child(env, "import conftest, os; print(os.environ['RC_STORE_DIR'])")
    assert ctrl.returncode == 0, ctrl.stderr[-800:]
    root = Path(ctrl.stdout.strip().splitlines()[-1]).parent
    root.mkdir(parents=True, exist_ok=True)
    (root / "marker").write_text("prepared by someone else", encoding="utf-8")

    # Note: no RC_PYTEST_SCRATCH_OWNER.
    out = _child(dict(env, RC_STORE_DIR=str(root / ".store")),
                 "import conftest; conftest.pytest_sessionfinish(None, 0); "
                 "print('owns', conftest.SCRATCH_ROOT)")
    assert out.returncode == 0, out.stderr[-800:]
    assert root.exists() and (root / "marker").exists(), "an unmarked root was claimed and removed"
    assert "owns None" in out.stdout
    shutil.rmtree(root, ignore_errors=True)


def test_the_process_that_created_the_root_does_remove_it():
    env = _clean_env()
    out = _child(env, "import conftest, os; r = os.environ['RC_STORE_DIR']; print(r); "
                      "conftest.pytest_sessionfinish(None, 0)")
    assert out.returncode == 0, out.stderr[-800:]
    root = Path(out.stdout.strip().splitlines()[0]).parent
    assert not root.exists(), "the creating process must remove its own root"
