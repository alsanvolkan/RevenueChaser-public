"""#158: the Automations panel on Linux, through systemd user timers.

Every call goes through a fake ``systemctl`` runner and a temp unit folder, so the tests stay offline and run the
same on any platform, the way ``WindowsScheduler``'s tests go through a fake PowerShell.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from RevenueChaser.core import automations as A


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(A.config, "OUTPUT_DIR", tmp_path)
    A.set_scheduler(None)
    yield
    A.set_scheduler(None)


class FakeSystemctl:
    def __init__(self, out="", rc=0, err=""):
        self.calls: list[list[str]] = []
        self.out, self.rc, self.err = out, rc, err

    def __call__(self, args):
        self.calls.append(list(args))
        return self.rc, self.out, self.err


def _linux(tmp_path, sc, python="/srv/rc/.venv/bin/python", workdir="/srv/rc"):
    return A.LinuxScheduler(runner=sc, python=python, workdir=workdir, unit_dir=tmp_path / "units")


def _unit(tmp_path, name, kind):
    return (tmp_path / "units" / f"{name}.{kind}").read_text(encoding="utf-8")


def _show(timer: dict, service: dict) -> str:
    """What ``systemctl show <timer> <service>`` prints: one block per unit, a blank line between them."""
    block = lambda d: "\n".join(f"{k}={v}" for k, v in d.items())
    return block(timer) + "\n\n" + block(service) + "\n"


# --- install ----------------------------------------------------------------------

def test_install_writes_one_service_and_one_timer_and_enables_the_timer(tmp_path):
    sc = FakeSystemctl()
    _linux(tmp_path, sc).install(A.entry("RC-Store-Backup"))
    service = _unit(tmp_path, "RC-Store-Backup", "service")
    assert "Type=oneshot" in service
    assert 'ExecStart="/srv/rc/.venv/bin/python" "-m" "RevenueChaser.core.automations" "run" "RC-Store-Backup"' \
        in service
    assert "WorkingDirectory=/srv/rc" in service
    assert "TimeoutStartSec=2h" in service
    timer = _unit(tmp_path, "RC-Store-Backup", "timer")
    assert "OnCalendar=*-*-* 04:00:00" in timer
    assert "Unit=RC-Store-Backup.service" in timer
    assert "Persistent=true" in timer
    # A user timer cannot arm a wake alarm (no CAP_WAKE_ALARM): WakeSystem=true leaves it failed and never run.
    assert "WakeSystem" not in timer
    assert "WantedBy=timers.target" in timer
    assert sc.calls == [["daemon-reload"], ["enable", "--now", "RC-Store-Backup.timer"]]
    assert sorted(p.name for p in (tmp_path / "units").iterdir()) == ["RC-Store-Backup.service",
                                                                       "RC-Store-Backup.timer"]


def test_a_weekly_row_installs_its_days_and_a_row_that_does_not_wake_says_so(tmp_path):
    sc = FakeSystemctl()
    s = _linux(tmp_path, sc)
    s.install(A.entry("RC-Index-Lists"))
    s.install(A.entry("RC-Screeners-Daily"))
    s.install(A.entry("RC-ValueChain-Sync"))
    assert "OnCalendar=Sat *-*-* 10:00:00" in _unit(tmp_path, "RC-Index-Lists", "timer")
    assert "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 23:00:00" in _unit(tmp_path, "RC-Screeners-Daily", "timer")
    vc = _unit(tmp_path, "RC-ValueChain-Sync", "timer")
    assert "OnCalendar=*-*-* 13:00:00" in vc
    for name in ("RC-Index-Lists", "RC-Screeners-Daily", "RC-ValueChain-Sync"):
        assert "WakeSystem" not in _unit(tmp_path, name, "timer")


def test_on_calendar_covers_every_registry_row():
    for e in A.REGISTRY:
        spec = A.on_calendar(e)
        assert spec.endswith(f"*-*-* {e.time}:00")
        assert (spec.count(",") + 1 if e.days else 0) == len(e.days)


def test_the_service_carries_the_resolved_data_roots_so_a_job_writes_where_the_panel_reads(tmp_path, monkeypatch):
    """Without them a job run by systemd falls back to the checkout's own .store/.cache (config._root)."""
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    monkeypatch.setenv("RAW_STORE_BACKUP_DIR", "/srv/backups 1")
    roots = {"RC_STORE_DIR": "DATA_DIR", "RC_CACHE_DIR": "OUTPUT_DIR", "RC_EDGAR_DOCS_DIR": "EDGAR_DOCS_DIR",
             "RC_DISCORD_MEDIA_DIR": "DISCORD_MEDIA_DIR"}
    for var, attr in roots.items():                  # the value this process resolved, set in .env or not at all
        monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(A.config, attr, tmp_path / "resolved" / attr)
    _linux(tmp_path, FakeSystemctl()).install(A.entry("RC-Store-Backup"))
    service = _unit(tmp_path, "RC-Store-Backup", "service")
    for var, value in (*((v, tmp_path / "resolved" / a) for v, a in roots.items()),
                       ("TZ", "Europe/Amsterdam"), ("RAW_STORE_BACKUP_DIR", "/srv/backups 1")):
        assert f'Environment="{var}={value}"' in service, var


