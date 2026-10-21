"""Root test configuration — offline enforcement, shared fixtures, HTTP mocking.

The unit suite never touches the network. ``core/providers/*`` are mocked at the
``httpx`` boundary (never at rule-math level), and the shared fixtures under
``core/tests/fixtures/`` provide canned inputs.

Testing a live API is out of scope here: write it as a standalone script under
``core/`` and run it deliberately, never as part of ``pytest``.
"""
from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "core" / "tests" / "fixtures"


def _redirect_data_roots() -> None:
    # Redirect BOTH data roots at a session temp dir before anything imports
    # RevenueChaser. ``isolate_rawstore`` below only ever covered the DuckDB
    # file: every ``*_LATEST`` artifact constant still resolved to the real
    # ``.cache/``, so a route test hitting ``?refresh=1`` unlinked the
    # developer's actual rendered boards and a runner test overwrote them.
    # These are read once, at import, by core/config.py — which is why this has
    # to happen here, when the root conftest is imported, and not in a fixture or in
    # pytest_configure: a package conftest named on the command line (e.g.
    # module_comovement/tests) imports core.config before pytest_configure runs.
    _root = Path(tempfile.mkdtemp(prefix="rc-pytest-"))
    # Named exactly like the real roots so core/tests/test_store_layout.py's
    # "DATA_DIR is the .store folder" guard keeps asserting something.
    os.environ.setdefault("RC_STORE_DIR", str(_root / ".store"))
    os.environ.setdefault("RC_CACHE_DIR", str(_root / ".cache"))
    os.environ.setdefault("RC_EDGAR_DOCS_DIR", str(_root / "edgar_docs"))
    # Backup tests zip this tree; without the redirect they would archive the developer's real one.
    os.environ.setdefault("RC_DISCORD_MEDIA_DIR", str(_root / "discord_media"))
    # Same for the SPX Gamma tree (UW day files, brier.json): the backup mirrors it (#276), and an
    # unredirected test once wrote into the real brier.json (G1). Tests that pass their own root still win.
    os.environ.setdefault("SPX_GAMMA_DATA_ROOT", str(_root / "spx_gamma"))
    # The backup switches are forced, not defaulted: a real off-machine folder in .env or the shell would receive a
    # copy of every test store (#275), and RAW_STORE_BACKUP_WEEKLY=0 would break the weekly-copy tests (#277).
    os.environ["RAW_STORE_BACKUP_MIRROR"] = ""
    os.environ["RAW_STORE_BACKUP_WEEKLY"] = "1"
    # rawstore applies the whole schema manifest on a process's first store
    # call (~1 s on a fresh file). Every test gets a fresh DB, so that would add
    # minutes to the suite; tests that need the manifest call
    # ``ensure_all_tables()`` (see the ``rawstore_tmp_db`` fixture), and
    # core/tests/test_rawstore_storage.py turns auto-ensure back on to test it.
    os.environ.setdefault("RC_STORE_AUTO_ENSURE", "0")


_redirect_data_roots()


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to open a real socket."""


def pytest_configure(config):
    """Isolate the data roots, then make any real network access fail loudly.

    Credentials are always present at run time (``core/config.py`` calls
    ``load_dotenv()`` on import), so nothing stops a stray live call except this.
    Mock at the HTTP boundary instead — see the ``mock_httpx`` fixture below.
    """
    # The end-to-end tier (RC_E2E=1) runs a real dashboard against a real local
    # mock-provider server, so loopback must stay open there. Everything that is
    # not loopback still fails loudly — an e2e test reaching the real internet is
    # as much a bug as a unit test doing it.
    e2e = os.environ.get("RC_E2E") == "1"
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _is_loopback(address) -> bool:
        if not isinstance(address, (tuple, list)) or not address:
            return False
        host = address[0]
        return host in ("127.0.0.1", "::1", "localhost")

    def _blocked(*args, **kwargs):
        raise NetworkAccessAttempted(
            "This test tried to open a real network connection. Unit tests must mock "
            "at the HTTP boundary — use the `mock_httpx` fixture, or patch the client "
            "method directly. Live-API checks belong in a standalone script under "
            "core/, not in the pytest suite."
        )

    def _connect(self, address, *a, **kw):
        if e2e and _is_loopback(address):
            return real_connect(self, address, *a, **kw)
        _blocked()

    def _connect_ex(self, address, *a, **kw):
        if e2e and _is_loopback(address):
            return real_connect_ex(self, address, *a, **kw)
        _blocked()

    def _create_connection(address, *a, **kw):
        if e2e and _is_loopback(address):
            return real_create_connection(address, *a, **kw)
        _blocked()

    def _getaddrinfo(host, *a, **kw):
        if e2e and host in ("127.0.0.1", "::1", "localhost"):
            return real_getaddrinfo(host, *a, **kw)
        _blocked()

    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.create_connection = _create_connection
    socket.getaddrinfo = _getaddrinfo


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Path to ``core/tests/fixtures/`` (uw/edgar/yfinance replay live here later)."""
    return FIXTURES_DIR


