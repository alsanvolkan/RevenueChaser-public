"""The Health strip's tiles (#492 slice B1, step 4): each in its group, every count with its denominator, the hourly
builder timer's tile, the longest build today, and the By-file lines."""
from __future__ import annotations

from datetime import datetime, timezone

from RevenueChaser.module_settings import control
from RevenueChaser.module_settings.tests.test_control_view import NOW, _build, control_nd

GROUPS = {"data": {"fresh", "late", "stale", "never", "problems", "session", "universe"},
          "sources": {"cap"}, "scheduled": {"timer", "builder", "queue"}, "build": {"longest", "backup"}}
TIMER = {"state": "on", "last": "2026-10-30T17:07:00+00:00", "next": "2026-10-30T18:07:00+00:00", "minute": 7}


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def _tiles(view):
    return {t["key"]: t for t in view["health"]["tiles"]}


def test_every_tile_sits_in_its_group():
    tiles = _tiles(_build(timer=lambda: TIMER, job_runs=lambda: []))
    assert {k: t["group"] for k, t in tiles.items()} == {k: g for g, keys in GROUPS.items() for k in keys}


def test_every_count_carries_its_denominator():
    nd = control_nd()
    nd["queue"] = {"active": [{"status": "running"}, {"status": "queued"}, {"status": "queued"}], "finished": []}
    t = _tiles(_build(nd))
    assert (t["fresh"]["n"], t["fresh"]["label"]) == ("1", "of 5 datasets fresh")
    assert (t["late"]["n"], t["late"]["label"]) == ("1", "of 5 datasets late")
    assert (t["stale"]["n"], t["stale"]["label"]) == ("1", "of 5 datasets stale")
    assert (t["never"]["n"], t["never"]["label"]) == ("2", "of 5 datasets never ran")
    assert (t["problems"]["n"], t["problems"]["label"]) == ("10", "open problems, in 2 of 5 datasets")
    assert (t["queue"]["n"], t["queue"]["label"]) == ("3", "1 running, 2 queued")
    assert f"{t['universe']['n']} of 7,670 names have a bar on it" in t["session"]["label"], "the universe's filled count"


def test_the_timer_tile_says_on_off_or_not_known_with_its_passes_in_amsterdam_time():
    t = _tiles(_build(timer=lambda: TIMER))["timer"]
    assert t["n"] == "on" and t["label"] == "hourly builder timer · last pass 18:07 · next 19:07"
    assert _tiles(_build(timer=lambda: {**TIMER, "state": "off"}))["timer"]["n"] == "off"
    unknown = _tiles(_build(timer=lambda: {"state": None, "last": None, "next": None, "minute": None}))["timer"]
    assert unknown["n"] == "not known" and unknown["label"] == "hourly builder timer · last pass — · next —"


def test_the_longest_build_today_is_measured_from_the_runs_of_the_new_york_day():
    runs = [{"dataset": "alpaca/bars", "started_at": _ts("2026-10-30T09:00:00+00:00"),
             "finished_at": _ts("2026-10-30T10:11:00+00:00")},
            {"dataset": "sec/13f", "started_at": _ts("2026-10-30T11:00:00+00:00"),
             "finished_at": _ts("2026-10-30T11:20:00+00:00")},
            {"dataset": "sec/filings", "started_at": _ts("2026-10-30T03:00:00+00:00"),     # 23:00 New York, 29 Oct
             "finished_at": _ts("2026-10-30T06:00:00+00:00")},
            {"dataset": "market_series", "started_at": _ts("2026-10-30T12:00:00+00:00"), "finished_at": None}]
    view = _build(job_runs=lambda: runs)
    t = _tiles(view)["longest"]
    assert t["n"] == "71 min" and t["label"] == "longest build today · alpaca/bars"
    assert view["longest_build"] == {"dataset": "alpaca/bars", "seconds": 71 * 60}


def test_no_run_today_is_not_measured_yet():
    view = _build(job_runs=lambda: [])
    t = _tiles(view)["longest"]
    assert (t["n"], t["label"]) == ("not measured yet", "longest build today")
    assert view["longest_build"] is None


def test_a_job_runs_read_that_fails_is_not_measured():
    def broken():
        raise OSError("locked")
    view = _build(job_runs=broken)
    assert view["longest_build"] is None
    t = _tiles(view)["longest"]   # #529: an unreadable store is not the same as no run today
    assert (t["cls"], t["n"], t["label"]) == ("warn", "could not read", "longest build today · job_runs2 could not be read")


def test_the_health_lists_the_files_in_plan_order_with_their_state():
    view = _build()
    assert [f["name"] for f in view["health"]["files"]] == [f["name"] for f in view["files"]]
    assert all(f["state"] is g["state"] for f, g in zip(view["health"]["files"], view["files"]))
    assert control.health(control_nd(), [], {"size": 0}, None)["files"] == []
