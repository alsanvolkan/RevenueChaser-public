"""Fixtures for the end-to-end tier.

Every test here gets a throwaway `.store` + `.cache` and a live dashboard
subprocess pointed at them, with all outbound HTTP routed to a local mock
provider server. Nothing touches the developer's real data or the real internet.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from .harness import mock_provider_server as mps
from .harness.shared import clear_cache_dir, provider_env, running_dashboard


def pytest_collection_modifyitems(config, items):
    """Everything under e2e/ carries the ``e2e`` marker automatically."""
    here = Path(__file__).resolve().parent
    for item in items:
        if here in Path(item.fspath).resolve().parents or Path(item.fspath).resolve().parent == here:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(autouse=True)
def isolate_rawstore():
    """Neutralise the root conftest's per-test rawstore redirect.

    The root fixture points ``rawstore.DB_PATH`` at its own ``tmp_path`` so a
    unit test can never see the real store. Here the store under test is the
    harness's temp store, which the dashboard subprocess owns — the test process
    reads it through ``Dashboard.table_count`` by explicit path, not through the
    module global. Overriding keeps the two from disagreeing.
    """
    yield None


@pytest.fixture(scope="session")
def mock_provider():
    """Local stand-in for every HTTP provider, shared across the session."""
    server = mps.MockProviderServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def provider_routes(mock_provider):
    """Fresh default routes + a clean request log for each test."""
    mock_provider.reset()
    mps.default_routes(mock_provider)
    yield mock_provider
    mock_provider.reset()


@pytest.fixture
def e2e_env(provider_routes) -> dict[str, str]:
    """Environment that points every redirectable provider at the mock server."""
    return provider_env(provider_routes.url)


@pytest.fixture
def dead_uw_token(provider_routes):
    """Make the UW auth probe answer 401 — the user's real broken-key state.

    ``UWClient.verify`` treats only 401/403 as a credential verdict, so this is
    the one way to reproduce "the token is set but rejected" and prove which
    boards still work without credentials.
    """
    provider_routes.route("/api/stock/AAPL/info", payload={"error": "unauthorized"},
                          status=401)
    return provider_routes


@pytest.fixture
def e2e_dirs(tmp_path) -> tuple[Path, Path]:
    """A clean (store, cache) pair."""
    store = tmp_path / ".store"
    cache = tmp_path / ".cache"
    store.mkdir()
    cache.mkdir()
    return store, cache


@pytest.fixture
def dashboard(e2e_dirs, e2e_env, tmp_path):
    """A live dashboard subprocess on a throwaway store/cache."""
    store, cache = e2e_dirs
    with running_dashboard(tmp_path, store, cache, e2e_env) as dash:
        yield dash


@pytest.fixture
def clear_cache(dashboard):
    """Delete every rendered artifact — the "empty the cache" step."""
    def _clear() -> None:
        clear_cache_dir(dashboard.cache_dir)
    return _clear


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="run the live tier against real provider APIs")


@pytest.fixture(scope="session")
def live_enabled(request) -> bool:
    return bool(request.config.getoption("--live"))