@pytest.fixture
def golden_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "golden"


@pytest.fixture(autouse=True)
def isolate_rawstore(tmp_path):
    """Point the rawstore DB at a per-test temp file — for EVERY test.

    Autouse on purpose: now that the JSON/CSV/SQLite stores all live in one
    ``.store/raw.duckdb``, a test that forgets to ask for isolation reads (and
    can write) the developer's real 500 MB store. Kept **per-test** (never
    per-session): a shared DuckDB file across parallel workers would hit the
    single-writer lock.
    """
    from RevenueChaser.core import rawstore
    mp = pytest.MonkeyPatch()  # its own instance: a test's monkeypatch.undo()
    mp.setattr(rawstore, "DB_PATH", tmp_path / "raw.duckdb")  # must not un-isolate
    try:
        yield rawstore.DB_PATH
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def isolate_uw_verify_cache(monkeypatch):
    """The UW client trusts a verified key for 60 s process-wide; never across tests."""
    from RevenueChaser.core.providers import unusual_whales
    monkeypatch.setattr(unusual_whales, "_VERIFIED_OK", {})
    # PL1 retry backoff: never really sleep in a test (a test that cares about
    # the delays injects ``sleep=`` or patches ``_sleep`` itself).
    monkeypatch.setattr(unusual_whales, "_sleep", lambda s: None)


@pytest.fixture(autouse=True)
def isolate_sec_breaker():
    """The SEC circuit breaker is process-wide state; one test must not open it for the next."""
    from RevenueChaser.core.providers import sec_edgar
    sec_edgar.reset_breaker()
    yield
    sec_edgar.reset_breaker()


@pytest.fixture(autouse=True)
def isolate_yf_session(monkeypatch):
    """The yfinance 401 reset (#402) keeps process-wide "seen" and "reset" times; one test must not arm the next."""
    from RevenueChaser.core.providers import yf_session
    monkeypatch.setattr(yf_session, "_STATE", {"seen": float("-inf"), "reset": float("-inf")})


@pytest.fixture(autouse=True)
def isolate_discord_env(monkeypatch):
    """Never start (or read the credentials of) a developer's real Discord setup from a test.

    ``core/config.py`` runs ``load_dotenv()``, so a developer's ``.env`` puts a real ``RC_DCE_CLI``,
    ``DISCORD_TOKEN`` and ``RC_DISCORD_GUILD_ID`` into every test process, and ``inventory.collect()`` (the
    Settings readiness card) would run the real exe with ``--version``. A test that needs them sets its own.
    The version probe's per-file cache is process-wide, so every test starts with an empty one.
    """
    from RevenueChaser.module_discord import exporter
    for name in ("RC_DCE_CLI", "DISCORD_TOKEN", "RC_DISCORD_GUILD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(exporter, "_VERSION_CACHE", {})


@pytest.fixture(autouse=True)
def isolate_deepseek_gate(tmp_path, monkeypatch):
    """Never read the real Hermes peak-override file from a test.

    If the user flipped "run at peak" on the card, the real file would make every
    peak-gate test pass or fail depending on the time of day.
    """
    monkeypatch.setenv("DEEPSEEK_GATE_FILE", str(tmp_path / "deepseek_gate.json"))


@pytest.fixture
def rawstore_tmp_db(isolate_rawstore):
    """The isolated DB path, with every manifest table created."""
    from RevenueChaser.core import rawstore
    rawstore.ensure_all_tables()
    return isolate_rawstore


@pytest.fixture
def db_conn(rawstore_tmp_db):
    """Fresh connection to the rawstore tmp DB."""
    from RevenueChaser.core import rawstore
    with rawstore.connect() as conn:
        yield conn


class _MockResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}
        self.text = payload if isinstance(payload, str) else ""
        if isinstance(payload, str):
            self.content = payload.encode("utf-8")
        else:
            import json as _json
            self.content = _json.dumps(payload).encode("utf-8")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://mock"),
                response=self,
            )

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def mock_httpx(monkeypatch):
    """Mock ``httpx.get`` at the HTTP boundary.

    Usage::

        add = mock_httpx
        add("/api/v3/sp500_constituent", [{"symbol": "AAPL"}])
        add("data.sec.gov", {"facts": ...})            # substring match

    Unknown URLs return ``200 []``.
    """
    routes: list[tuple[str, object, int]] = []

    def add(url_substring: str, payload, status_code: int = 200) -> None:
        routes.append((url_substring, payload, status_code))

    def fake_get(url: str, *args, **kwargs):
        for sub, payload, status in routes:
            if sub in url:
                return _MockResponse(payload, status)
        return _MockResponse([], 200)

    monkeypatch.setattr("httpx.get", fake_get)
    return add
