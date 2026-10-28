"""Root test configuration — offline enforcement, shared fixtures, HTTP mocking.

The unit suite never touches the network. ``core/providers/*`` are mocked at the
``httpx`` boundary (never at rule-math level), and the shared fixtures under
``core/tests/fixtures/`` provide canned inputs.

Testing a live API is out of scope here: write it as a standalone script under
``core/`` and run it deliberately, never as part of ``pytest``.
"""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "core" / "tests" / "fixtures"

# The scratch root this process is responsible for removing at session end, or None when the
# root was inherited from a parent process (an xdist worker) and is not ours to touch.
# Until this existed, one rc-pytest-* directory leaked per pytest process: 917 of them, and
# pytest's own basetemp on top, had collected in the system temp dir by 2026-10-23.
SCRATCH_ROOT: Path | None = None
SCRATCH_PREFIX = "rc-pytest-"


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
    _root = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX))
    # Named exactly like the real roots so core/tests/test_store_layout.py's
    # "DATA_DIR is the .store folder" guard keeps asserting something.
    os.environ.setdefault("RC_STORE_DIR", str(_root / ".store"))
    os.environ.setdefault("RC_CACHE_DIR", str(_root / ".cache"))
    os.environ.setdefault("RC_EDGAR_DOCS_DIR", str(_root / "edgar_docs"))
    # Same for the SPX Gamma tree (UW day files, brier.json): the backup mirrors it (#276), and an
    # unredirected test once wrote into the real brier.json (G1). Tests that pass their own root still win.
    os.environ.setdefault("SPX_GAMMA_DATA_ROOT", str(_root / "spx_gamma"))
    # The market store's SSD and HDD roots (#206). The off-box copy target is forced off, like the backup mirror.
    os.environ.setdefault("RC_MARKET_DIR", str(_root / "market"))
    os.environ.setdefault("RC_MARKET_RAW_DIR", str(_root / "market_raw"))
    os.environ["RC_MARKET_RAW_COPY_DIR"] = ""
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

    global SCRATCH_ROOT
    in_use = Path(os.environ["RC_STORE_DIR"]).parent
    if in_use != _root:
        # An earlier import of this file already redirected the roots — pytest loads the rootdir
        # conftest, and the package imports it as RevenueChaser.conftest as well — so every
        # setdefault above was a no-op and the root just made is unused. Remove it here instead of
        # leaking it, and keep the root the environment points at.
        shutil.rmtree(_root, ignore_errors=True)
    # Whoever created the root removes it, and only them. CI runs `pytest -n auto`, where the xdist
    # controller creates the root and every worker inherits RC_STORE_DIR through its environment:
    # without this, each worker would remove the shared root at its own session end and the first
    # one to finish would pull the scratch out from under the workers still running.
    owner = os.environ.get("RC_PYTEST_SCRATCH_OWNER")
    if owner is None and in_use == _root:
        # We made the root this run is using, so we own removing it, and any process we start can
        # see that it does not. Claimed only when the root is ours: a root handed to us without this
        # marker belongs to whoever prepared it.
        os.environ["RC_PYTEST_SCRATCH_OWNER"] = owner = str(os.getpid())
    owned = owner == str(os.getpid())
    # None when the root was inherited, or when something outside the suite set RC_STORE_DIR:
    # in neither case is that directory ours to remove.
    SCRATCH_ROOT = in_use if owned and in_use.name.startswith(SCRATCH_PREFIX) else None


_redirect_data_roots()


def _remove_scratch_root(root: Path | str | None) -> None:
    """Remove a scratch root. Never raises, and never touches a directory it did not name."""
    if root is None:
        return
    root = Path(root)
    # A wrong path here would delete real data, so the name is the gate, not the caller.
    if not root.name.startswith(SCRATCH_PREFIX):
        return
    # ignore_errors: on Windows a subprocess the suite started may still hold a DuckDB file open,
    # and scratch left behind is not worth failing a green run over. scripts/clean_test_scratch.py
    # is the broom for whatever survives.
    shutil.rmtree(root, ignore_errors=True)


