"""Dashboards for the e2e fixtures: one per test (``conftest.dashboard``) or one shared by read-only tests.

A test module whose tests only read what one Fetch left behind shares one dashboard and one Fetch through
``shared_dashboard`` in a module- or class-scoped fixture, instead of booting a dashboard and fetching again for
every test. Under ``pytest -n 4 --dist loadgroup`` such a module marks its shared tests with one
``xdist_group``, so they land on one worker and the Fetch really runs once.
"""
from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from . import mock_provider_server as mps
from .dashboard_driver import Dashboard


def provider_env(base: str) -> dict[str, str]:
    """Environment that points every redirectable provider at the mock server at ``base``.

    ``UNUSUAL_WHALES_API_KEY`` is set to a dummy: the mock server accepts any
    token, and an unset key would trip the credential preflight before a single
    board ran.
    """
    return {
        "RC_E2E": "1",
        "RC_UW_BASE": f"{base}/api",
        "RC_UW_BASE_ROOT": base,
        "RC_SEC_WWW_BASE": base,
        "RC_SEC_DATA_BASE": base,
        "RC_FORM13F_INDEX_URL": f"{base}/data-research/sec-markets-data/form-13f-data-sets",
        # identity master (issue #85): every source adapter reads its own RC_<NAME>_BASE
        "RC_OPENFIGI_BASE": base,
        "RC_NASDAQTRADER_BASE": f"{base}/dynamic/SymDir",
        "RC_FDIC_BASE": base,
        "RC_WIKIPEDIA_BASE": base,
        "RC_SPDR_BASE": f"{base}/spdr",
        "OPENFIGI_API_KEY": "",
        "RC_FRED_BASE": f"{base}/fred/series/observations",
        "RC_CFTC_BASE": f"{base}/resource/udgc-27he.json",
        "UNUSUAL_WHALES_API_KEY": "e2e-test-token",
        # DeepSeek: the mock host, a dummy key (so .env's real key is never read) and the
        # off-peak gate open (so no test depends on the clock).
        "RC_DEEPSEEK_BASE": base,
        "DEEPSEEK_API_KEY": "e2e-test-token",
        "RC_DEEPSEEK_OFF_PEAK_ONLY": "0",
        # FRED short-circuits when its key is absent; keep it absent so a missing
        # fixture cannot silently reach the real internet.
        "FRED_API_KEY": "",
    }



@contextmanager
def running_dashboard(root: Path, store: Path, cache: Path, base_env: dict[str, str]) -> Iterator[Dashboard]:
    """A dashboard subprocess whose every writable root sits under ``root``; stopped on exit."""
    env = dict(base_env)
    env["SPX_GAMMA_DATA_ROOT"] = str(root / "spx_gamma")
    env["RC_PATREON_DATA_DIR"] = str(root / "patreon")
    # Every other root the subprocess writes to: inherited, it would be the root conftest's one folder per
    # RUN, which all xdist workers share (and the backup dir would be the host's real platform default).
    env["RC_EDGAR_DOCS_DIR"] = str(root / "edgar_docs")
    env["RAW_STORE_BACKUP_DIR"] = str(root / "backups")
    (root / "patreon").mkdir(exist_ok=True)
    dash = Dashboard(store_dir=store, cache_dir=cache, env=env,
                     log_path=root / "dashboard.log")
    try:
        dash.start()
        yield dash
    finally:
        dash.stop()


@contextmanager
def shared_dashboard(tmp_path_factory, mock_provider, name: str) -> Iterator[Dashboard]:
    """One dashboard for several read-only tests (a module- or class-scoped fixture builds on this).

    Its own store and cache, the default provider routes at the start; the caller seeds it and runs its Fetch
    once. The tests that share it may only read: a test that changes the store, the routes or the cache in a
    way a later test could see takes the function-scoped ``dashboard`` instead.
    """
    root = tmp_path_factory.mktemp(name)
    store, cache = root / ".store", root / ".cache"
    store.mkdir()
    cache.mkdir()
    mock_provider.reset()
    mps.default_routes(mock_provider)
    with running_dashboard(root, store, cache, provider_env(mock_provider.url)) as dash:
        yield dash


@contextmanager
def store_of(dashboard: Dashboard) -> Iterator[None]:
    """``rawstore.DB_PATH`` at ``dashboard``'s store for the block (the test process reads and seeds through it)."""
    from RevenueChaser.core import rawstore
    mp = pytest.MonkeyPatch()
    mp.setattr(rawstore, "DB_PATH", dashboard.store_dir / "raw.duckdb")
    try:
        yield
    finally:
        mp.undo()


def clear_cache_dir(cache_dir: Path) -> None:
    """Delete every rendered artifact — the "empty the cache" step."""
    for child in cache_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
