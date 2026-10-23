"""Cross-process SEC fair-access bucket (#26, PL6-b).

Two processes must share one budget. The bucket is a file beside the store, taken
with the OS lock primitives in ``job_lock``. When that file cannot be used, the
request still runs on the in-process throttle — it is never failed for pacing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from RevenueChaser.core import job_lock, rawstore, sec_throttle
from RevenueChaser.core.providers import sec_edgar as se


class _Resp:
    def __init__(self, status=200, content=b"{}"):
        self.status_code = status
        self.content = content
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "http://x"),
                response=self,
            )


class _Client:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self._responses.pop(0)


def _child_env() -> dict:
    """A spawned child must import the package, so PYTHONPATH carries the folder that contains it."""
    env = dict(os.environ)
    pkg_root = str(Path(sec_throttle.__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (pkg_root, env.get("PYTHONPATH"))))
    return env


def _wait_for(path: Path, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


# Two children hammer one bucket file and record the monotonic time of every grant.
_HAMMER = r"""
import json, sys, time
from pathlib import Path
from RevenueChaser.core import sec_throttle

path, ready, go, result, rate, duration = sys.argv[1:7]
sec_throttle.bucket_path = lambda: Path(path)
sec_throttle.RATE = int(rate)
Path(ready).write_text("ready")
deadline = time.time() + 30
while time.time() < deadline and not Path(go).exists():
    time.sleep(0.01)
end = time.monotonic() + float(duration)
times = []
while time.monotonic() < end:
    ok = sec_throttle.pace()
    times.append(time.monotonic() if ok else "fallback")
    if not ok:
        break