def pytest_sessionfinish(session, exitstatus):
    _remove_scratch_root(SCRATCH_ROOT)


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
def isolate_ops(tmp_path):
    """Give every test its own market root, so its ``ops.sqlite`` (the watch lists, the queue) is its own.

    The watch lists live in ``ops.sqlite`` since PR 4, and LEAPS, Earnings and the dashboard's toggle read and write
    them, so a list one test left would be the next test's input. Both market roots move (``rcdata`` reads
    ``config.MARKET_DIR`` at call time); the read log moves too, or its exit flush would write into the per-process
    root after the session removed it. Nothing is created until a test writes. A test's own ``monkeypatch`` still wins.
    """
    from RevenueChaser.core import config
    from RevenueChaser.rcdata import ops
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "MARKET_DIR", tmp_path / "market")
    mp.setattr(config, "MARKET_RAW_DIR", tmp_path / "market_raw")
    mp.setattr(ops, "read_log", ops.ReadLog(process="test"))
    try:
        yield config.MARKET_DIR
    finally:
        mp.undo()


# #111: six boards keep the same ``<PREFIX>_LATEST`` / ``<PREFIX>_JSON`` pair twice, in ``app/runners.py`` and in
# their own config, and different code reads each copy, so both copies are redirected together.
BOARD_ARTIFACT_PAIRS = (("FUNDAMENTALS", "module_fundamentals"), ("SWING", "module_swing_potential"),
                        ("COMOVEMENT", "module_comovement"), ("VALUE_CHAIN", "module_value_chain"),
                        ("RELATEDNESS", "module_relatedness"), ("OIPD", "module_oipd"))


@pytest.fixture(autouse=True)
def isolate_board_artifacts():
    """Point both copies of the six boards' artifact paths, and Asymmetric Growth's, at a folder of this test's own.

    Autouse for the reason ``isolate_rawstore`` is: the data-root redirect is one folder per process, so a payload
    one test saved was what the next test's Refresh read, and a Refresh test could pass on that leftover (#111). The
    folder sits inside the cache root and keeps each path's place below it, so code that takes a path relative to
    ``OUTPUT_DIR`` and the store-layout guard both still hold. A test that sets its own path still wins: its
    ``monkeypatch`` runs after this. The folder is removed when the test ends.
    """
    import importlib
    import uuid

    from RevenueChaser.core import config
    root = Path(config.OUTPUT_DIR)
    dest = root / "per-test" / uuid.uuid4().hex
    runners = importlib.import_module("RevenueChaser.app.runners")
    mp = pytest.MonkeyPatch()
    for prefix, module in BOARD_ARTIFACT_PAIRS:
        cfg = importlib.import_module(f"RevenueChaser.{module}.config")
        for attr in (f"{prefix}_LATEST", f"{prefix}_JSON"):
            for mod in (runners, cfg):
                mp.setattr(mod, attr, dest / Path(getattr(mod, attr)).relative_to(root))
    # Asymmetric Growth's two paths live on the runners only (the board has no config module of them). Left in the
    # process root, two files' AG tests on two xdist workers wrote and unlinked the same file (a setup error seen
    # under ``-n 4``, 28 Oct 2026).
    for attr in ("ASYMMETRIC_GROWTH_LATEST", "ASYMMETRIC_GROWTH_JSON"):
        mp.setattr(runners, attr, dest / Path(getattr(runners, attr)).relative_to(root))
    try:
        yield dest
    finally:
        mp.undo()
        shutil.rmtree(dest, ignore_errors=True)


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
def isolate_deepseek_gate(tmp_path, monkeypatch):
    """Never read the real Hermes peak-override file from a test.

    If the user flipped "run at peak" on the card, the real file would make every
    peak-gate test pass or fail depending on the time of day.
    """
    monkeypatch.setenv("DEEPSEEK_GATE_FILE", str(tmp_path / "deepseek_gate.json"))


@pytest.fixture(autouse=True)
def _reset_cik_map():
    """The CIK map is cached per process (#301 review MUST-2); no test may inherit another's map."""
    from RevenueChaser.core.providers import sec_edgar
    sec_edgar.reset_cik_map()
    yield
    sec_edgar.reset_cik_map()


# The module-level inputs ``ensure_all_tables`` builds a store from. A test that changes one of them before its store
# is made gets the apply run on its own file, never a template built from the unchanged ones.
# NOT covered: the apply's code (``_ddl``, ``_reconcile_table``, ``_apply_views``, ``_sync_tracked`` and the other
# helpers), the stamp constants (``_SCHEMA_FILES``, ``_SCHEMA_STAMP_TABLE``, ``_SCHEMA_STAMP_KEY``,
# ``_REBUILD_RUN_ID``) and the environment (``RC_STORE_SCHEMA_OK``, ``RC_STORE_SCHEMA_REFUSE``). A test that patches
# one of those before its store exists must carry ``@pytest.mark.rawstore_schema_from_scratch``.
_SCHEMA_INPUTS = ("_SCHEMA_VERSION", "MANIFEST", "CHANGES", "_RENAMES", "_MERGES", "_VIEWS", "_KEY_REBUILDS",
                  "_PRICE_MOVES", "_EDGAR_TABLES")
