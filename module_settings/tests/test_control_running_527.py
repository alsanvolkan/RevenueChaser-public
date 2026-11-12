"""#527 step 6 + 7: each running request reads "name · elapsed / limit · who started it", and the queue table is
readable (names whole, Stop beside the status, one Asked column with its time, a long note cut to one line).

Who started it is read from the process's cgroup: a ``life-rc-*`` systemd unit has the registry's limit (none for the
builder), a process under the Claude Code session's unit has the 2 h cap a background command gets, anything else
is "unknown". A running row whose process is gone reads "probably lost"; closing it stays the sweep's job."""
from __future__ import annotations

from RevenueChaser.module_settings import control
from RevenueChaser.module_settings.tests.test_control_view import NOW, control_nd

T = NOW.timestamp()
CG = {101: "0::/system.slice/life-rc-builder.service",
      202: "0::/system.slice/life-claude-remote-control-b.service/tmux.scope",
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


def test_the_builder_unit_and_a_claude_session_each_carry_their_two_hour_limit():
    """RC-Build's limit is 120 min since #534 (avo 6 Nov 17:01, Q56); the board reads it from the registry."""
    nd = _nd(_req(1, "sec/13f", 30), _req(2, "sec/13dg", 100))
    got, v = _running(nd, [_run(101, "sec/13f", 30), _run(202, "sec/13dg", 100)])
    assert got[1]["who"] == "systemd life-rc-builder" and got[1]["limit_s"] == 120 * 60
    assert got[2]["who"] == "Claude session" and got[2]["limit_s"] == 2 * 3600
    assert got[1]["line"] == "sec/13f · 30 min / 2 h · systemd life-rc-builder"
    assert got[2]["line"] == "sec/13dg · 1 h 40 min / 2 h · Claude session"
    assert got[2]["cls"] == "warn", "past 80 % of its limit"
    alerts = {a["key"]: a for a in v["health"]["alerts"]}
    assert "sec/13dg" in alerts["running"]["label"] and "Claude session" in alerts["running"]["label"]


def test_every_remote_control_unit_name_is_a_claude_session():
    """The homesrv sessions run under life-claude-remote-control-{a,b} (renamed from life-claude-rc-{a,b} on
    9 Nov 2026); until the rename window both spellings are live, and the name before 28 Oct is gone."""
    for unit, who in (("life-claude-remote-control-a.service", "Claude session"),
                      ("life-claude-remote-control-b.service", "Claude session"),
                      ("life-claude-rc-a.service", "Claude session"), ("life-claude-rc-b.service", "Claude session"),
                      ("life-claude-rc.service", "unknown")):
        nd = _nd(_req(1, "sec/13dg", 10))
        v = control.build(nd, now=NOW, runs_now=lambda: [_run(505, "sec/13dg", 10)],
                          cgroup=lambda pid: f"0::/system.slice/{unit}", alive=lambda pid: True)
        assert v["running"][0]["who"] == who, unit


def test_an_unknown_starter_and_a_lost_process_say_so():
    nd = _nd(_req(1, "sec/13dg", 200), _req(2, "sec/form4", 10))
    got, _ = _running(nd, [_run(999, "sec/13dg", 200), _run(303, "sec/form4", 10)])
    assert got[1]["who"] == "process gone · probably lost" and got[1]["cls"] == "none"
    assert got[2]["who"] == "unknown" and got[2]["limit_s"] is None
