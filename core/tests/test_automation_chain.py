"""The nightly chain rule of the automations registry (#129 slice E).

A chained entry (``after=<registry name>``) is still its own Task Scheduler task with its own
not-before time. When it starts, ``run_task`` first waits for the predecessor's run of this night
(the run that started at or after the predecessor's last scheduled time) to exit 0; a failed,
missing or overdue predecessor run makes the step exit ``CHAIN_BLOCKED`` without running its
command. Nothing here touches a real Task Scheduler or sleeps.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from RevenueChaser.core import automations as A

AMS = ZoneInfo("Europe/Amsterdam")


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(A.config, "OUTPUT_DIR", tmp_path)
    A.set_scheduler(None)
    yield
    A.set_scheduler(None)


PRED = A.AutomationEntry(name="RC-Pred", label="pred", args=("-c", "pass"), time="22:35", description="d")
SUCC = A.AutomationEntry(name="RC-Succ", label="succ", args=("-c", "print('step ran')"), time="23:00",
                         description="d", after="RC-Pred", chain_wait_minutes=30)


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(A, "_BY_NAME", {PRED.name: PRED, SUCC.name: SUCC})
    PRED.log_path.parent.mkdir(parents=True, exist_ok=True)
    return PRED, SUCC


def _ams(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=AMS)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _pred_log(*runs):
    """``runs``: (start, end or None, rc or None) in Amsterdam time."""
    lines = []
    for start, end, rc in runs:
        lines.append(f"=== {_iso(start)} start RC-Pred\nsome output\n")
        if end is not None:
            lines.append(f"=== {_iso(end)} exit {rc}\n")
    PRED.log_path.write_text("".join(lines), encoding="utf-8")


class Clock:
    """A fake clock; ``sleep`` advances it and runs an optional hook (the other process)."""

    def __init__(self, start, hook=None, max_sleeps=1000):
        self.now, self.hook, self.sleeps, self.max = start, hook, 0, max_sleeps

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        if self.sleeps > self.max:
            raise AssertionError("the chain wait never gave up")
        self.now += timedelta(seconds=seconds)
        if self.hook:
            self.hook(self)


# --- registry ---------------------------------------------------------------

def test_a_chained_schedule_names_its_predecessor():
    assert SUCC.schedule == "daily 23:00 (local), after RC-Pred"
    assert A.static_row(SUCC)["after"] == "RC-Pred"


def test_unchained_rows_keep_their_schedule_text():
    assert A.entry("RC-Store-Backup").schedule == "daily 04:00 (local)"
    assert all(r["after"] is None for r in A.static_rows() if r["name"] == "RC-Store-Backup")


def test_the_shipped_registry_passes_the_chain_check():
    A.check_chain(A.REGISTRY)


def test_the_chain_check_runs_on_import():
    src = Path(A.__file__).read_text(encoding="utf-8")
    assert re.search(r"^check_chain\(REGISTRY\)$", src, re.M)


@pytest.mark.parametrize("step", ["22:35", "23:00", "00:30", "10:34"])
def test_a_step_due_up_to_12_hours_after_its_predecessor_is_accepted(step):
    A.check_chain((PRED, A.AutomationEntry(name="RC-Ok", label="o", args=(), time=step, description="d",
                                           after="RC-Pred")))


def test_a_step_chained_on_a_weekly_task_is_rejected():
    # #129 A1: chain_cutoff reads the predecessor's time as daily, so a step after a Saturday-only task
    # would find no run of "tonight" six days a week and exit CHAIN_BLOCKED by design.
    weekly = A.AutomationEntry(name="RC-Weekly", label="w", args=(), time="10:00", description="d",
                               days=("Saturday",))
    step = A.AutomationEntry(name="RC-After", label="a", args=(), time="10:30", description="d", after="RC-Weekly")
    with pytest.raises(ValueError, match="weekly"):
        A.check_chain((weekly, step))


def test_a_weekday_step_may_follow_a_daily_task():
    A.check_chain((PRED, A.AutomationEntry(name="RC-Wd", label="w", args=(), time="23:00", description="d",
                                           after="RC-Pred", days=("Monday", "Friday"))))


def test_the_default_wait_is_the_one_the_doc_states():
    default = A.AutomationEntry.__dataclass_fields__["chain_wait_minutes"].default
    assert default == 120
    doc = (Path(A.__file__).resolve().parents[1] / "docs" / "data_health.md").read_text(encoding="utf-8")
    assert f"`chain_wait_minutes` (default {default})" in doc


@pytest.mark.parametrize("bad,why", [
    ((SUCC,), "unknown"),
    ((SUCC, PRED), "earlier"),
    ((PRED, A.AutomationEntry(name="RC-Self", label="s", args=(), time="23:00", description="d",
                              after="RC-Self")), "itself"),
    ((PRED, A.AutomationEntry(name="RC-Long", label="l", args=(), time="23:00", description="d",
                              after="RC-Pred", chain_wait_minutes=240)), "time limit"),
    ((PRED, A.AutomationEntry(name="RC-Zero", label="z", args=(), time="23:00", description="d",
                              after="RC-Pred", chain_wait_minutes=0)), "positive"),
    # Clock order: a step due before its predecessor in the night would be released by last night's run.
    ((PRED, A.AutomationEntry(name="RC-Early", label="e", args=(), time="22:00", description="d",
                              after="RC-Pred")), "clock"),
    ((PRED, A.AutomationEntry(name="RC-Noon", label="n", args=(), time="10:35", description="d",
                              after="RC-Pred")), "clock"),
])
def test_a_broken_chain_is_rejected(bad, why):
    with pytest.raises(ValueError, match=why):
        A.check_chain(bad)


# --- the cutoff: the predecessor's last scheduled time ---------------------------------

@pytest.mark.parametrize("now,expected", [
    (_ams(2026, 10, 9, 23, 0), _ams(2026, 10, 9, 22, 35)),    # the step's own 23:00 start
    (_ams(2026, 10, 9, 22, 35), _ams(2026, 10, 9, 22, 35)),   # exactly at the predecessor's time
    (_ams(2026, 10, 10, 7, 0), _ams(2026, 10, 9, 22, 35)),    # catch-up after a sleeping night
    (_ams(2026, 10, 9, 20, 0), _ams(2026, 10, 8, 22, 35)),    # a manual run before tonight's bars
    (_ams(2026, 10, 25, 23, 0), _ams(2026, 10, 25, 22, 35)),  # CET again after the DST change
])
def test_cutoff_is_the_last_local_scheduled_time(now, expected):
    got = A.chain_cutoff("22:35", now.astimezone(timezone.utc), tz=AMS)
    assert got == expected
    assert got.utcoffset() == expected.utcoffset()


def test_cutoff_uses_local_time_not_utc():
    # 22:35 Amsterdam in summer is 20:35 UTC; a UTC reading of "22:35" would be two hours late.
    got = A.chain_cutoff("22:35", datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc), tz=AMS)
    assert got.astimezone(timezone.utc) == datetime(2026, 10, 9, 20, 35, tzinfo=timezone.utc)


# --- reading the predecessor's log -------------------------------------------------------

def test_last_run_reads_running_finished_and_missing(chain):
    assert A.last_run(PRED.log_path) is None
    _pred_log((_ams(2026, 10, 8, 22, 35), _ams(2026, 10, 8, 23, 0), 0), (_ams(2026, 10, 9, 22, 35), None, None))
    r = A.last_run(PRED.log_path)
    assert r.start == _ams(2026, 10, 9, 22, 35) and r.end is None and r.rc is None
    _pred_log((_ams(2026, 10, 9, 22, 35), _ams(2026, 10, 9, 23, 5), 1))
    r = A.last_run(PRED.log_path)
    assert r.end == _ams(2026, 10, 9, 23, 5) and r.rc == 1


def test_the_last_footer_of_a_run_decides_it(chain):
    # A child line shaped like a footer must not decide the run: run_task writes the real footer last.
    PRED.log_path.write_text(
        f"=== {_iso(_ams(2026, 10, 9, 22, 35))} start RC-Pred\n"
        f"=== {_iso(_ams(2026, 10, 9, 22, 36))} exit 0\n"
        f"=== {_iso(_ams(2026, 10, 9, 22, 50))} exit 1\n", encoding="utf-8")
    r = A.last_run(PRED.log_path)
    assert r.rc == 1 and r.end == _ams(2026, 10, 9, 22, 50)


# --- the wait ---------------------------------------------------------------------------

def test_tonights_success_lets_the_step_run_at_once(chain):
    _pred_log((_ams(2026, 10, 9, 22, 35), _ams(2026, 10, 9, 22, 58), 0))
    c = Clock(_ams(2026, 10, 9, 23, 0))
    ok, line = A.wait_for_chain(SUCC, clock=c, sleep=c.sleep, tz=AMS)
    assert ok and c.sleeps == 0 and "RC-Pred exit 0" in line


def test_last_nights_success_does_not_count_and_the_wait_gives_up(chain):
    _pred_log((_ams(2026, 10, 8, 22, 35), _ams(2026, 10, 8, 22, 58), 0))
    c = Clock(_ams(2026, 10, 9, 23, 0))
    ok, line = A.wait_for_chain(SUCC, clock=c, sleep=c.sleep, tz=AMS)
    assert not ok and "has not succeeded since" in line and "waited 30 min" in line
    assert c.now - _ams(2026, 10, 9, 23, 0) >= timedelta(minutes=30)


@pytest.mark.parametrize("rc", [1, A.CHAIN_BLOCKED])
def test_a_failed_or_blocked_predecessor_blocks_at_once(chain, rc):
    _pred_log((_ams(2026, 10, 9, 22, 35), _ams(2026, 10, 9, 22, 40), rc))
    c = Clock(_ams(2026, 10, 9, 23, 0))
    ok, line = A.wait_for_chain(SUCC, clock=c, sleep=c.sleep, tz=AMS)
    assert not ok and c.sleeps == 0 and f"RC-Pred exit {rc}" in line


def test_a_running_predecessor_is_waited_for(chain):
    start = _ams(2026, 10, 9, 22, 35)
    _pred_log((start, None, None))

    def finish_after_ten_minutes(c):
        if c.now >= _ams(2026, 10, 9, 23, 10) and A.last_run(PRED.log_path).rc is None:
            _pred_log((start, c.now, 0))

    c = Clock(_ams(2026, 10, 9, 23, 0), hook=finish_after_ten_minutes)
    ok, line = A.wait_for_chain(SUCC, clock=c, sleep=c.sleep, tz=AMS)
    assert ok and c.sleeps >= 10 and "RC-Pred exit 0" in line


def test_a_predecessor_that_has_not_started_yet_is_waited_for(chain):
    # Both tasks catch up when a sleeping PC wakes; the step may start a moment before the predecessor.
    _pred_log((_ams(2026, 10, 8, 22, 35), _ams(2026, 10, 8, 22, 58), 0))
    t0 = _ams(2026, 10, 10, 7, 0)

    def predecessor_catches_up(c):
        if c.now >= t0 + timedelta(minutes=2):
            _pred_log((_ams(2026, 10, 8, 22, 35), _ams(2026, 10, 8, 22, 58), 0),
                      (t0 + timedelta(minutes=1), c.now, 0))

    c = Clock(t0, hook=predecessor_catches_up)
    ok, _ = A.wait_for_chain(SUCC, clock=c, sleep=c.sleep, tz=AMS)
    assert ok


# --- run_task -------------------------------------------------------------------------------

def test_a_blocked_step_does_not_run_its_command(chain, monkeypatch):
    _pred_log((_ams(2026, 10, 9, 22, 35), _ams(2026, 10, 9, 22, 40), 1))
    ran = []
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: ran.append(a))
    c = Clock(_ams(2026, 10, 9, 23, 0))
    assert A.run_task(SUCC.name, clock=c, sleep=c.sleep, tz=AMS) == A.CHAIN_BLOCKED
    assert ran == []
    text = SUCC.log_path.read_text(encoding="utf-8")
    assert "chain: blocked: RC-Pred exit 1" in text
    assert re.search(rf"=== \S+ exit {A.CHAIN_BLOCKED}\s*$", text)
    assert A.map_result(A.CHAIN_BLOCKED)[0] == "red"


def test_a_released_step_runs_its_command(chain):
    _pred_log((_ams(2026, 10, 9, 22, 35), _ams(2026, 10, 9, 22, 58), 0))
    c = Clock(_ams(2026, 10, 9, 23, 0))
    assert A.run_task(SUCC.name, clock=c, sleep=c.sleep, tz=AMS) == 0
    text = SUCC.log_path.read_text(encoding="utf-8")
    assert "chain: RC-Pred exit 0" in text and "step ran" in text
    assert text.index("chain:") < text.index("step ran")


def test_an_unchained_task_never_reads_another_log(chain, monkeypatch):
    monkeypatch.setattr(A, "last_run", lambda *a, **k: pytest.fail("read a predecessor log"))
    assert A.run_task(PRED.name) == 0


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="needs time.tzset")
def test_without_a_zone_the_cutoff_follows_the_machine_clock(monkeypatch):
    # What a real task uses (tz=None): the machine's own zone, DST included.
    import time
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    time.tzset()
    try:
        got = A.chain_cutoff("22:35", datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc))  # 23:00 CET
        assert got.astimezone(timezone.utc) == datetime(2026, 10, 25, 21, 35, tzinfo=timezone.utc)
        got = A.chain_cutoff("22:35", datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc))   # 23:00 CEST
        assert got.astimezone(timezone.utc) == datetime(2026, 10, 9, 20, 35, tzinfo=timezone.utc)
        # A catch-up the morning after the change: last night's 22:35 was still CEST.
        got = A.chain_cutoff("22:35", datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc))   # 07:00 CET
        assert got.astimezone(timezone.utc) == datetime(2026, 10, 24, 20, 35, tzinfo=timezone.utc)
        # After local midnight the UTC date is still yesterday's; a 00:30 step is today's.
        got = A.chain_cutoff("00:30", datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc))   # 01:00 CEST
        assert got.astimezone(timezone.utc) == datetime(2026, 10, 9, 22, 30, tzinfo=timezone.utc)
    finally:
        if old is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old)
        time.tzset()
