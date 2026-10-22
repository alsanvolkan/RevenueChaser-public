"""Automations registry, scheduler interface and the Windows installer (#130).

Windows-only code is tested through a fake PowerShell runner: nothing here touches a
real Task Scheduler.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from RevenueChaser.core import automations as A

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(A.config, "OUTPUT_DIR", tmp_path)
    A.set_scheduler(None)
    yield
    A.set_scheduler(None)


# --- registry ---------------------------------------------------------------

def test_registry_names_are_stable_and_prefixed():
    assert [e.name for e in A.REGISTRY] == [
        "RC-Store-Backup", "RC-Relatedness-OffPeak", "RC-ValueChain-Sync", "RC-Daily-Bars", "RC-Screeners-Daily",
        "RC-Forecast-Outcomes", "RC-Index-Lists", "RC-Earnings-Inputs"]
    assert all(e.name.startswith("RC-") for e in A.REGISTRY)


def test_the_index_lists_row_runs_weekly_on_saturday():
    """#302 (D24): the index lists step runs Saturday 10:00; every other row stays daily."""
    e = A.entry("RC-Index-Lists")
    assert (e.time, e.days) == ("10:00", ("Saturday",))
    assert e.schedule == "weekly Saturday 10:00 (local)"
    assert e.args == ("-m", "RevenueChaser.scripts.index_lists", "--weekly")
    assert set(e.tables_written) == {"index_constituents", "index_constituents_changes"}
    assert not e.default_install
    assert all(x.days == () and x.schedule == f"daily {x.time} (local)" for x in A.REGISTRY
               if x.name not in (e.name, "RC-Screeners-Daily", "RC-Forecast-Outcomes", "RC-Earnings-Inputs"))


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


def test_the_screeners_row_is_chained_on_the_daily_bars():
    """#129 A1: S2 (the universe CLI) waits for S1 (RC-Daily-Bars) on weekdays, not before 23:00."""
    e = A.entry("RC-Screeners-Daily")
    assert (e.after, e.time, e.days) == ("RC-Daily-Bars", "23:00", _WEEKDAYS)
    assert e.args == ("-m", "RevenueChaser.app.universe_cli")
    assert e.schedule == "weekly Monday, Tuesday, Wednesday, Thursday, Friday 23:00 (local), after RC-Daily-Bars"
    assert not e.default_install
    s = e.settings
    assert s.wake_to_run and s.allow_on_battery and s.start_when_available and s.run_only_when_logged_on
    assert s.time_limit_hours == 4 and e.chain_wait_minutes < s.time_limit_hours * 60
    assert {"equity_universe", "forecast_ledger", "swing_ohlc", "job_runs"} <= set(e.tables_written)
    assert not A.entry("RC-Daily-Bars").days       # the chain needs a daily predecessor
    assert [x.name for x in A.REGISTRY if x.after] == ["RC-Screeners-Daily", "RC-Forecast-Outcomes",
                                                       "RC-Earnings-Inputs"]


def test_the_daily_bars_row_first_stores_the_sec_files():
    """#301 F0: RC-Daily-Bars runs the SEC exchange and fund files as its best-effort first step; it stays daily,
    so the chain on it still holds."""
    from RevenueChaser.core import sec_files

    e = A.entry("RC-Daily-Bars")
    assert e.args[:4] == ("-m", "RevenueChaser.scripts.daily_bars", "--nightly", "--sec-files")
    assert (e.time, e.days, e.after) == ("22:35", (), None)
    assert {"price_bars_daily", "price_actions", "price_checks", "sec_exchange_file",
            "sec_mf_file"} <= set(e.tables_written)
    assert {f.table for f in sec_files.FILES.values()} <= set(e.tables_written)
    A.check_chain(A.REGISTRY)


def test_the_daily_bars_row_then_stores_the_spx_levels():
    """M-SPX-0 (#293, #376): after the bars, best effort, the ^GSPC daily levels (D03) into prices.duckdb, at most
    1 yfinance request a night (avo's typed cap, 2026-10-16); the one-time fill is never part of the task."""
    from RevenueChaser.core import index_levels, rawstore

    e = A.entry("RC-Daily-Bars")
    assert e.args == ("-m", "RevenueChaser.scripts.daily_bars", "--nightly", "--sec-files", "--index-levels")
    assert "--index-fill" not in e.args and "--fill" not in e.args
    assert set(e.tables_written) == {"price_bars_daily", "price_actions", "price_checks",
                                     "sec_exchange_file", "sec_mf_file", "index_levels_daily"}
    assert rawstore._db_of("index_levels_daily") == "prices"
    assert index_levels.SYMBOL in e.description and "1 yfinance request a night" in e.description