def test_no_other_variable_reaches_a_unit_file(tmp_path, monkeypatch):
    """Unit files are world-readable and ``.env`` loads keys into the environment: an allowlist, never a copy."""
    for name in ("UNUSUAL_WHALES_API_KEY", "FRED_API_KEY", "DEEPSEEK_API_KEY", "GITHUB_TOKEN", "SOME_SECRET",
                 "RC_PASSWORD"):
        monkeypatch.setenv(name, "s3cr3t-value-" + name)
    _linux(tmp_path, FakeSystemctl()).install(A.entry("RC-Store-Backup"))
    for kind in ("service", "timer"):
        text = _unit(tmp_path, "RC-Store-Backup", kind)
        assert "s3cr3t-value" not in text
    names = {line.split("=", 1)[1].strip('"').split("=", 1)[0]
             for line in _unit(tmp_path, "RC-Store-Backup", "service").splitlines()
             if line.startswith("Environment=")}
    assert names <= set(A.UNIT_ENVIRONMENT)


def test_install_quotes_paths_and_escapes_systemd_specifiers(tmp_path):
    sc = FakeSystemctl()
    _linux(tmp_path, sc, python='/opt/my "py"/100%/bin/python', workdir="/srv/rc 100%").install(
        A.entry("RC-Store-Backup"))
    service = _unit(tmp_path, "RC-Store-Backup", "service")
    assert 'ExecStart="/opt/my \\"py\\"/100%%/bin/python" "-m"' in service
    assert "WorkingDirectory=/srv/rc 100%%" in service


def test_a_path_with_a_newline_is_refused_before_anything_is_written(tmp_path):
    sc = FakeSystemctl()
    with pytest.raises(A.AutomationError):
        _linux(tmp_path, sc, workdir="/srv/rc\nExecStartPre=/bin/rm").install(A.entry("RC-Store-Backup"))
    assert sc.calls == [] and not (tmp_path / "units").exists()


def test_a_failing_systemctl_call_raises_with_its_message(tmp_path):
    with pytest.raises(A.AutomationError, match="Failed to connect to bus"):
        _linux(tmp_path, FakeSystemctl(rc=1, err="Failed to connect to bus")).install(A.entry("RC-Store-Backup"))


class FailOn(FakeSystemctl):
    """Fails the calls whose first word is in ``verbs``, like a missing bus or a refused unit."""

    def __init__(self, *verbs):
        super().__init__()
        self.verbs = set(verbs)

    def __call__(self, args):
        self.calls.append(list(args))
        return (1, "", "Failed to connect to bus") if args[0] in self.verbs else (0, "", "")


@pytest.mark.parametrize("verb", ["daemon-reload", "enable"])
def test_a_failed_install_leaves_no_unit_file_behind(tmp_path, verb):
    """Otherwise the row reads "error", the panel draws no button for it and the half-installed unit stays."""
    sc = FailOn(verb)
    with pytest.raises(A.AutomationError, match="bus"):
        _linux(tmp_path, sc).install(A.entry("RC-Store-Backup"))
    assert list((tmp_path / "units").iterdir()) == []
    assert sc.calls[-1] == ["daemon-reload"], "the rollback reloads so systemd forgets the units too"