Path(result).write_text(json.dumps(times))
"""


def _hammer(path, ready, go, result, rate, duration):
    return subprocess.Popen(
        [sys.executable, "-c", _HAMMER, str(path), str(ready), str(go), str(result),
         str(rate), str(duration)],
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_rate_is_one_constant_below_the_ceiling_and_the_burst_is_what_the_ceiling_leaves():
    """SEC fair-access is 10/s. RATE is the one knob; the burst is LIMIT - RATE, never the rate itself."""
    assert sec_throttle.LIMIT == 10
    assert 1 <= sec_throttle.RATE <= 8
    assert sec_throttle._burst(sec_throttle.RATE) == sec_throttle.LIMIT - sec_throttle.RATE
    assert sec_throttle._burst(9.5) == 1.0       # never below one token


def test_the_first_second_after_idle_never_carries_more_than_the_ceiling():
    """A burst equal to the rate (8) plus 8 refilled tokens put ~16 requests into the first second, over 10/s."""
    for rate in (4, 8):
        state, now, granted = {}, 0.0, []
        for _ in range(60):
            wait, state = sec_throttle._take(state, now, float(rate))
            now += wait
            granted.append(now)
        in_first_second = sum(1 for t in granted if t < 1.0)
        assert in_first_second <= sec_throttle.LIMIT, f"rate {rate}: {in_first_second} grants in the first second"
        assert (len(granted) - 1) / granted[-1] <= sec_throttle.LIMIT


def test_the_shared_path_skips_the_in_process_interval(monkeypatch):
    """With the shared bucket the old 0.1 s in-process sleep must not run on top of it (it halves the rate)."""
    monkeypatch.setattr(se.sec_throttle, "pace", lambda: True)
    called = []
    monkeypatch.setattr(se, "_throttle_local", lambda: called.append(1))
    se._throttle()
    se._throttle()
    assert called == []
    monkeypatch.setattr(se.sec_throttle, "pace", lambda: False)
    se._throttle()
    assert called == [1]


def test_which_pacing_is_active_is_logged_once(monkeypatch, caplog):
    """A silent fallback hides that the cross-process budget is off; one line per process says which."""
    caplog.set_level("INFO", logger=sec_throttle.__name__)
    monkeypatch.setattr(sec_throttle, "_LOGGED", set())
    monkeypatch.setattr(sec_throttle, "_reserve", lambda now: None)
    assert sec_throttle.pace() is False
    assert sec_throttle.pace() is False
    assert len(caplog.records) == 1 and "paces itself" in caplog.text
    caplog.clear()
    monkeypatch.setattr(sec_throttle, "_reserve", lambda now: 0.0)
    assert sec_throttle.pace() is True
    assert len(caplog.records) == 1 and "shared bucket" in caplog.text


def test_bucket_sits_beside_the_store_and_is_not_a_job_lock(isolate_rawstore):
    """Same directory ``job_lock`` uses, but not a ``*.lock`` name ``holders()`` would claim."""
    path = sec_throttle.bucket_path()
    assert path.parent == Path(rawstore.DB_PATH).parent
    assert path.name.startswith(".")
    assert not path.name.endswith(".lock")


def test_burst_is_what_the_ceiling_leaves(monkeypatch, tmp_path):
    """The first ``LIMIT - RATE`` takes do not wait; the next one waits one interval."""
    monkeypatch.setattr(sec_throttle, "bucket_path", lambda: tmp_path / ".sec_throttle.json")
    monkeypatch.setattr(sec_throttle, "RATE", 4)
    slept = []
    monkeypatch.setattr(sec_throttle.time, "sleep", lambda s: slept.append(s))
    for _ in range(6):
        assert sec_throttle.pace() is True
    assert slept == []
    assert sec_throttle.pace() is True
    assert slept and abs(slept[0] - 0.25) < 0.02


def test_two_processes_share_one_budget(tmp_path):
    """Two real processes on one file stay within burst + rate * span. Two buckets would not."""
    path = tmp_path / ".sec_throttle.json"
    ready_a, ready_b = tmp_path / "ready_a", tmp_path / "ready_b"
    go = tmp_path / "go"
    result_a, result_b = tmp_path / "a.json", tmp_path / "b.json"
    rate = sec_throttle.RATE
    duration = 0.5
    first = second = None
    try:
        first = _hammer(path, ready_a, go, result_a, rate, duration)
        second = _hammer(path, ready_b, go, result_b, rate, duration)
        assert _wait_for(ready_a, 20) and _wait_for(ready_b, 20), "a child never started"
        go.write_text("go")
        assert first.wait(30) == 0, "first child failed"
        assert second.wait(30) == 0, "second child failed"
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait()
    a = json.loads(result_a.read_text())
    b = json.loads(result_b.read_text())
    assert a and b, "both processes must take tokens"
    assert all(isinstance(t, float) for t in a + b), "a process left the shared bucket"
    times = sorted(a + b)
    span = times[-1] - times[0]
    assert span >= 0.2, f"window too short to prove sharing ({span:.3f}s)"
    # burst (LIMIT - rate) plus refill over the observed span, plus one boundary grant.
    limit = sec_throttle._burst(rate) + rate * span + 2
    assert len(times) <= limit, f"{len(times)} grants in {span:.3f}s, shared cap {limit:.1f}"
    # Two independent buckets would each spend their own burst and refill.
    assert len(times) < 2 * sec_throttle._burst(rate) + 2 * rate * span, "the two processes did not share one budget"


def test_unavailable_bucket_falls_back_and_the_request_still_succeeds(monkeypatch):
    """No OS lock, a refused open, or a lock that never comes free: pace returns False, the GET still runs."""
    monkeypatch.setattr(job_lock, "_OS_LOCK_AVAILABLE", False)
    assert sec_throttle.pace() is False

    def _denied(*_a, **_k):
        raise OSError(13, "denied")

    monkeypatch.setattr(job_lock, "_OS_LOCK_AVAILABLE", True)
    monkeypatch.setattr(sec_throttle.os, "open", _denied)
    assert sec_throttle.pace() is False

    monkeypatch.setattr(sec_throttle.os, "open", os.open)
    monkeypatch.setattr(job_lock, "_os_lock", lambda _fd: False)
    started = time.perf_counter()
    assert sec_throttle.pace() is False
    assert time.perf_counter() - started < 1.0, "a stuck lock must fall back, not hang"

    monkeypatch.setattr(se.sec_throttle, "pace", lambda: False)
    slept = []
    monkeypatch.setattr(se.time, "sleep", lambda s: slept.append(s))
    se._throttle_last[0] = 0.0
    client = _Client([_Resp(200, b'{"ok": true}'), _Resp(200, b"{}")])
    assert se.get_json("http://x", client) == {"ok": True}
    assert se.get_json("http://x", client) == {}
    assert len(client.calls) == 2
    assert slept and 0 < slept[0] <= se._THROTTLE_INTERVAL


def test_a_dead_holder_does_not_wedge_the_bucket(tmp_path):
    """A process killed while it holds the bucket's OS lock must not block the next take."""
    path = tmp_path / ".sec_throttle.json"
    path.write_bytes(b"")
    ready = tmp_path / "ready"
    script = r"""
import os, sys, time
from pathlib import Path
from RevenueChaser.core import job_lock
path, ready = sys.argv[1:3]
fd = os.open(path, os.O_RDWR)
assert job_lock._os_lock(fd)
Path(ready).write_text("held")
time.sleep(60)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(ready)],
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for(ready, 20), "the holder never took the lock"
        proc.kill()
        proc.wait(10)
        original = sec_throttle.bucket_path
        sec_throttle.bucket_path = lambda: path
        try:
            started = time.perf_counter()
            assert sec_throttle.pace() is True
            assert time.perf_counter() - started < 1.0
        finally:
            sec_throttle.bucket_path = original
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_piled_deficit_is_waited_not_refilled(monkeypatch, tmp_path):
    """Reservations from several threads are a deficit, not a corrupt file to refill."""
    path = tmp_path / ".sec_throttle.json"
    monkeypatch.setattr(sec_throttle, "bucket_path", lambda: path)
    monkeypatch.setattr(sec_throttle, "RATE", 4)
    path.write_text(json.dumps({"tokens": -4.0, "ts": time.monotonic()}), encoding="ascii")
    slept = []
    monkeypatch.setattr(sec_throttle.time, "sleep", lambda s: slept.append(s))
    assert sec_throttle.pace() is True
    assert slept and abs(slept[0] - 1.25) < 0.05
    assert json.loads(path.read_text())["tokens"] < 0


def test_a_corrupt_or_huge_deficit_does_not_stall(monkeypatch, tmp_path):
    """Garbage, or a deficit a dead writer left behind, resets. It does not sleep for hours."""
    path = tmp_path / ".sec_throttle.json"
    monkeypatch.setattr(sec_throttle, "bucket_path", lambda: path)
    path.write_text("not-json{{{", encoding="ascii")
    assert sec_throttle.pace() is True
    path.write_text(json.dumps({"tokens": -1e9, "ts": time.monotonic()}), encoding="ascii")
    started = time.perf_counter()
    assert sec_throttle.pace() is True
    assert time.perf_counter() - started < 0.3


def test_uncontended_burst_stays_cheap(monkeypatch, tmp_path):
    """A full burst, no other process: file locking must not add a per-request sleep."""
    monkeypatch.setattr(sec_throttle, "bucket_path", lambda: tmp_path / ".sec_throttle.json")
    n = int(sec_throttle._burst(sec_throttle.RATE))
    started = time.perf_counter()
    for _ in range(n):
        assert sec_throttle.pace() is True
    elapsed = time.perf_counter() - started
    # Today's in-process take is microseconds. Half a second for the whole burst is
    # generous, and still fails if each take sleeps an interval (8 * 0.125s = 1s).
    assert elapsed < 0.5, f"burst of {n} took {elapsed:.3f}s"


def test_every_sec_request_takes_one_shared_token(monkeypatch):
    """``_throttle`` — the seam ``_request`` and the bulk download both use — takes one token per call."""
    taken = []
    monkeypatch.setattr(se.sec_throttle, "pace", lambda: taken.append(1) or True)
    client = _Client([_Resp(), _Resp(content=b"ok"), _Resp()])
    se.get_json("http://x", client)
    se.get_text("http://x", client)
    se.get_bytes("http://x", client)
    se._throttle()
    assert taken == [1, 1, 1, 1]