def test_the_forecast_outcomes_row_is_chained_daily_on_the_daily_bars():
    """S0 (#380): the outcomes writer reads D01, so it waits for tonight's RC-Daily-Bars, every day at 23:45, clear of
    RC-Screeners-Daily (23:00 to about 23:15), and writes forecast_outcomes alone."""
    e = A.entry("RC-Forecast-Outcomes")
    assert (e.after, e.time, e.days) == ("RC-Daily-Bars", "23:45", ())
    assert e.args == ("-m", "RevenueChaser.scripts.forecast_outcomes", "--nightly")
    assert e.schedule == "daily 23:45 (local), after RC-Daily-Bars"
    assert e.tables_written == ("forecast_outcomes",)
    assert e.chain_wait_minutes == 120 and not e.default_install and not e.offpeak_gated


def test_each_busy_capable_nightly_row_says_its_busy_exit_in_the_description():
    """S-1 (#26): Task Scheduler shows exit 3 as a red "failed (0x3)", so the row itself has to say what it means.

    The wording follows RC-Screeners-Daily's "A busy universe lock exits 3."
    """
    for name in ("RC-Forecast-Outcomes", "RC-Earnings-Inputs"):
        description = A.entry(name).description
        assert "exits 3" in description and "nothing was written" in description, name


def test_the_outcomes_row_cannot_chain_on_the_weekday_screeners_row():
    """``check_chain`` refuses a weekly predecessor; RC-Screeners-Daily runs on weekdays only."""
    import dataclasses
    rows = tuple(dataclasses.replace(e, after="RC-Screeners-Daily") if e.name == "RC-Forecast-Outcomes" else e
                 for e in A.REGISTRY)
    with pytest.raises(ValueError, match="runs weekly"):
        A.check_chain(rows)


# --- nightly inputs steps (C25a/K11, #383) ------------------------------------------------------------------------

def _inputs_entry(**kw):
    import dataclasses
    base = A.AutomationEntry(
        name="RC-Test-Inputs", label="x", time="01:30", description="d", after="RC-Daily-Bars",
        args=("-m", A.INPUTS_CLI, "fake", "--max-uw-calls", "200", "--max-fred-calls", "10", "--max-yf-calls", "5"),
        tables_written=("short_interest",))
    return dataclasses.replace(base, **kw)


@pytest.fixture
def fake_step(monkeypatch):
    from RevenueChaser.core import nightly_inputs
    monkeypatch.setattr(nightly_inputs, "STEPS", {**nightly_inputs.STEPS, "fake": "x:y"})


def test_check_inputs_steps_passes_on_the_registry():
    A.check_inputs_steps(A.REGISTRY)


def test_an_inputs_step_with_every_cap_stated_passes(fake_step):
    A.check_inputs_steps(A.REGISTRY + (_inputs_entry(),))


def test_an_inputs_step_must_run_after_the_daily_bars(fake_step):
    with pytest.raises(ValueError, match="after RC-Daily-Bars"):
        A.check_inputs_steps(A.REGISTRY + (_inputs_entry(after=None),))
    with pytest.raises(ValueError, match="after RC-Daily-Bars"):
        A.check_inputs_steps(A.REGISTRY + (_inputs_entry(after="RC-Store-Backup"),))


def test_an_inputs_step_must_name_a_known_step(fake_step):
    with pytest.raises(ValueError, match="unknown inputs step"):
        A.check_inputs_steps(A.REGISTRY + (_inputs_entry(args=("-m", A.INPUTS_CLI, "nope", "--max-uw-calls", "1",
                                                                 "--max-fred-calls", "0", "--max-yf-calls", "0")),))


@pytest.mark.parametrize("drop", A.INPUTS_CAP_FLAGS)
def test_an_inputs_step_must_state_every_cap(fake_step, drop):
    args = list(_inputs_entry().args)
    i = args.index(drop)
    del args[i:i + 2]
    with pytest.raises(ValueError, match=drop):
        A.check_inputs_steps(A.REGISTRY + (_inputs_entry(args=tuple(args)),))


@pytest.mark.parametrize("value", ["-1", "lots", ""])
def test_an_inputs_cap_must_be_a_whole_number_of_calls(fake_step, value):
    args = list(_inputs_entry().args)
    args[args.index("--max-uw-calls") + 1] = value
    with pytest.raises(ValueError, match="--max-uw-calls"):
        A.check_inputs_steps(A.REGISTRY + (_inputs_entry(args=tuple(args)),))


def test_an_inputs_step_must_list_its_tables(fake_step):
    with pytest.raises(ValueError, match="tables"):
        A.check_inputs_steps(A.REGISTRY + (_inputs_entry(tables_written=()),))


