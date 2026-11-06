"""The Health strip's tiles (#492 slice B1, step 4): each in its group, every count with its denominator, the hourly
builder timer's tile, the longest build today, and the By-file lines."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from RevenueChaser.module_settings import control
from RevenueChaser.module_settings.tests.test_control_view import NOW, _build, control_nd

GROUPS = {"trust": {"prices", "stale", "late"}, "builder": {"builder"}, "sources": {"cap"}, "backup": {"backup"}}
TIMER = {"state": "on", "last": "2026-10-30T17:07:00+00:00", "next": "2026-10-30T18:07:00+00:00", "minute": 7}


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def _tiles(view):
    return {t["key"]: t for t in view["health"]["tiles"]}


def test_every_tile_sits_in_its_group():
    tiles = _tiles(_build(timer=lambda: TIMER, job_runs=lambda: [], procs=lambda: []))
    assert {k: t["group"] for k, t in tiles.items()} == {k: g for g, keys in GROUPS.items() for k in keys}


def test_the_counts_carry_their_denominator_and_prices_leave_the_stale_count():
    nd = control_nd()
    t = _tiles(_build(nd, procs=lambda: []))
    assert (t["late"]["n"], t["late"]["label"]) == ("1", "datasets late, of 5")
    assert (t["stale"]["n"], t["stale"]["label"]) == ("1", "datasets stale, of 5")
    nd["datasets"][2]["status"] = "stale"   # wrds/prices serves the bars: its own card says so (Q27)
    t = _tiles(_build(nd, procs=lambda: []))
    assert (t["stale"]["n"], t["stale"]["label"]) == ("1", "other datasets stale, of 5")


def test_the_prices_card_counts_the_sessions_behind_and_is_red_only_then_or_at_zero():
    # NOW is Fri 30 Oct 17:31 UTC (13:31 New York): the last complete session is Thu 29 Oct; prices hold 28 Oct.
    t = _tiles(_build(procs=lambda: []))["prices"]
    assert (t["n"], t["cls"]) == ("1 session behind", "bad")
    assert t["label"] == "prices · last session 28 Oct · 7,380 of 7,670 names have a bar on it"
    assert control.sessions_behind("2026-10-29", NOW) == 0
    assert control.sessions_behind("2026-10-23", NOW) == 4, "Mon to Thu: the weekend is not a session"


def test_an_off_timer_is_a_grey_line_and_an_unreadable_one_is_not_known():
    h = _build(timer=lambda: {**TIMER, "state": "off"}, procs=lambda: [])["health"]
    assert h["timer_line"].startswith("Builder timer off (on purpose) · last run ")
    assert h["timer_line"].endswith(", from the queue")
    assert "the builder timer" not in h["unread"]
    h = _build(timer=lambda: TIMER, procs=lambda: [])["health"]
    assert h["timer_line"] is None and "the builder timer" not in h["unread"]
    h = _build(timer=lambda: {"state": None, "last": None, "next": None, "minute": None}, procs=lambda: [])["health"]
    assert "the builder timer" in h["unread"]


def test_the_all_well_words_and_the_gateway_for_the_top_line():
    h = _build(procs=lambda: [])["health"]
    assert h["ok"][0] == "1/5 fresh" and "builder 24 min ago" in h["ok"]
    assert set(h["gateway"]) == {"cls", "n", "label", "since"}


def test_the_longest_build_today_is_measured_from_the_runs_of_the_new_york_day():
    runs = [{"dataset": "alpaca/bars", "started_at": _ts("2026-10-30T09:00:00+00:00"),
             "finished_at": _ts("2026-10-30T10:11:00+00:00")},
            {"dataset": "sec/13f", "started_at": _ts("2026-10-30T11:00:00+00:00"),
             "finished_at": _ts("2026-10-30T11:20:00+00:00")},
            {"dataset": "uw/financials", "started_at": _ts("2026-10-30T03:00:00+00:00"),   # 23:00 New York, 29 Oct
             "finished_at": _ts("2026-10-30T06:00:00+00:00")},
            {"dataset": "market_series", "started_at": _ts("2026-10-30T12:00:00+00:00"), "finished_at": None}]
    view = _build(job_runs=lambda: runs)
    t = _tiles(view)["longest"]
    assert t["n"] == "71 min" and t["label"] == "longest build today · alpaca/bars"
    assert view["longest_build"] == {"dataset": "alpaca/bars", "seconds": 71 * 60}


def test_no_run_today_is_not_measured_yet():
    view = _build(job_runs=lambda: [], procs=lambda: [])
    assert "longest" not in _tiles(view) and "the longest build today" in view["health"]["unread"]
    assert view["longest_build"] is None


def test_a_job_runs_read_that_fails_is_not_measured():
    def broken():
        raise OSError("locked")
    assert _build(job_runs=broken)["longest_build"] is None


def test_the_health_lists_the_files_in_plan_order_with_their_state():
    view = _build()
    assert [f["name"] for f in view["health"]["files"]] == [f["name"] for f in view["files"]]
    assert all(f["state"] is g["state"] for f, g in zip(view["health"]["files"], view["files"]))
    assert control.health(control_nd(), [], {"size": 0}, None)["files"] == []


# ── The running jobs (Q6, Q16-Q18): read from the process table, never from a guess ────────────────────────────

def _proc(root, pid, argv, unit, ticks=100):
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "stat").write_text(f"{pid} (python3) S " + " ".join(["0"] * 18) + f" {ticks} 0 0\n")
    (d / "cgroup").write_text(f"0::/system.slice/{unit}.service\n")


def test_the_process_table_gives_each_job_its_name_age_limit_and_starter(tmp_path, monkeypatch):
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
    py = "/usr/bin/python3"
    _proc(tmp_path, 11, [py, "-I", "/usr/local/bin/life-with-keys", "sec", "--", py, "-m",
                         "RevenueChaser.rcdata.build", "backfill", "sec/13dg"], "life-claude-rc-b")
    _proc(tmp_path, 12, [py, "-m", "RevenueChaser.rcdata.build", "backfill", "sec/13dg", "--from", "2022-01-01",
                         "--no-duckdb"], "life-claude-rc-b")
    _proc(tmp_path, 13, [py, "-m", "RevenueChaser.core.automations", "run", "RC-Build"], "life-rc-builder")
    _proc(tmp_path, 14, [py, "-c", "from RevenueChaser.app.dashboard import main"], "life-rc-dashboard")
    monkeypatch.setattr(control.os, "sysconf", lambda name: 100)
    # pinned here, so the line is tested apart from RC-Build's real limit (#527 sets that in its own PR)
    from RevenueChaser.core import automations
    build = automations._BY_NAME["RC-Build"]
    monkeypatch.setitem(automations._BY_NAME, "RC-Build", dataclasses.replace(
        build, settings=dataclasses.replace(build.settings, time_limit_minutes=120)))
    procs = control.read_procs(str(tmp_path))
    assert sorted(p["pid"] for p in procs) == [12, 13], "the wrapper and the dashboard are not jobs"
    now = datetime.fromtimestamp(1001 + 100 * 60, timezone.utc)   # each started at btime + 1 s
    jobs = {j["pid"]: j for j in (control._job_line(p, now) for p in procs)}
    assert control.job_words(jobs[12]) == "sec/13dg from 2022 · 1 h 40 min / no limit · Claude session B"
    assert jobs[12]["cls"] == "none", "a session's backfill has no limit anywhere: grey (Q17)"
    assert control.job_words(jobs[13]) == "RC-Build · 1 h 40 min / 2 h 00 min · systemd timer"
    assert jobs[13]["cls"] == "warn", "past 80% of its limit (Q6)"


def test_an_unreadable_process_table_is_not_known_never_nothing_runs(tmp_path):
    assert control.read_procs(str(tmp_path / "absent")) is None
    h = _build(procs=lambda: None)["health"]
    assert h["jobs"] == [] and "the running jobs" in h["unread"]
