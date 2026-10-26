"""Fixtures for the end-to-end tier.

Every test here gets a throwaway `.store` + `.cache` and a live dashboard
subprocess pointed at them, with all outbound HTTP routed to a local mock
provider server. Nothing touches the developer's real data or the real internet.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from .harness import mock_provider_server as mps
from .harness.dashboard_driver import Dashboard


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
    """Environment that points every redirectable provider at the mock server.

    ``UNUSUAL_WHALES_API_KEY`` is set to a dummy: the mock server accepts any
    token, and an unset key would trip the credential preflight before a single
    board ran.
    """
    base = provider_routes.url
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
        # core/config.py loads the developer's real .env into the dashboard subprocess, and the Settings
        # readiness card would start the real DiscordChatExporter with --version. A blank value counts as
        # set for load_dotenv, so the real ones never come through; the Discord tests set their own.
        "RC_DCE_CLI": "",
        "DISCORD_TOKEN": "",
        "RC_DISCORD_GUILD_ID": "",
    }


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
    env = dict(e2e_env)
    env["SPX_GAMMA_DATA_ROOT"] = str(tmp_path / "spx_gamma")
    env["RC_PATREON_DATA_DIR"] = str(tmp_path / "patreon")
    # Every other root the subprocess writes to: inherited, it would be the root conftest's one folder per
    # RUN, which all xdist workers share (and the backup dir would be the host's real platform default).
    env["RC_EDGAR_DOCS_DIR"] = str(tmp_path / "edgar_docs")
    env["RC_DISCORD_MEDIA_DIR"] = str(tmp_path / "discord_media")
    env["RAW_STORE_BACKUP_DIR"] = str(tmp_path / "backups")
    (tmp_path / "patreon").mkdir(exist_ok=True)
    dash = Dashboard(store_dir=store, cache_dir=cache, env=env,
                     log_path=tmp_path / "dashboard.log")
    try:
        dash.start()
        yield dash
    finally:
        dash.stop()


@pytest.fixture
def clear_cache(dashboard):
    """Delete every rendered artifact — the "empty the cache" step."""
    def _clear() -> None:
        for child in dashboard.cache_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    return _clear


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="run the live tier against real provider APIs")


@pytest.fixture(scope="session")
def live_enabled(request) -> bool:
    return bool(request.config.getoption("--live"))