def test_remove_deletes_the_files_even_when_the_bus_is_down_and_still_reports_it(tmp_path):
    s = _linux(tmp_path, FakeSystemctl())
    s.install(A.entry("RC-Store-Backup"))
    s._run = FailOn("disable", "daemon-reload")
    with pytest.raises(A.AutomationError, match="bus"):
        s.remove("RC-Store-Backup")
    assert list((tmp_path / "units").iterdir()) == []
    assert s.status("RC-Store-Backup") is None


# --- the other four buttons ---------------------------------------------------------

def test_enable_disable_run_and_remove_name_exactly_one_registry_unit(tmp_path):
    sc = FakeSystemctl()
    s = _linux(tmp_path, sc)
    s.install(A.entry("RC-Relatedness-OffPeak"))
    sc.calls.clear()
    s.set_enabled("RC-Relatedness-OffPeak", False)
    s.set_enabled("RC-Relatedness-OffPeak", True)
    s.run("RC-Relatedness-OffPeak")
    s.remove("RC-Relatedness-OffPeak")
    assert sc.calls == [
        ["disable", "--now", "RC-Relatedness-OffPeak.timer"],
        ["enable", "--now", "RC-Relatedness-OffPeak.timer"],
        ["start", "--no-block", "RC-Relatedness-OffPeak.service"],
        ["disable", "--now", "RC-Relatedness-OffPeak.timer"],
        ["daemon-reload"],
    ]
    assert list((tmp_path / "units").iterdir()) == []


def test_a_name_outside_the_registry_is_refused_without_a_call(tmp_path):
    sc = FakeSystemctl()
    s = _linux(tmp_path, sc)
    for call in (lambda: s.status("Other"), lambda: s.remove("Other"), lambda: s.run("x; rm -rf ~"),
                 lambda: s.set_enabled("Other", False)):
        with pytest.raises(A.UnknownTask):
            call()
    assert sc.calls == []


def test_remove_leaves_other_unit_files_alone(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "someone-else.timer").write_text("[Timer]\n", encoding="utf-8")
    s = _linux(tmp_path, FakeSystemctl())
    s.install(A.entry("RC-Store-Backup"))
    s.remove("RC-Store-Backup")
    assert [p.name for p in units.iterdir()] == ["someone-else.timer"]


# --- status -----------------------------------------------------------------------

_TIMER_ON = {"ActiveState": "active", "Result": "success", "NextElapseUSecRealtime": "@1792987200",
             "LastTriggerUSec": "@1792900800"}
_SERVICE_OK = {"ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0",
               "ExecMainStartTimestamp": "@1792900800"}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat()


def test_status_is_none_when_the_units_are_not_installed_and_calls_nothing(tmp_path):
    sc = FakeSystemctl()
    assert _linux(tmp_path, sc).status("RC-Store-Backup") is None
    assert sc.calls == []


def test_status_reads_one_show_call_for_both_units(tmp_path):
    sc = FakeSystemctl()
    s = _linux(tmp_path, sc)
    s.install(A.entry("RC-Store-Backup"))
    sc.calls.clear()
    sc.out = _show(_TIMER_ON, _SERVICE_OK)
    st = s.status("RC-Store-Backup")
    assert len(sc.calls) == 1
    call = sc.calls[0]
    assert call[0] == "show" and "--timestamp=unix" in call
    assert call[-2:] == ["RC-Store-Backup.timer", "RC-Store-Backup.service"]
    assert st == A.TaskStatus(state="ready", last_run=_iso(1792900800), next_run=_iso(1792987200),
                              last_result=A.RESULT_OK, missed_runs=0)


@pytest.mark.parametrize("timer, service, state, result", [
    ({**_TIMER_ON, "ActiveState": "inactive", "NextElapseUSecRealtime": ""}, _SERVICE_OK, "disabled", A.RESULT_OK),
    (_TIMER_ON, {**_SERVICE_OK, "ActiveState": "activating"}, "running", A.RESULT_RUNNING),
    (_TIMER_ON, {**_SERVICE_OK, "Result": "exit-code", "ExecMainStatus": "5"}, "ready", 5),
    (_TIMER_ON, {**_SERVICE_OK, "Result": "timeout", "ExecMainStatus": "0"}, "ready", 1),
    ({**_TIMER_ON, "LastTriggerUSec": "n/a"}, {**_SERVICE_OK, "ExecMainStartTimestamp": ""}, "ready",
     A.RESULT_NOT_YET_RUN),
    # The user manager restarted since the last run: the timer remembers it, the service does not.
    (_TIMER_ON, {**_SERVICE_OK, "ExecMainStartTimestamp": ""}, "ready", None),
])
def test_status_maps_systemd_state_to_the_panel_codes(tmp_path, timer, service, state, result):
    sc = FakeSystemctl()
    s = _linux(tmp_path, sc)
    s.install(A.entry("RC-Store-Backup"))
    sc.out = _show(timer, service)
    st = s.status("RC-Store-Backup")
    assert (st.state, st.last_result) == (state, result)