def test_the_screeners_task_installs_a_weekday_trigger():
    ps = FakePS()
    _win(ps).install(A.entry("RC-Screeners-Daily"))
    script = ps.scripts[0]
    assert "-Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '23:00'" in script
    assert "-WakeToRun" in script and "-StartWhenAvailable" in script and "-AllowStartIfOnBatteries" in script
    assert "run RC-Screeners-Daily" in script


def test_the_backup_row_carries_the_settings_that_make_it_run():
    s = A.entry("RC-Store-Backup").settings
    assert s.allow_on_battery and s.wake_to_run and s.start_when_available
    assert s.run_only_when_logged_on


def test_only_the_backup_is_marked_default_install():
    assert [e.name for e in A.REGISTRY if e.default_install] == ["RC-Store-Backup"]


def test_log_path_derives_from_output_dir(tmp_path):
    assert A.entry("RC-Store-Backup").log_path == tmp_path / "logs" / "RC-Store-Backup.log"


def test_unknown_name_is_rejected():
    with pytest.raises(A.UnknownTask):
        A.entry("RC-Nope")
    with pytest.raises(A.UnknownTask):
        A.entry(None)  # type: ignore[arg-type]


def _ps_registrations(path: Path) -> set[str]:
    return set(re.findall(r"RC-[A-Za-z-]+", path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("ps1", ["register_tasks.ps1", "scripts/register_offpeak_tasks.ps1"])
def test_ps1_files_register_only_registry_names(ps1):
    names = {e.name for e in A.REGISTRY}
    assert _ps_registrations(ROOT / ps1) <= names


def test_ps1_files_do_not_define_their_own_tasks():
    for ps1 in ("register_tasks.ps1", "scripts/register_offpeak_tasks.ps1"):
        text = (ROOT / ps1).read_text(encoding="utf-8")
        assert "Register-ScheduledTask" not in text
        assert "automations" in text


# --- result mapping + row states ------------------------------------------------

@pytest.mark.parametrize("code,kind", [
    (None, "none"), (0, "ok"), (0x41303, "neutral"), (0x41301, "running"),
    (1, "red"), (0x80070002, "red")])
def test_result_mapping(code, kind):
    assert A.map_result(code)[0] == kind


def _row(sched, name="RC-Store-Backup"):
    return A.live_row(A.entry(name), sched, NOW)


def test_row_not_installed():
    r = _row(A.FakeScheduler())
    assert r["state"] == "not_installed" and r["result_kind"] == "none"


def test_row_unavailable_host():
    r = _row(A.FakeScheduler(available=False))
    assert r["state"] == "unavailable"


def test_row_disabled_and_enabled_and_running():
    mk = lambda **kw: A.FakeScheduler(tasks={"RC-Store-Backup": A.TaskStatus(**kw)})  # noqa: E731
    assert _row(mk(state="disabled"))["state"] == "disabled"
    assert _row(mk(state="ready", last_result=0))["state"] == "enabled"
    assert _row(mk(state="running"))["state"] == "running"
    assert _row(mk(state="ready", last_result=0x41301))["state"] == "running"


def test_never_run_task_is_neutral_not_red():
    r = _row(A.FakeScheduler(tasks={"RC-Store-Backup": A.TaskStatus(last_result=0x41303)}))
    assert r["result_kind"] == "neutral"


def test_failed_last_run_is_red_with_missed_runs():
    r = _row(A.FakeScheduler(tasks={"RC-Store-Backup": A.TaskStatus(
        last_result=1, missed_runs=3, last_run="2026-10-02T04:00:00+00:00")}))
    assert r["result_kind"] == "red" and r["missed_runs"] == 3
    assert r["last_success"] is None and r["last_success_age"] == "never"


def test_last_success_age_comes_from_the_log():
    e = A.entry("RC-Store-Backup")
    e.log_path.parent.mkdir(parents=True)
    e.log_path.write_text(
        "=== 2026-10-01T04:00:00+00:00 start RC-Store-Backup\nok\n"
        "=== 2026-10-01T04:00:30+00:00 exit 0\n"
        "=== 2026-10-02T04:00:00+00:00 start RC-Store-Backup\nboom\n"
        "=== 2026-10-02T04:00:05+00:00 exit 1\n", encoding="utf-8")
    r = _row(A.FakeScheduler(tasks={"RC-Store-Backup": A.TaskStatus(last_result=1)}))
    assert r["last_success"] == "2026-10-01T04:00:30+00:00"
    assert r["last_success_age"] == "1 d ago" or r["last_success_age"].endswith("h ago")
    assert r["log_tail"][-1].endswith("exit 1")


def test_static_rows_never_touch_the_scheduler():
    f = A.FakeScheduler()
    A.set_scheduler(f)
    rows = A.static_rows()
    assert [r["name"] for r in rows] == [e.name for e in A.REGISTRY]
    assert f.calls == []
    assert "state" not in rows[0]


def test_get_scheduler_is_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(A.sys, "platform", "linux")
    assert A.get_scheduler().available is False


# --- actions --------------------------------------------------------------------

def test_apply_actions_hit_the_registry_task_only():
    f = A.FakeScheduler()
    A.apply("RC-Store-Backup", "install", f)
    A.apply("RC-Store-Backup", "disable", f)
    A.apply("RC-Store-Backup", "enable", f)
    A.apply("RC-Store-Backup", "run", f)
    A.apply("RC-Store-Backup", "remove", f)
    assert [c[:2] for c in f.mutations] == [
        ("install", "RC-Store-Backup"), ("disable", "RC-Store-Backup"),
        ("enable", "RC-Store-Backup"), ("run", "RC-Store-Backup"),
        ("remove", "RC-Store-Backup")]


def test_apply_rejects_unknown_name_and_action_without_touching_the_scheduler():
    f = A.FakeScheduler()
    with pytest.raises(A.UnknownTask):
        A.apply("Some Other Task", "remove", f)
    with pytest.raises(A.UnknownAction):
        A.apply("RC-Store-Backup", "format", f)
    assert f.calls == []


def test_apply_on_an_unavailable_host_is_a_state_error():
    with pytest.raises(A.SchedulerUnavailable):
        A.apply("RC-Store-Backup", "install", A.FakeScheduler(available=False))


# --- Windows scheduler through a fake PowerShell ----------------------------------

class FakePS:
    def __init__(self, out="", rc=0, err=""):
        self.scripts: list[str] = []
        self.out, self.rc, self.err = out, rc, err

    def __call__(self, script):
        self.scripts.append(script)
        return self.rc, self.out, self.err


def _win(ps, python=r"C:\Program Files\Python 3\python.exe", workdir=r"C:\my projects"):
    return A.WindowsScheduler(runner=ps, python=python, workdir=workdir)


@pytest.mark.parametrize("op", ["status", "install", "enable", "disable", "remove", "run"])
def test_each_call_names_exactly_one_registry_task(op):
    ps = FakePS('{"installed":false}')
    w = _win(ps)
    e = A.entry("RC-Relatedness-OffPeak")
    {"status": lambda: w.status(e.name), "install": lambda: w.install(e),
     "enable": lambda: w.set_enabled(e.name, True), "disable": lambda: w.set_enabled(e.name, False),
     "remove": lambda: w.remove(e.name), "run": lambda: w.run(e.name)}[op]()
    assert len(ps.scripts) == 1
    named = set(re.findall(r"'(RC-[^']*)'", ps.scripts[0]))
    assert named == {"RC-Relatedness-OffPeak"}
    assert "-TaskName *" not in ps.scripts[0] and "Get-ScheduledTask\n" not in ps.scripts[0]


def test_windows_refuses_a_name_outside_the_registry():
    ps = FakePS()
    w = _win(ps)
    for call in (lambda: w.status("Other"), lambda: w.remove("Other"),
                 lambda: w.run("x'; Remove-Item *"), lambda: w.set_enabled("Other", False)):
        with pytest.raises(A.UnknownTask):
            call()
    assert ps.scripts == []


def test_install_quotes_paths_with_spaces_and_apostrophes():
    ps = FakePS()
    _win(ps, python=r"C:\Program Files\Python 3\python.exe",
         workdir=r"C:\O'Brien projects").install(A.entry("RC-Store-Backup"))
    s = ps.scripts[0]
    assert r"-Execute 'C:\Program Files\Python 3\python.exe'" in s
    assert r"-WorkingDirectory 'C:\O''Brien projects'" in s
    assert "-Argument '-m RevenueChaser.core.automations run RC-Store-Backup'" in s


def test_install_uses_the_registry_settings():
    ps = FakePS()
    w = _win(ps)
    w.install(A.entry("RC-Store-Backup"))
    w.install(A.entry("RC-ValueChain-Sync"))
    backup, vc = ps.scripts
    for flag in ("-StartWhenAvailable", "-AllowStartIfOnBatteries",
                 "-DontStopIfGoingOnBatteries", "-WakeToRun"):
        assert flag in backup
    assert "-WakeToRun" not in vc and "-AllowStartIfOnBatteries" in vc
    assert "-Daily -At '04:00'" in backup and "-Daily -At '13:00'" in vc
    assert "-LogonType Interactive" in backup


def test_a_weekly_row_installs_a_weekly_trigger():
    ps = FakePS()
    _win(ps).install(A.entry("RC-Index-Lists"))
    (script,) = ps.scripts
    assert "$tr = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '10:00'" in script
    assert "-Daily" not in script


def test_status_parses_the_scheduler_answer():
    out = ('{"installed":true,"state":"Ready","last_run":"1999-11-30T00:00:00.0000000",'
           '"next_run":"2026-10-03T04:00:00.0000000+02:00","last_result":267011,"missed":2}')
    st = _win(FakePS(out)).status("RC-Store-Backup")
    assert st.state == "ready" and st.last_run is None
    assert st.next_run.startswith("2026-10-03") and st.last_result == 0x41303
    assert st.missed_runs == 2
    assert _win(FakePS('{"installed":false}')).status("RC-Store-Backup") is None


def test_a_failing_powershell_call_raises():
    with pytest.raises(A.AutomationError):
        _win(FakePS("", rc=1, err="Access is denied")).remove("RC-Store-Backup")


# --- the command a task runs ------------------------------------------------------

def test_run_task_logs_output_and_exit_code(monkeypatch, tmp_path):
    e = A.entry("RC-ValueChain-Sync")
    monkeypatch.setattr(A, "_BY_NAME", {e.name: A.AutomationEntry(
        name=e.name, label="x", args=("-c", "import sys; print('hello'); sys.exit(3)"),
        time="13:00", description="d")})
    assert A.run_task(e.name) == 3
    text = (tmp_path / "logs" / f"{e.name}.log").read_text(encoding="utf-8")
    assert "hello" in text and re.search(r"=== \S+ exit 3", text) and "start" in text


def test_main_runs_a_registry_name_and_rejects_others(monkeypatch):
    seen = []
    monkeypatch.setattr(A, "run_task", lambda n: seen.append(n) or 0)
    assert A.main(["run", "RC-Store-Backup"]) == 0 and seen == ["RC-Store-Backup"]
    monkeypatch.undo()
    assert A.main(["run", "nope"]) == 2 and A.main([]) == 2


def test_main_install_and_remove_go_through_the_scheduler():
    f = A.FakeScheduler()
    A.set_scheduler(f)
    assert A.main(["install", "RC-Store-Backup"]) == 0
    assert A.main(["remove", "RC-Store-Backup"]) == 0
    assert A.main(["install", "Other"]) == 2
    assert [c[:2] for c in f.mutations] == [("install", "RC-Store-Backup"),
                                            ("remove", "RC-Store-Backup")]



def test_registry_names_are_task_scheduler_safe():
    for e in A.REGISTRY:
        assert re.fullmatch(r"RC-[A-Za-z0-9-]+", e.name), e.name


def test_tables_written_cover_what_each_job_writes():
    rel = set(A.entry("RC-Relatedness-OffPeak").tables_written)
    assert {"relatedness_profiles", "relatedness_attributes", "relatedness_scores",
            "run_registry", "llm_cache", "chain_filings", "edgar_documents"} <= rel
    assert "edgar_documents" in A.entry("RC-ValueChain-Sync").tables_written


def test_every_registry_tables_written_is_a_manifest_table():
    from RevenueChaser.core import rawstore
    for e in A.REGISTRY:
        for table in e.tables_written:
            assert table in rawstore.MANIFEST, f"{e.name} names {table}, not in MANIFEST"


# --- B1: PowerShell output, encoding and failures (coordinator review) --------------

_CLIXML = ('#< CLIXML\r\n<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
           '<Obj S="progress" RefId="0"><TN RefId="0"><T>System.Management.Automation.PSCustomObject'
           '</T></TN><MS><I64 N="SourceId">1</I64><PR N="Record"><AV>Preparing modules for first use.'
           '</AV></PR></MS></Obj></Objs>')


def test_every_script_silences_progress_and_writes_utf8():
    ps = FakePS('{"installed":false}')
    _win(ps).status("RC-Store-Backup")
    head = ps.scripts[0].splitlines()[0]
    assert "$ProgressPreference = 'SilentlyContinue'" in head
    assert "UTF8" in head


def test_status_ignores_a_progress_record_on_stderr():
    out = '{"installed":true,"state":"Ready","last_result":0,"missed":0}'
    st = _win(FakePS(out, err=_CLIXML)).status("RC-Store-Backup")
    assert st is not None and st.state == "ready"


def test_status_reads_the_last_json_line_of_stdout():
    out = ('WARNING: something\r\n{"installed":true,"state":"Disabled","last_result":0,"missed":0}'
           '\r\nmodule loaded\r\n')
    st = _win(FakePS(out)).status("RC-Store-Backup")
    assert st.state == "disabled"


def test_disabled_task_row_offers_enable_not_install():
    out = '{"installed":true,"state":"Disabled","last_result":0,"missed":0}'
    r = A.live_row(A.entry("RC-Store-Backup"), _win(FakePS(out)), NOW)
    assert r["state"] == "disabled"


def test_a_garbage_answer_is_an_error_row_never_not_installed():
    r = A.live_row(A.entry("RC-Store-Backup"), _win(FakePS("ÿþgarbage")), NOW)
    assert r["state"] == "error" and r["result_kind"] == "red"
    assert r["result_text"]


def test_one_failing_row_does_not_fail_the_others():
    class Mixed(A.FakeScheduler):
        def status(self, name):
            if name == "RC-Relatedness-OffPeak":
                raise A.AutomationError("boom")
            return super().status(name)

    rows = A.live_state(Mixed(), NOW)["rows"]
    assert [r["state"] for r in rows] == ["not_installed", "error"] + ["not_installed"] * (len(A.REGISTRY) - 2)
    assert "boom" in rows[1]["result_text"]


@pytest.mark.parametrize("exc", [
    __import__("subprocess").TimeoutExpired("powershell.exe", 60),
    FileNotFoundError("powershell.exe"),
    OSError("no such file"),
])
def test_powershell_failures_become_automation_errors(monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(A.subprocess, "run", boom)
    with pytest.raises(A.AutomationError):
        A._powershell("Get-Date")


def test_powershell_decodes_utf8_with_replacement(monkeypatch):
    seen = {}

    class P:
        returncode, stdout, stderr = 0, "{}", ""

    def run(*a, **k):
        seen.update(k)
        return P()
    monkeypatch.setattr(A.subprocess, "run", run)
    assert A._powershell("x") == (0, "{}", "")
    assert seen["encoding"] == "utf-8-sig" and seen["errors"] == "replace"
    assert "text" not in seen or seen["text"] is not True or seen["encoding"]


def test_error_text_comes_from_stderr_on_failure():
    with pytest.raises(A.AutomationError, match="Access is denied"):
        _win(FakePS("", rc=1, err="Access is denied")).run("RC-Store-Backup")


# --- N6: no console window per run ----------------------------------------------------

def test_installer_prefers_pythonw_next_to_python(tmp_path):
    py = tmp_path / "python.exe"
    pyw = tmp_path / "pythonw.exe"
    py.write_text("")
    pyw.write_text("")
    assert A.windowless_python(str(py)) == str(pyw)
    pyw.unlink()
    assert A.windowless_python(str(py)) == str(py)
    assert A.windowless_python("/usr/bin/python3") == "/usr/bin/python3"


def test_install_uses_the_windowless_interpreter(tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    ps = FakePS()
    A.WindowsScheduler(runner=ps, python=str(tmp_path / "python.exe"),
                       workdir=r"C:\x").install(A.entry("RC-Store-Backup"))
    assert "pythonw.exe" in ps.scripts[0]


def test_ps1_callers_propagate_the_exit_code_and_drop_dryrun():
    for ps1 in ("register_tasks.ps1", "scripts/register_offpeak_tasks.ps1"):
        text = (ROOT / ps1).read_text(encoding="utf-8")
        assert "exit $" in text and "LASTEXITCODE" in text
        assert "-DryRun" not in text.split("param(")[1].split(")")[0]


# --- F1: BOM, and no console window (coordinator re-review) -----------------------------

def test_status_parses_a_bom_prefixed_json_line():
    out = '﻿{"installed":true,"state":"Ready","last_result":0,"missed":0}\r\n'
    assert _win(FakePS(out)).status("RC-Store-Backup").state == "ready"


def test_powershell_decodes_with_utf8_sig(monkeypatch):
    seen = {}

    class P:
        returncode, stdout, stderr = 0, "{}", ""

    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: seen.update(k) or P())
    A._powershell("x")
    assert seen["encoding"] == "utf-8-sig"


def test_no_console_window_flag_is_passed_to_powershell_and_the_task_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(A.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    calls = []

    class P:
        returncode, stdout, stderr = 0, "{}", ""

    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: calls.append(k) or P())
    A._powershell("x")
    monkeypatch.setattr(A.config, "OUTPUT_DIR", tmp_path)
    A.run_task("RC-Store-Backup")
    assert [c["creationflags"] for c in calls] == [0x08000000, 0x08000000]


# --- #275: the off-machine copy line on the backup row ---------------------------------------------------

def _off(monkeypatch, mirror="/Drive/RevenueChaser", log=""):
    monkeypatch.setattr(A.config, "RAW_STORE_BACKUP_MIRROR", mirror)
    e = A.entry("RC-Store-Backup")
    e.log_path.parent.mkdir(parents=True, exist_ok=True)
    e.log_path.write_text(log, encoding="utf-8")
    return A.offmachine_state(e, NOW)


def test_only_the_backup_row_has_an_off_machine_line(monkeypatch):
    monkeypatch.setattr(A.config, "RAW_STORE_BACKUP_MIRROR", "/Drive")
    assert [e.name for e in A.REGISTRY if e.offmachine] == ["RC-Store-Backup"]
    assert A.offmachine_state(A.entry("RC-Relatedness-OffPeak"), NOW) is None
    assert A.live_row(A.entry("RC-Relatedness-OffPeak"), A.FakeScheduler(), NOW)["offmachine"] is None


def test_an_unset_folder_shows_red_not_set(monkeypatch):
    st = _off(monkeypatch, mirror="")
    assert st["configured"] is False and st["kind"] == "red"
    assert "not set" in st["text"] and "RAW_STORE_BACKUP_MIRROR" in st["text"]
    assert _off(monkeypatch, mirror="   ")["configured"] is False


def test_a_configured_folder_with_no_copy_yet_shows_red(monkeypatch):
    st = _off(monkeypatch, log="=== 2026-10-02T04:00:00+00:00 start RC-Store-Backup\nbackup OK: x\n"
                               "=== 2026-10-02T04:00:30+00:00 exit 0\n")
    assert st["configured"] is True and st["kind"] == "red" and "no copy yet" in st["text"]


def test_a_recent_ok_copy_shows_green_with_its_age(monkeypatch):
    st = _off(monkeypatch, log="off-machine copy OK 2026-10-02T03:59:00+00:00: 4 copied, 5 already there -> D:\\x\n")
    assert st["kind"] == "ok" and st["text"] == "OK, 8 h ago"


def test_an_ok_copy_older_than_two_days_shows_red_as_stale(monkeypatch):
    st = _off(monkeypatch, log="off-machine copy OK 2026-09-29T04:01:00+00:00: 4 copied, 5 already there -> D:\\x\n")
    assert st["kind"] == "red" and "3 d ago" in st["text"] and "48 h" in st["text"]


def test_the_two_day_limit_is_exact(monkeypatch):
    just_in = "off-machine copy OK 2026-09-30T12:00:01+00:00: 1 copied, 0 already there -> D:\\x\n"     # 47 h 59 m 59 s
    just_out = "off-machine copy OK 2026-09-30T12:00:00+00:00: 1 copied, 0 already there -> D:\\x\n"    # exactly 48 h
    assert _off(monkeypatch, log=just_in)["kind"] == "ok"
    assert _off(monkeypatch, log=just_out)["kind"] == "red"


def test_a_failed_copy_shows_red_with_the_last_good_one(monkeypatch):
    log = ("off-machine copy OK 2026-09-29T12:00:00+00:00: 4 copied, 5 already there -> D:\\x\n"
           "off-machine copy FAILED 2026-10-02T03:59:00+00:00: off-machine folder D:\\x is not reachable\n")
    st = _off(monkeypatch, log=log)
    assert st["kind"] == "red" and st["text"] == "FAILED 8 h ago; last OK 3 d ago"


def test_a_failure_followed_by_an_ok_copy_is_green_again(monkeypatch):
    log = ("off-machine copy FAILED 2026-10-01T04:01:00+00:00: disk full\n"
           "off-machine copy OK 2026-10-02T04:01:00+00:00: 4 copied, 5 already there -> D:\\x\n")
    assert _off(monkeypatch, log=log)["kind"] == "ok"


def test_a_failure_with_no_good_copy_ever(monkeypatch):
    st = _off(monkeypatch, log="off-machine copy FAILED 2026-10-02T03:59:00+00:00: disk full\n")
    assert st["kind"] == "red" and st["text"] == "FAILED 8 h ago; no copy has succeeded yet"


def test_the_live_row_carries_the_state_even_when_the_scheduler_errors(monkeypatch):
    monkeypatch.setattr(A.config, "RAW_STORE_BACKUP_MIRROR", "")

    class Broken(A.FakeScheduler):
        def status(self, name):
            raise A.AutomationError("scheduler unreadable")

    assert A.live_row(A.entry("RC-Store-Backup"), Broken(), NOW)["offmachine"]["configured"] is False
    assert A.live_row(A.entry("RC-Store-Backup"), A.FakeScheduler(), NOW)["offmachine"]["configured"] is False


def test_static_rows_have_no_off_machine_state_so_the_panel_shows_nothing_before_it_polls():
    assert all("offmachine" not in r for r in A.static_rows())


# --- #309 part 3: the sizes of both backup folders on the backup row ---------------------------------------

_SIZES = "backup sizes 2026-10-02T03:59:00+00:00: local {local} bytes{off}\n"


def _size(monkeypatch, local=12_782_917_784, off=7_676_019_838, log=None, local_gb=20.0, off_gb=320.0):
    monkeypatch.setattr(A.config, "RAW_STORE_BACKUP_WARN_GB", local_gb)
    monkeypatch.setattr(A.config, "RAW_STORE_BACKUP_MIRROR_WARN_GB", off_gb)
    e = A.entry("RC-Store-Backup")
    e.log_path.parent.mkdir(parents=True, exist_ok=True)
    text = _SIZES.format(local=local, off="" if off is None else f", off-machine {off} bytes")
    e.log_path.write_text(text if log is None else log, encoding="utf-8")
    return A.backup_size_state(e, NOW)


def test_the_backup_row_shows_both_folder_sizes(monkeypatch):
    st = _size(monkeypatch)
    assert st["kind"] == "ok"
    assert st["text"] == "local 12.78 GB, off-machine 7.68 GB (8 h ago)"


def test_a_local_folder_above_its_threshold_warns(monkeypatch):
    st = _size(monkeypatch, local=21_500_000_000)
    assert st["kind"] == "warn"
    assert "local 21.50 GB, above 20 GB (RAW_STORE_BACKUP_WARN_GB)" in st["text"]
    assert "off-machine 7.68 GB" in st["text"] and "RAW_STORE_BACKUP_MIRROR_WARN_GB" not in st["text"]


def test_an_offmachine_folder_above_its_threshold_warns(monkeypatch):
    st = _size(monkeypatch, off=330_000_000_000)
    assert st["kind"] == "warn"
    assert "off-machine 330.00 GB, above 320 GB (RAW_STORE_BACKUP_MIRROR_WARN_GB)" in st["text"]
    assert "RAW_STORE_BACKUP_WARN_GB)" not in st["text"].replace("MIRROR_WARN_GB)", "")


def test_the_thresholds_are_exact_and_come_from_config(monkeypatch):
    assert _size(monkeypatch, local=20_000_000_000)["kind"] == "ok"            # at the threshold: not above it
    assert _size(monkeypatch, local=20_000_000_001)["kind"] == "warn"
    assert _size(monkeypatch, off=320_000_000_000)["kind"] == "ok"
    assert _size(monkeypatch, off=320_000_000_001)["kind"] == "warn"
    assert _size(monkeypatch, local=12_782_917_784, local_gb=10)["kind"] == "warn"
    assert _size(monkeypatch, off=7_676_019_838, off_gb=5)["kind"] == "warn"


def test_no_size_line_yet_says_so(monkeypatch):
    st = _size(monkeypatch, log="backup OK: x\n")
    assert st["kind"] == "none" and "not measured yet" in st["text"]


def test_the_newest_size_line_wins(monkeypatch):
    log = (_SIZES.format(local=25_000_000_000, off=", off-machine 1 bytes")
           + "backup sizes 2026-10-02T04:30:00+00:00: local 1000000000 bytes, off-machine 2000000000 bytes\n")
    st = _size(monkeypatch, log=log)
    assert st["kind"] == "ok" and st["text"].startswith("local 1.00 GB, off-machine 2.00 GB")


def test_a_line_without_an_offmachine_size_shows_the_local_one_only(monkeypatch):
    assert _size(monkeypatch, off=None)["text"] == "local 12.78 GB (8 h ago)"
    assert _size(monkeypatch, off=None, local=25_000_000_000)["kind"] == "warn"


def test_the_warn_defaults_are_avos_numbers():
    """avo, 2026-10-16: 20 GB local; 320 GB off-machine (80% of the 400 GB Google quota)."""
    assert A.config.BACKUP_WARN_GB_DEFAULT == 20
    assert A.config.BACKUP_MIRROR_WARN_GB_DEFAULT == 320


def test_only_the_backup_row_has_a_size_line_and_static_rows_have_none(monkeypatch):
    _size(monkeypatch)
    assert A.backup_size_state(A.entry("RC-Relatedness-OffPeak"), NOW) is None
    assert A.live_row(A.entry("RC-Store-Backup"), A.FakeScheduler(), NOW)["backup_size"]["kind"] == "ok"
    assert A.live_row(A.entry("RC-Relatedness-OffPeak"), A.FakeScheduler(), NOW)["backup_size"] is None
    assert all("backup_size" not in r for r in A.static_rows())


@pytest.mark.parametrize("raw, want", [(None, 20.0), ("", 20.0), ("abc", 20.0), ("-3", 20.0), ("0", 20.0),
                                       ("12.5", 12.5), (" 30 ", 30.0)])
def test_the_warn_thresholds_read_a_positive_number_or_keep_the_default(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("X_WARN_GB", raising=False)
    else:
        monkeypatch.setenv("X_WARN_GB", raw)
    assert A.config._warn_gb("X_WARN_GB", 20.0) == want
