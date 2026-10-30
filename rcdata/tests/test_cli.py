"""The two CLIs end to end, as subprocesses against a loopback mock of every market_series source.

``python -m RevenueChaser.rcdata.build run --once --dataset market_series`` builds ``market.duckdb`` (serving schema
1, ``market_series`` rows, a ``dataset_check`` and a ``job_runs2`` row) and a second run calls nothing;
``python -m RevenueChaser.rcdata backup`` writes a verified copy and refuses an empty backup folder. The server runs
in this process (the root conftest only blocks *outgoing* connections); the subprocesses talk to it on 127.0.0.1.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import duckdb
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]


def _route(path: str) -> bytes | None:
    files = {"/treasury/": "treasury_2026.csv", "/cboe/VVIX_History.csv": "VVIX_History.csv", "/cboe/": "VIX_History.csv",
             "/fred": "fred_DGS10.json", "/cftc/udgc-27he.json": "cftc_tff.json",
             "/cftc/72hh-3qpy.json": "cftc_disagg.json", "/fed/": "fomc.htm", "/bls/": "bls.ics",
             "/yahoo/": "yahoo_history_MOVE.json"}
    for prefix, name in files.items():
        if path.startswith(prefix):
            return (FIXTURES / name).read_bytes()
    return None


SLOW: set[str] = set()          # path prefixes the mock answers only after a long wait


@pytest.fixture
def mock_server():
    seen: list[str] = []
    SLOW.clear()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if any(self.path.startswith(p) for p in SLOW):
                import time
                time.sleep(20)
            body = _route(self.path.split("?")[0])
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(body or b"no route")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def cli_env(tmp_path, mock_server):
    base, _ = mock_server
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RC_", "RAW_STORE_BACKUP"))}
    env.update({
        "RC_STORE_DIR": str(tmp_path / ".store"), "RC_CACHE_DIR": str(tmp_path / ".cache"),
        "RC_EDGAR_DOCS_DIR": str(tmp_path / "edgar_docs"), "RC_STORE_AUTO_ENSURE": "0",
        "RC_MARKET_DIR": str(tmp_path / "market"), "RC_MARKET_RAW_DIR": str(tmp_path / "market_raw"),
        "RC_MARKET_RAW_COPY_DIR": "", "RAW_STORE_BACKUP_DIR": str(tmp_path / "backups"),
        "RC_TREASURY_BASE": f"{base}/treasury", "RC_CBOE_BASE": f"{base}/cboe", "RC_FRED_BASE": f"{base}/fred",
        "RC_CFTC_BASE": f"{base}/cftc/udgc-27he.json", "RC_FED_BASE": f"{base}/fed", "RC_BLS_BASE": f"{base}/bls",
        "RC_YAHOO_BASE": f"{base}/yahoo",
        "FRED_API_KEY": "cli-test-key", "UNUSUAL_WHALES_API_KEY": "",
    })
    return env


def _run(env, *args, timeout=300):
    return subprocess.run([sys.executable, "-m", *args], cwd=REPO.parent, env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_the_builder_cli_builds_market_series_and_a_second_run_calls_nothing(cli_env, mock_server, tmp_path):
    _, seen = mock_server
    args = ("RevenueChaser.rcdata.build", "run", "--once", "--dataset", "market_series", "--as-of", "2026-10-27")
    r = _run(cli_env, *args)
    assert r.returncode == 0, r.stderr[-3000:]
    assert r.stdout.startswith("market_series changed as_of=2026-10-27")
    assert all(len(line) <= 120 for line in r.stdout.splitlines())
    first = len(seen)
    assert first > 0 and not any("cli-test-key" in line for line in (r.stdout + r.stderr).splitlines())
    assert sorted(p for p in seen if p.startswith("/yahoo/")) == ["/yahoo/%5EMOVE.json", "/yahoo/ZW%3DF.json"]

    market = tmp_path / "market" / "market.duckdb"
    con = duckdb.connect(str(market), read_only=True)
    try:
        assert con.execute("SELECT kind, version FROM serving_schema_version").fetchall() == [("market", 1)]
        assert con.execute("SELECT count(DISTINCT source) FROM market_series").fetchone()[0] == 7
    finally:
        con.close()
    ops = sqlite3.connect(tmp_path / "market" / "ops.sqlite")
    assert ops.execute("SELECT result FROM dataset_check").fetchall() == [("changed",)]
    assert ops.execute("SELECT dataset, status FROM job_runs2").fetchall() == [("market_series", "ok")]
    ops.close()

    r = _run(cli_env, *args)
    assert r.returncode == 0, r.stderr[-3000:]
    assert r.stdout.startswith("market_series unchanged") and "calls=0" in r.stdout
    assert len(seen) == first, f"the second run called {seen[first:]}"


@contextmanager
def _holding(env, name: str):
    """Another process holding ``name``'s job lock in this env's store dir, for as long as the block runs."""
    hold = ("import sys,time; from RevenueChaser.core import job_lock\n"
            f"with job_lock.acquire({name!r}, wait=0) as got:\n"
            "    print(got, flush=True); time.sleep(30)")
    p = subprocess.Popen([sys.executable, "-c", hold], cwd=REPO.parent, env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "True"
        yield
    finally:
        p.kill()
        p.wait()


def test_the_builder_starts_while_a_lock_on_the_old_store_is_held(cli_env, tmp_path):
    """End to end for the per-file gate: ``daily_bars`` writes the old store, the builder the market one.

    Before the gate asked ``job_lock.blocking_holders``, this exited 3 and built nothing — which is how the WRDS
    price backfill was refused while the Relatedness pilot ran (30 Oct 2026).
    """
    with _holding(cli_env, "daily_bars"):
        r = _run(cli_env, "RevenueChaser.rcdata.build", "run", "--once", "--dataset", "market_series",
                 "--as-of", "2026-10-27")
        assert r.returncode == 0, r.stderr[-3000:]
        assert r.stdout.startswith("market_series changed as_of=2026-10-27")
        assert (tmp_path / "market" / "market.duckdb").exists()


def test_the_builder_refuses_to_start_while_an_undeclared_lock_is_held(cli_env):
    """A family with no ``job_lock.WRITES`` entry still holds the builder off, and the message names it."""
    with _holding(cli_env, "a_new_writer"):
        r = _run(cli_env, "RevenueChaser.rcdata.build", "run", "--once", "--dataset", "market_series")
        assert r.returncode == 3 and "a_new_writer" in r.stderr
        assert not (Path(cli_env["RC_MARKET_DIR"]) / "market.duckdb").exists()


def test_the_backup_cli_writes_a_verified_copy_and_refuses_an_empty_folder(cli_env, tmp_path):
    r = _run(cli_env, "RevenueChaser.rcdata.build", "run", "--once", "--dataset", "market_series",
             "--as-of", "2026-10-27")
    assert r.returncode == 0, r.stderr[-3000:]
    r = _run(cli_env, "RevenueChaser.rcdata", "backup")
    assert r.returncode == 0, r.stderr[-3000:]
    out = tmp_path / "backups" / "market"
    assert len(list(out.glob("market_*.duckdb"))) == 1 and len(list(out.glob("ops_*.sqlite"))) == 1
    assert (out / ".manifest.json").exists()

    empty = {**cli_env, "RAW_STORE_BACKUP_DIR": ""}
    r = _run(empty, "RevenueChaser.rcdata", "backup")
    assert r.returncode == 2 and "RAW_STORE_BACKUP_DIR" in r.stderr


def test_n2_a_sigterm_mid_run_still_counts_the_calls_sent(cli_env, mock_server, tmp_path):
    """systemd stops a unit with SIGTERM: the builder turns it into SystemExit, so the run's finally writes the
    call counter and the run is marked killed."""
    import signal
    import time
    _, seen = mock_server
    SLOW.add("/bls/")                                  # the last source: everything before it has been called
    p = subprocess.Popen([sys.executable, "-m", "RevenueChaser.rcdata.build", "run", "--once", "--dataset",
                          "market_series", "--as-of", "2026-10-27"], cwd=REPO.parent, env=cli_env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 120
        while not any(path.startswith("/bls/") for path in seen):
            assert p.poll() is None and time.monotonic() < deadline, "the builder ended before reaching BLS"
            time.sleep(0.1)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    ops = sqlite3.connect(tmp_path / "market" / "ops.sqlite")
    calls = dict(ops.execute("SELECT provider, calls FROM provider_calls_daily").fetchall())
    status = ops.execute("SELECT status FROM job_runs2").fetchall()
    ops.close()
    assert calls.get("cboe") == 8 and calls.get("fred") == 13 and calls.get("bls") == 1, calls
    assert status == [("killed",)]
