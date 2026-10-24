"""Drive a real dashboard subprocess: start it, run jobs, read boards back.

This is the tests' only interface to the app. It speaks HTTP to a live server
rather than using Flask's test client, so the harness exercises the same path a
browser does — including the async job queue, which is where "the button did
nothing" bugs actually live.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

REPO_PARENT = Path(__file__).resolve().parents[4]
ENTRYPOINT = "RevenueChaser.module_testing.e2e.harness.dashboard_entrypoint"

STARTUP_TIMEOUT = 60.0
JOB_TIMEOUT = 180.0


class DashboardStartupError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class JobResult:
    """Terminal state of a ``/run/*`` job."""

    id: str
    status: str
    error: str | None
    message: str | None
    notes: list[str] = field(default_factory=list)       # the forecast-freeze outcome (#128), never a popup

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def __str__(self) -> str:
        return f"job {self.id} {self.status} error={self.error!r} message={self.message!r} notes={self.notes!r}"


class Dashboard:
    """A running dashboard subprocess bound to a temp store/cache."""

    def __init__(self, store_dir: Path, cache_dir: Path, env: dict[str, str],
                 log_path: Path):
        self.store_dir = store_dir
        self.cache_dir = cache_dir
        self.env = env
        self.log_path = log_path
        self.yf_log_path = log_path.parent / "yf_calls.log"
        self.port = _free_port()
        self._proc: subprocess.Popen | None = None
        self._log = None
        self._client: httpx.Client | None = None

    # ── lifecycle ──
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "Dashboard":
        env = {**os.environ, **self.env,
               "RC_STORE_DIR": str(self.store_dir),
               "RC_CACHE_DIR": str(self.cache_dir),
               # The unit suite turns the first-use manifest pass off for speed;
               # the real dashboard under test must behave like production.
               "RC_STORE_AUTO_ENSURE": "1",
               "RC_E2E_YF_LOG": str(self.yf_log_path),
               "PYTHONUNBUFFERED": "1"}
        self._log = self.log_path.open("w", encoding="utf-8")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", ENTRYPOINT, str(self.port)],
            cwd=str(REPO_PARENT), env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        # The dashboard refuses a write without X-RC-Panel (#231); the pages send it, so
        # does the harness. A test about the missing header builds a bare httpx client.
        self._client = httpx.Client(base_url=self.url, timeout=JOB_TIMEOUT,
                                    headers={"X-RC-Panel": "e2e"})
        self._await_ready()
        return self

    def _await_ready(self) -> None:
        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise DashboardStartupError(
                    f"dashboard exited with {self._proc.returncode}\n{self.logs()}")
            try:
                if self._client.get("/", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.15)
        raise DashboardStartupError(f"dashboard did not come up\n{self.logs()}")

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                self._proc.kill()
                self._proc.wait(timeout=10)
            self._proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def restart(self) -> "Dashboard":
        """Stop and start again on the same store/cache.

        A restart is the honest way to prove a board survives a cold process:
        nothing in memory carries over, only what reached ``.store``/``.cache``.
        """
        self.stop()
        self.port = _free_port()
        return self.start()

    def logs(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover
            return "<no logs>"

    def yf_calls(self) -> list[str]:
        """yfinance calls recorded inside the subprocess, one per line.

        The fake appends to ``$RC_E2E_YF_LOG`` (set in ``start``); the file does
        not exist until the first call, which reads as "no calls".
        """
        try:
            text = self.yf_log_path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line for line in text.splitlines() if line]

    # ── HTTP ──
    @property
    def client(self) -> httpx.Client:
        assert self._client is not None, "dashboard not started"
        return self._client

    def get(self, path: str, **params) -> httpx.Response:
        return self.client.get(path, params=params or None)

    def view(self, module: str, refresh: bool = False, **params) -> httpx.Response:
        if refresh:
            params["refresh"] = 1
        return self.get(f"/view/{module}", **params)

    # ── jobs ──
    def post_fetch(self, route: str, **form) -> httpx.Response:
        """POST a ``/run/*`` route without waiting. Returns the raw response."""
        return self.client.post(route, data=form or None)

    def run_job(self, route: str, timeout: float = JOB_TIMEOUT, **form) -> JobResult:
        """POST a ``/run/*`` route and wait for the job to reach a terminal state."""
        r = self.post_fetch(route, **form)
        if r.status_code != 202:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            raise AssertionError(f"{route} refused: HTTP {r.status_code} {body}")
        return self.wait_for_job(r.json()["job_id"], timeout=timeout)

    def wait_for_job(self, job_id: str, timeout: float = JOB_TIMEOUT) -> JobResult:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.client.get(f"/api/job/{job_id}")
            if r.status_code == 200:
                job = r.json()["job"]
                if job["status"] in ("done", "error", "cancelled"):
                    return JobResult(id=job_id, status=job["status"], error=job.get("error"),
                                     message=job.get("message"), notes=job.get("notes") or [])
            time.sleep(0.1)
        raise TimeoutError(f"job {job_id} did not finish in {timeout}s\n{self.logs()}")

    def wait_until_idle(self, timeout: float = JOB_TIMEOUT) -> None:
        """Wait until no job is queued or running.

        Some routes queue a follow-up job (``/run/rotation`` also queues
        ``moneyflow``), whose requests would otherwise land in the next step."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.client.get("/api/jobs")
            if r.status_code == 200 and not any(
                    j["status"] in ("queued", "running") for j in r.json()["jobs"]):
                return
            time.sleep(0.1)
        raise TimeoutError(f"jobs still busy after {timeout}s\n{self.logs()}")

    # ── store / cache inspection ──
    def data_as_of(self, module: str) -> str | None:
        r = self.get(f"/api/data_asof/{module}")
        return r.json().get("data_as_of") if r.status_code == 200 else None

    def artifact(self, relative: str) -> Path:
        return self.cache_dir / relative

    def table_count(self, table: str) -> int:
        """Row count straight from the temp DuckDB store.

        Opened here, in the test process, only while the dashboard is idle —
        DuckDB allows a single writer process at a time.
        """
        import duckdb
        try:
            from RevenueChaser.core import rawstore
            name = rawstore.MANIFEST.get(table, {}).get("db", "raw")    # edgar, history, prices (#156)
        except Exception:  # noqa: BLE001
            name = "raw"
        db = self.store_dir / f"{name}.duckdb"
        if not db.exists():
            return 0
        conn = duckdb.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                [table]).fetchone()
            if not rows or not rows[0]:
                return 0
            return conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        finally:
            conn.close()
