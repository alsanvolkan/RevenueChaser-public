"""The root conftest must remove the scratch root it made (#the 917-directory leak).

``_redirect_data_roots()`` points RC_STORE_DIR, RC_CACHE_DIR, RC_EDGAR_DOCS_DIR,
RC_DISCORD_MEDIA_DIR and SPX_GAMMA_DATA_ROOT at a ``tempfile.mkdtemp(prefix="rc-pytest-")`` root at
import time. Nothing used to remove it, so one directory leaked per pytest process.
"""
from __future__ import annotations

import os
from pathlib import Path

import conftest


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
