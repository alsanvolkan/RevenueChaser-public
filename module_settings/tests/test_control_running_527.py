"""#527 step 6 + 7: each running request reads "name · elapsed / limit · who started it", and the queue table is
readable (names whole, Stop beside the status, one Asked column with its time, a long note cut to one line).

Who started it is read from the process's cgroup: a ``life-rc-*`` systemd unit has the registry's limit (none for the
builder), a process under the Claude Code session's unit has the 2 h cap a background command gets, anything else
is "unknown". A running row whose process is gone reads "probably lost"; closing it stays the sweep's job."""
from __future__ import annotations

from RevenueChaser.module_settings import control, control_render
from RevenueChaser.module_settings.tests.test_control_render_c1 import _view
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd

T = NOW.timestamp()
CG = {101: "0::/system.slice/life-rc-builder.service",
      202: "0::/system.slice/life-claude-rc.service",
      303: "0::/user.slice/user-1000.slice/session-4.scope",
      404: "0::/system.slice/life-rc-store-backup.service"}


def _nd(*active):
    nd = control_nd()
    nd["queue"] = {"active": list(active), "finished": []}
    return nd


def _req(i, ds, mins, status="running", by="cli"):
    return {"id": i, "dataset": ds, "status": status, "requested_by": by, "requested_at": T - mins * 60 - 5,
            "started_at": T - mins * 60, "note": None}


def _run(pid, ds, mins):
    return {"run_id": f"r{pid}", "dataset": ds, "pid": pid, "started_at": T - mins * 60, "status": "running"}


def _running(nd, runs, alive=lambda pid: pid in CG):
    v = control.build(nd, now=NOW, runs_now=lambda: runs, cgroup=CG.get, alive=alive)
    return {r["id"]: r for r in v["running"]}, v


def test_the_builder_unit_has_no_limit_and_a_claude_session_has_two_hours():
    nd = _nd(_req(1, "sec/13f", 30), _req(2, "sec/13dg", 100))
    got, v = _running(nd, [_run(101, "sec/13f", 30), _run(202, "sec/13dg", 100)])
    assert got[1]["who"] == "systemd life-rc-builder" and got[1]["limit_s"] is None
    assert got[2]["who"] == "Claude session" and got[2]["limit_s"] == 2 * 3600
    assert got[1]["line"] == "sec/13f · 30 min / no limit · systemd life-rc-builder"
    assert got[2]["line"] == "sec/13dg · 1 h 40 min / 2 h · Claude session"
    assert got[2]["cls"] == "warn", "past 80 % of its limit"
    alerts = {a["key"]: a for a in v["health"]["alerts"]}
    assert "sec/13dg" in alerts["running"]["label"] and "Claude session" in alerts["running"]["label"]


def test_an_unknown_starter_and_a_lost_process_say_so():
    nd = _nd(_req(1, "sec/13dg", 200), _req(2, "sec/form4", 10))
    got, _ = _running(nd, [_run(999, "sec/13dg", 200), _run(303, "sec/form4", 10)])
    assert got[1]["who"] == "process gone · probably lost" and got[1]["cls"] == "none"
    assert got[2]["who"] == "unknown" and got[2]["limit_s"] is None


def test_the_queue_table_is_readable():
    v = _view()
    long_note = "skipped: " + "x" * 400
    v["queue"] = {"active": [_req(1, "sec/13dg", 30)],
                  "finished": [{"id": 2, "dataset": "sec/13f", "status": "skipped", "requested_by": "cli",
                                "requested_at": T - 600, "note": long_note}]}
    v["running"] = [{"id": 1, "line": "sec/13dg · 30 min / 2 h · Claude session", "cls": "ok"}]
    q = control_render._queue(v)
    assert q.count("<th>Asked") == 1 and "<th>Asked by</th>" not in q, "one Asked column: who and when"
    assert '<code class="dsn">sec/13dg</code>' in q
    assert '<span class="stbtn">running' in q, "Stop sits beside the status"
    assert "30 min / 2 h · Claude session" in q
    assert "<details class=\"note\"><summary>" in q and long_note in q, "a long note is cut, the whole is one tap away"
