"""The hourly builder timer on the control board (#492 slice B1, step 1): ``view["timer"]`` from the scheduler's
``RC-Build`` row, its minute from the entry's ``OnCalendar``, all None when it cannot be read."""
from __future__ import annotations

from RevenueChaser.core import automations
from RevenueChaser.module_settings import control
from RevenueChaser.module_settings.tests.test_control_view import _build

NONE = {"state": None, "last": None, "next": None, "minute": None}


class FakeScheduler(automations.Scheduler):
    def __init__(self, status=None, error=None):
        self._status, self._error, self.asked = status, error, []

    def status(self, name):
        self.asked.append(name)
        if self._error:
            raise self._error
        return self._status


def _status(state):
    return automations.TaskStatus(state=state, last_run="2026-10-30T17:07:01+00:00",
                                  next_run="2026-10-30T18:07:00+00:00")


def test_an_active_timer_is_on_with_its_passes_and_the_minute_of_its_calendar():
    s = FakeScheduler(_status("ready"))
    got = control.read_timer(s)
    assert s.asked == ["RC-Build"]
    assert got == {"state": "on", "last": "2026-10-30T17:07:01+00:00", "next": "2026-10-30T18:07:00+00:00",
                   "minute": 7}
    assert control.read_timer(FakeScheduler(_status("running")))["state"] == "on"


def test_the_minute_is_the_entrys_own_not_a_fixed_number(monkeypatch):
    e = automations.entry("RC-Build")
    monkeypatch.setattr(automations, "on_calendar", lambda entry: "*-*-* *:23:00" if entry is e else "")
    assert control.read_timer(FakeScheduler(_status("ready")))["minute"] == 23


def test_a_disabled_timer_is_off():
    assert control.read_timer(FakeScheduler(_status("disabled")))["state"] == "off"


def test_a_failed_or_missing_read_is_all_none():
    assert control.read_timer(FakeScheduler(error=automations.AutomationError("the timer failed"))) == NONE
    assert control.read_timer(FakeScheduler(None)) == NONE, "not installed: nothing to read"
    assert control.read_timer(automations.UnavailableScheduler()) == NONE


def test_build_carries_the_timer_and_the_rows_read_it():
    view = _build(timer=lambda: control.read_timer(FakeScheduler(_status("disabled"))))
    assert view["timer"]["state"] == "off" and view["timer"]["minute"] == 7
    rows = {r["dataset"]: r for f in view["files"] for r in f["datasets"]}
    assert rows["market_series"]["next_words"].startswith("timer off · ")


def test_build_with_a_reader_that_raises_says_not_known():
    def broken():
        raise RuntimeError("systemctl gone")
    view = _build(timer=broken)
    assert view["timer"] == NONE
    assert {t["key"]: t for t in view["health"]["tiles"]}["timer"]["n"] == "not known"


def test_build_reads_the_scheduler_by_default(monkeypatch):
    monkeypatch.setattr(automations, "get_scheduler", lambda: FakeScheduler(_status("ready")))
    assert _build()["timer"]["state"] == "on"