SCHEMA_FROM_SCRATCH = "rawstore_schema_from_scratch"


def _canonical(value):
    # Key order is not schema: monkeypatch.delitem's undo puts a MANIFEST entry back at the end.
    if isinstance(value, dict):
        return tuple(sorted((k, _canonical(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canonical(v) for v in value)
    return value


def _schema_fingerprint() -> str:
    import hashlib
    from RevenueChaser.core import rawstore
    inputs = [_canonical(getattr(rawstore, n, None)) for n in _SCHEMA_INPUTS]
    return hashlib.sha256(repr(inputs).encode()).hexdigest()


class RawstoreTemplate:
    """The store files ``ensure_all_tables`` makes on an empty directory, made once per pytest process.

    Built by the apply itself (no copy of its DDL), so a schema change reaches the template with the code, and every
    copy first compares the schema inputs (``_SCHEMA_INPUTS``) with the ones the template was built from. It is built
    fresh in each session and never reused across sessions, which is what keeps it from going stale; the directory
    name (schema version and a hash of ``core/rawstore.py``) only labels it and guards nothing. It lives in this
    process's own pytest basetemp (one per xdist worker), so no worker shares it; a passing run removes it with the
    basetemp, a failing run keeps it there (``tmp_path_retention_policy = failed``).
    """

    def __init__(self, root: Path):
        import hashlib
        from RevenueChaser.core import rawstore
        code = hashlib.sha256(Path(rawstore.__file__).read_bytes()).hexdigest()[:12]
        self.dir = root / f"v{rawstore._SCHEMA_VERSION}-{code}"
        self.dir.mkdir(parents=True)
        self.fingerprint = _schema_fingerprint()
        mp = pytest.MonkeyPatch()
        mp.setattr(rawstore, "DB_PATH", self.dir / "raw.duckdb")
        try:
            rawstore.ensure_all_tables()
        finally:
            mp.undo()
        # What the apply leaves in this process besides the files: the per-(file, table) memo and the "applied" mark.
        self.ready = {(Path(p).name, t) for p, t in rawstore._TABLE_READY if Path(p).parent == self.dir}
        self.files = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in self.dir.iterdir()
                      if p.is_file() and not p.name.startswith(rawstore._PRESENCE_PREFIX)}

    def copy_to(self, raw_path: Path) -> bool:
        """Give ``raw_path``'s directory its own copy of the template; False when it has to be built from scratch."""
        from RevenueChaser.core import rawstore
        dest = raw_path.parent
        if (raw_path.name != "raw.duckdb" or _schema_fingerprint() != self.fingerprint
                or any((dest / name).exists() for name in self.files)):
            return False
        for name in self.files:
            shutil.copyfile(self.dir / name, dest / name)
        rawstore._TABLE_READY.update((str(dest / name), table) for name, table in self.ready)
        rawstore._ENSURED[str(raw_path)] = 0.0
        return True

    def unchanged(self) -> bool:
        """True while the template's files are exactly as the build left them."""
        return {name: ((self.dir / name).stat().st_size, (self.dir / name).stat().st_mtime_ns)
                for name in self.files} == self.files


@pytest.fixture(scope="session")
def rawstore_template(tmp_path_factory):
    """The per-process template ``rawstore_tmp_db`` copies. Never open it: a test gets its own copy."""
    return RawstoreTemplate(tmp_path_factory.mktemp("rawstore-template"))


@pytest.fixture
def rawstore_tmp_db(isolate_rawstore, rawstore_template, request):
    """The isolated DB path, with every manifest table created.

    The files are a copy of ``rawstore_template`` (~2 ms instead of ~1 s for the apply), made in this test's own
    ``tmp_path``: only the template is shared, and no test opens it, so every test still gets a fresh store. The apply
    runs on the test's own file instead when the test is marked ``rawstore_schema_from_scratch`` (it tests the apply or
    a migration itself), when a schema input differs from the template's, or when a store file is already there.
    """
    from RevenueChaser.core import rawstore
    if request.node.get_closest_marker(SCHEMA_FROM_SCRATCH) or not rawstore_template.copy_to(Path(rawstore.DB_PATH)):
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