@pytest.mark.parametrize("timer", [
    {**_TIMER_ON, "ActiveState": "failed", "Result": "resources"},
    {**_TIMER_ON, "ActiveState": "inactive", "Result": "resources"},
])
def test_a_failed_timer_is_an_error_row_not_a_disabled_one(tmp_path, timer):
    sc = FakeSystemctl()
    s = _linux(tmp_path, sc)
    s.install(A.entry("RC-Store-Backup"))
    sc.out = _show(timer, _SERVICE_OK)
    with pytest.raises(A.AutomationError, match="resources"):
        s.status("RC-Store-Backup")
    row = A.live_row(A.entry("RC-Store-Backup"), s)
    assert row["state"] == "error" and "resources" in row["result_text"]


def test_an_unreadable_show_answer_raises(tmp_path):
    s = _linux(tmp_path, FakeSystemctl())
    s.install(A.entry("RC-Store-Backup"))
    s._run = FakeSystemctl(out="garbage")
    with pytest.raises(A.AutomationError):
        s.status("RC-Store-Backup")


def test_a_failed_row_reads_red_on_the_panel(tmp_path):
    sc = FakeSystemctl()
    s = _linux(tmp_path, sc)
    s.install(A.entry("RC-Store-Backup"))
    sc.out = _show(_TIMER_ON, {**_SERVICE_OK, "Result": "exit-code", "ExecMainStatus": "2"})
    row = A.live_row(A.entry("RC-Store-Backup"), s)
    assert row["state"] == "enabled" and row["result_kind"] == "red"


# --- which scheduler a host gets -----------------------------------------------------

@pytest.fixture
def linux_host(monkeypatch):
    monkeypatch.setattr(A.sys, "platform", "linux")
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.delenv("RC_SCHEDULER", raising=False)


def test_linux_gets_the_systemd_backend_only_when_asked_for(linux_host, monkeypatch):
    """Opt-in: a server that already runs these jobs as system timers must not be offered a second schedule."""
    assert isinstance(A.get_scheduler(), A.UnavailableScheduler)
    A.set_scheduler(None)
    monkeypatch.setenv("RC_SCHEDULER", "systemd-user")
    assert isinstance(A.get_scheduler(), A.LinuxScheduler)


def test_an_unknown_scheduler_choice_stays_unavailable(linux_host, monkeypatch):
    monkeypatch.setenv("RC_SCHEDULER", "systemd")
    assert isinstance(A.get_scheduler(), A.UnavailableScheduler)


def test_systemctl_finds_the_user_bus_when_xdg_runtime_dir_is_missing(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, env=kw.get("env"))
        return A.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(A.subprocess, "run", run)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(A.os, "getuid", lambda: 1234, raising=False)
    A._systemctl(["daemon-reload"])
    assert seen["cmd"] == ["systemctl", "--user", "daemon-reload"]
    assert seen["env"]["XDG_RUNTIME_DIR"] == "/run/user/1234"
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/77")
    A._systemctl(["daemon-reload"])
    assert seen["env"]["XDG_RUNTIME_DIR"] == "/run/user/77"


def test_windows_is_unchanged(monkeypatch):
    monkeypatch.setattr(A.sys, "platform", "win32")
    assert isinstance(A.get_scheduler(), A.WindowsScheduler)


def test_the_default_unit_folder_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert A.LinuxScheduler(runner=FakeSystemctl()).unit_dir == tmp_path / "cfg" / "systemd" / "user"


# --- #164: scripts ask the code for the task list -------------------------------------

def test_names_prints_every_registry_name_one_per_line_without_the_scheduler(capsys):
    f = A.FakeScheduler()
    A.set_scheduler(f)
    assert A.main(["names"]) == 0
    assert capsys.readouterr().out.splitlines() == [e.name for e in A.REGISTRY]
    assert f.calls == []
