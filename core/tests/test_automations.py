"""Automations registry, scheduler interface and the Windows installer (#130).

Windows-only code is tested through a fake PowerShell runner: nothing here touches a
real Task Scheduler.
"""
from __future__ import annotations

import json
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

# The Earnings inputs entries, one per schedule (#620).
_EARNINGS_INPUTS = ("RC-Earnings-Calendar", "RC-Short-Interest", "RC-Earnings-Daytime", "RC-Macro-Weekly",
                    "RC-CFTC-Weekly")

def test_registry_names_are_stable_and_prefixed():
    assert [e.name for e in A.REGISTRY] == [
        "RC-Relatedness-OffPeak", "RC-ValueChain-Sync", "RC-Daily-Bars", "RC-Screeners-Daily",
        "RC-Forecast-Outcomes", "RC-Forecast-Baselines", "RC-Index-Lists", "RC-Earnings-Calendar",
        "RC-Short-Interest", "RC-Earnings-Daytime", "RC-Macro-Weekly", "RC-CFTC-Weekly", "RC-IBKR-Login-Check",
        "RC-Build", "RC-Backup-Weekly", "RC-SPX-Gamma-Fetch"]
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
               if x.name not in (e.name, "RC-Screeners-Daily", "RC-Forecast-Outcomes", "RC-Forecast-Baselines",
                                 "RC-Build", "RC-Backup-Weekly", "RC-SPX-Gamma-Fetch", *_EARNINGS_INPUTS))


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


def test_the_screeners_row_is_chained_on_the_daily_bars():
    """#129 A1: S2 (the universe CLI) waits for S1 (RC-Daily-Bars) on weekdays, not before 23:30 (#620)."""
    e = A.entry("RC-Screeners-Daily")
    assert (e.after, e.time, e.days) == ("RC-Daily-Bars", "23:30", _WEEKDAYS)
    assert e.args == ("-m", "RevenueChaser.app.universe_cli")
    assert e.schedule == "weekly Monday, Tuesday, Wednesday, Thursday, Friday 23:30 (local), after RC-Daily-Bars"
    assert not e.default_install
    s = e.settings
    assert s.wake_to_run and s.allow_on_battery and s.start_when_available and s.run_only_when_logged_on
    assert s.time_limit_hours == 4 and e.chain_wait_minutes < s.time_limit_hours * 60
    assert {"equity_universe", "forecast_ledger", "swing_ohlc", "job_runs"} <= set(e.tables_written)
    assert not A.entry("RC-Daily-Bars").days       # the chain needs a daily predecessor
    assert [x.name for x in A.REGISTRY if x.after] == ["RC-Screeners-Daily", "RC-Forecast-Outcomes",
                                                       "RC-Forecast-Baselines"]


def test_the_daily_bars_row_first_stores_the_sec_files():
    """#301 F0: RC-Daily-Bars runs the SEC exchange and fund files as its best-effort first step; it stays daily,
    so the chain on it still holds."""
    from RevenueChaser.core import sec_files

    e = A.entry("RC-Daily-Bars")
    assert e.args[:4] == ("-m", "RevenueChaser.scripts.daily_bars", "--nightly", "--sec-files")
    assert (e.time, e.days, e.after) == ("23:00", (), None)
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
    """S0 (#380): the outcomes writer reads D01, so it waits for tonight's RC-Daily-Bars, every day at 23:35 (#620),
    and writes forecast_outcomes alone."""
    e = A.entry("RC-Forecast-Outcomes")
    assert (e.after, e.time, e.days) == ("RC-Daily-Bars", "23:35", ())
    assert e.args == ("-m", "RevenueChaser.scripts.forecast_outcomes", "--nightly")
    assert e.schedule == "daily 23:35 (local), after RC-Daily-Bars"
    assert e.tables_written == ("forecast_outcomes",)
    assert e.chain_wait_minutes == 120 and not e.default_install and not e.offpeak_gated


def test_the_forecast_baselines_row_is_chained_on_the_outcomes_run():
    """#25 HR-1: the baselines read the same stored bars and write raw.duckdb, so they wait for tonight's
    RC-Forecast-Outcomes instead of running beside it, on their own 23:50 trigger, and write
    ``forecast_baselines`` alone."""
    e = A.entry("RC-Forecast-Baselines")
    assert (e.after, e.time, e.days) == ("RC-Forecast-Outcomes", "23:50", ())
    assert e.args == ("-m", "RevenueChaser.scripts.forecast_baselines", "--nightly")
    assert e.schedule == "daily 23:50 (local), after RC-Forecast-Outcomes"
    assert e.tables_written == ("forecast_baselines",)
    assert e.chain_wait_minutes == 120 and not e.default_install and not e.offpeak_gated
    names = [x.name for x in A.REGISTRY]
    assert names.index("RC-Forecast-Outcomes") + 1 == names.index("RC-Forecast-Baselines")    # after it, never before


def test_each_busy_capable_nightly_row_says_its_busy_exit_in_the_description():
    """S-1 (#26): Task Scheduler shows exit 3 as a red "failed (0x3)", so the row itself has to say what it means.

    The wording follows RC-Screeners-Daily's "A busy universe lock exits 3."
    """
    for name in ("RC-Forecast-Outcomes", *_EARNINGS_INPUTS):
        description = A.entry(name).description
        assert "exits 3" in description and "nothing was written" in description, name


def test_the_outcomes_row_cannot_chain_on_the_weekday_screeners_row():
    """``check_chain`` refuses a weekly predecessor; RC-Screeners-Daily runs on weekdays only."""
    import dataclasses
    rows = tuple(dataclasses.replace(e, after="RC-Screeners-Daily") if e.name == "RC-Forecast-Outcomes" else e
                 for e in A.REGISTRY)
    with pytest.raises(ValueError, match="runs weekly"):
        A.check_chain(rows)


def _inputs_entry(**kw):
    import dataclasses
    base = A.AutomationEntry(
        name="RC-Test-Inputs", label="x", time="01:30", description="d",
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


def test_an_inputs_step_need_not_chain_on_the_daily_bars(fake_step):
    """#620: the sweeps read no bars, so an inputs step may run unchained (a 13:00 entry could not chain at all)."""
    A.check_inputs_steps(A.REGISTRY + (_inputs_entry(after=None, time="13:00"),))
    assert all(A.entry(n).after is None for n in _EARNINGS_INPUTS)


def test_an_inputs_cap_may_be_unlimited(fake_step):
    """#620: ``unlimited`` (no cap) is accepted beside a whole number."""
    args = list(_inputs_entry().args)
    args[args.index("--max-uw-calls") + 1] = "unlimited"
    A.check_inputs_steps(A.REGISTRY + (_inputs_entry(args=tuple(args)),))


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


@pytest.mark.parametrize("value", ["-1", "lots", "", "Unlimited"])
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
    assert "-Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '23:30'" in script
    assert "-WakeToRun" in script and "-StartWhenAvailable" in script and "-AllowStartIfOnBatteries" in script
    assert "run RC-Screeners-Daily" in script


def test_no_row_is_marked_default_install():
    """RC-Store-Backup was the one default install; it retired with the weekly backup (avo, 8 Nov 2026)."""
    assert [e.name for e in A.REGISTRY if e.default_install] == []


def test_log_path_derives_from_output_dir(tmp_path):
    assert A.entry("RC-Daily-Bars").log_path == tmp_path / "logs" / "RC-Daily-Bars.log"


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


def _row(sched, name="RC-Daily-Bars"):
    return A.live_row(A.entry(name), sched, NOW)


def test_row_not_installed():
    r = _row(A.FakeScheduler())
    assert r["state"] == "not_installed" and r["result_kind"] == "none"


def test_row_unavailable_host():
    r = _row(A.FakeScheduler(available=False))
    assert r["state"] == "unavailable"


def test_row_disabled_and_enabled_and_running():
    mk = lambda **kw: A.FakeScheduler(tasks={"RC-Daily-Bars": A.TaskStatus(**kw)})  # noqa: E731
    assert _row(mk(state="disabled"))["state"] == "disabled"
    assert _row(mk(state="ready", last_result=0))["state"] == "enabled"
    assert _row(mk(state="running"))["state"] == "running"
    assert _row(mk(state="ready", last_result=0x41301))["state"] == "running"


def test_never_run_task_is_neutral_not_red():
    r = _row(A.FakeScheduler(tasks={"RC-Daily-Bars": A.TaskStatus(last_result=0x41303)}))
    assert r["result_kind"] == "neutral"


def test_failed_last_run_is_red_with_missed_runs():
    r = _row(A.FakeScheduler(tasks={"RC-Daily-Bars": A.TaskStatus(
        last_result=1, missed_runs=3, last_run="2026-10-02T04:00:00+00:00")}))
    assert r["result_kind"] == "red" and r["missed_runs"] == 3
    assert r["last_success"] is None and r["last_success_age"] == "never"


def test_last_success_age_comes_from_the_log():
    e = A.entry("RC-Daily-Bars")
    e.log_path.parent.mkdir(parents=True)
    e.log_path.write_text(
        "=== 2026-10-01T04:00:00+00:00 start RC-Daily-Bars\nok\n"
        "=== 2026-10-01T04:00:30+00:00 exit 0\n"
        "=== 2026-10-02T04:00:00+00:00 start RC-Daily-Bars\nboom\n"
        "=== 2026-10-02T04:00:05+00:00 exit 1\n", encoding="utf-8")
    r = _row(A.FakeScheduler(tasks={"RC-Daily-Bars": A.TaskStatus(last_result=1)}))
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


def test_get_scheduler_is_unavailable_without_task_scheduler_or_systemd(monkeypatch):
    monkeypatch.setattr(A.sys, "platform", "linux")
    monkeypatch.setattr(A.shutil, "which", lambda name: None)
    assert A.get_scheduler().available is False


# --- actions --------------------------------------------------------------------

def test_apply_actions_hit_the_registry_task_only():
    f = A.FakeScheduler()
    A.apply("RC-Daily-Bars", "install", f)
    A.apply("RC-Daily-Bars", "disable", f)
    A.apply("RC-Daily-Bars", "enable", f)
    A.apply("RC-Daily-Bars", "run", f)
    A.apply("RC-Daily-Bars", "remove", f)
    assert [c[:2] for c in f.mutations] == [
        ("install", "RC-Daily-Bars"), ("disable", "RC-Daily-Bars"),
        ("enable", "RC-Daily-Bars"), ("run", "RC-Daily-Bars"),
        ("remove", "RC-Daily-Bars")]


def test_apply_rejects_unknown_name_and_action_without_touching_the_scheduler():
    f = A.FakeScheduler()
    with pytest.raises(A.UnknownTask):
        A.apply("Some Other Task", "remove", f)
    with pytest.raises(A.UnknownAction):
        A.apply("RC-Daily-Bars", "format", f)
    assert f.calls == []


def test_apply_on_an_unavailable_host_is_a_state_error():
    with pytest.raises(A.SchedulerUnavailable):
        A.apply("RC-Daily-Bars", "install", A.FakeScheduler(available=False))


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
         workdir=r"C:\O'Brien projects").install(A.entry("RC-Daily-Bars"))
    s = ps.scripts[0]
    assert r"-Execute 'C:\Program Files\Python 3\python.exe'" in s
    assert r"-WorkingDirectory 'C:\O''Brien projects'" in s
    assert "-Argument '-m RevenueChaser.core.automations run RC-Daily-Bars'" in s


def test_install_uses_the_registry_settings():
    ps = FakePS()
    w = _win(ps)
    w.install(A.entry("RC-Screeners-Daily"))              # a row that wakes the machine
    w.install(A.entry("RC-ValueChain-Sync"))
    waking, vc = ps.scripts
    for flag in ("-StartWhenAvailable", "-AllowStartIfOnBatteries",
                 "-DontStopIfGoingOnBatteries", "-WakeToRun"):
        assert flag in waking
    assert "-WakeToRun" not in vc and "-AllowStartIfOnBatteries" in vc
    assert "-At '23:30'" in waking and "-Daily -At '00:15'" in vc
    assert "-LogonType Interactive" in waking


def test_a_row_without_restarts_installs_no_restart_settings():
    """RC-IBKR-Login-Check has ``restart_count=0``: ``-RestartCount 0`` makes Task Scheduler refuse the task
    (HRESULT 0x80041319, "Count" out of range), so a zero count installs without the restart pair."""
    ps = FakePS()
    w = _win(ps)
    w.install(A.entry("RC-IBKR-Login-Check"))
    w.install(A.entry("RC-Daily-Bars"))
    issues, backup = ps.scripts
    assert "-RestartCount" not in issues and "-RestartInterval" not in issues
    assert "-RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)" in backup


def test_a_weekly_row_installs_a_weekly_trigger():
    ps = FakePS()
    _win(ps).install(A.entry("RC-Index-Lists"))
    (script,) = ps.scripts
    assert "$tr = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At '10:00'" in script
    assert "-Daily" not in script


def test_status_parses_the_scheduler_answer():
    out = ('{"installed":true,"state":"Ready","last_run":"1999-11-30T00:00:00.0000000",'
           '"next_run":"2026-10-03T04:00:00.0000000+02:00","last_result":267011,"missed":2}')
    st = _win(FakePS(out)).status("RC-Daily-Bars")
    assert st.state == "ready" and st.last_run is None
    assert st.next_run.startswith("2026-10-03") and st.last_result == 0x41303
    assert st.missed_runs == 2
    assert _win(FakePS('{"installed":false}')).status("RC-Daily-Bars") is None


def test_a_failing_powershell_call_raises():
    with pytest.raises(A.AutomationError):
        _win(FakePS("", rc=1, err="Access is denied")).remove("RC-Daily-Bars")


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
    assert A.main(["run", "RC-Daily-Bars"]) == 0 and seen == ["RC-Daily-Bars"]
    monkeypatch.undo()
    assert A.main(["run", "nope"]) == 2 and A.main([]) == 2


def test_main_install_and_remove_go_through_the_scheduler():
    f = A.FakeScheduler()
    A.set_scheduler(f)
    assert A.main(["install", "RC-Daily-Bars"]) == 0
    assert A.main(["remove", "RC-Daily-Bars"]) == 0
    assert A.main(["install", "Other"]) == 2
    assert [c[:2] for c in f.mutations] == [("install", "RC-Daily-Bars"),
                                            ("remove", "RC-Daily-Bars")]



def test_registry_names_are_task_scheduler_safe():
    for e in A.REGISTRY:
        assert re.fullmatch(r"RC-[A-Za-z0-9-]+", e.name), e.name


def test_tables_written_cover_what_each_job_writes():
    rel = set(A.entry("RC-Relatedness-OffPeak").tables_written)
    assert {"relatedness_profiles", "relatedness_attributes", "relatedness_scores",
            "run_registry", "llm_cache", "chain_filings", "edgar_documents"} <= rel
    # FAM-4: the Relatedness run fetches the filings it scores, so it records its misses in chain_meta through
    # module_value_chain.engine.record_misses (engine.py: ``vc_engine.fetch_corpus``). The list says so.
    assert "chain_meta" in rel
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
    _win(ps).status("RC-Daily-Bars")
    head = ps.scripts[0].splitlines()[0]
    assert "$ProgressPreference = 'SilentlyContinue'" in head
    assert "UTF8" in head


def test_status_ignores_a_progress_record_on_stderr():
    out = '{"installed":true,"state":"Ready","last_result":0,"missed":0}'
    st = _win(FakePS(out, err=_CLIXML)).status("RC-Daily-Bars")
    assert st is not None and st.state == "ready"


def test_status_reads_the_last_json_line_of_stdout():
    out = ('WARNING: something\r\n{"installed":true,"state":"Disabled","last_result":0,"missed":0}'
           '\r\nmodule loaded\r\n')
    st = _win(FakePS(out)).status("RC-Daily-Bars")
    assert st.state == "disabled"


def test_disabled_task_row_offers_enable_not_install():
    out = '{"installed":true,"state":"Disabled","last_result":0,"missed":0}'
    r = A.live_row(A.entry("RC-Daily-Bars"), _win(FakePS(out)), NOW)
    assert r["state"] == "disabled"


def test_a_garbage_answer_is_an_error_row_never_not_installed():
    r = A.live_row(A.entry("RC-Daily-Bars"), _win(FakePS("ÿþgarbage")), NOW)
    assert r["state"] == "error" and r["result_kind"] == "red"
    assert r["result_text"]


def test_one_failing_row_does_not_fail_the_others():
    class Mixed(A.FakeScheduler):
        def status(self, name):
            if name == "RC-Relatedness-OffPeak":
                raise A.AutomationError("boom")
            return super().status(name)

    rows = A.live_state(Mixed(), NOW)["rows"]
    assert [r["state"] for r in rows] == ["error"] + ["not_installed"] * (len(A.REGISTRY) - 1)
    assert "boom" in rows[0]["result_text"]


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
        _win(FakePS("", rc=1, err="Access is denied")).run("RC-Daily-Bars")


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
                       workdir=r"C:\x").install(A.entry("RC-Daily-Bars"))
    assert "pythonw.exe" in ps.scripts[0]


def test_ps1_callers_propagate_the_exit_code_and_drop_dryrun():
    for ps1 in ("register_tasks.ps1", "scripts/register_offpeak_tasks.ps1"):
        text = (ROOT / ps1).read_text(encoding="utf-8")
        assert "exit $" in text and "LASTEXITCODE" in text
        assert "-DryRun" not in text.split("param(")[1].split(")")[0]


# --- F1: BOM, and no console window (coordinator re-review) -----------------------------

def test_status_parses_a_bom_prefixed_json_line():
    out = '﻿{"installed":true,"state":"Ready","last_result":0,"missed":0}\r\n'
    assert _win(FakePS(out)).status("RC-Daily-Bars").state == "ready"


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
    A.run_task("RC-Daily-Bars")
    assert [c["creationflags"] for c in calls] == [0x08000000, 0x08000000]


# ── #162: the IBKR Gateway login check ──────────────────────────────────────────────────────────

def test_the_ibkr_login_check_writes_no_table():
    """It records only its own file under .cache/, so it can never queue behind or hold the
    dashboard's single DuckDB writer."""
    entry = A.entry("RC-IBKR-Login-Check")
    assert entry.tables_written == ()
    assert entry.offpeak_gated is False
    assert entry.after is None, "it depends on no other run: it reports a state, it does not build data"


def test_the_ibkr_login_check_runs_the_module_it_claims_to():
    entry = A.entry("RC-IBKR-Login-Check")
    assert entry.command == "python -m RevenueChaser.scripts.ibkr_login_check"


def test_the_ibkr_login_check_description_says_what_it_never_asks_for():
    """The one guard a reader of the Automations panel has against scope creep into account data."""
    text = A.entry("RC-IBKR-Login-Check").description
    for phrase in ("account", "position", "order", "execution", "RC_IBKR_LOGIN_CHECK"):
        assert phrase in text, f"the description no longer names {phrase!r}"


def test_the_ibkr_login_check_is_not_installed_by_default():
    assert A.entry("RC-IBKR-Login-Check").default_install is False


# --- the market-store builder as a service (PR 2 step 2) ----------------------------------------------------------

class _FakeSystemctl:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        return 0, "", ""


def test_the_builder_row_runs_one_queue_pass_every_hour_at_05():
    e = A.entry("RC-Build")
    assert e.args == ("-m", "RevenueChaser.rcdata.build", "run", "--once")
    assert e.hourly and e.time == "00:05" and e.schedule == "hourly at :05 (local)"
    assert e.settings.time_limit_minutes == 120
    assert e.tables_written == (), "it writes the market store, never the old one: no job_runs row (job_runs2 instead)"
    assert e.after is None and not e.default_install
    assert e.unit_name == "life-rc-builder"


def test_the_weekly_backup_row_runs_sunday_0330_every_step_for_at_most_60_minutes():
    """avo, 8 Nov 2026: one weekly backup of everything onto the HDD, the newest 4 copies kept. It replaces the
    nightly RC-Build-Backup (04:30, serving files only) and the weekly RC-Build-Backup-Heavy."""
    e = A.entry("RC-Backup-Weekly")
    assert e.args == ("-m", "RevenueChaser.rcdata", "backup"), "no --only: every step runs"
    assert (e.time, e.days) == ("03:30", ("Sunday",))
    assert e.schedule == "weekly Sunday 03:30 (local)"
    assert A.on_calendar(e) == "Sun *-*-* 03:30:00"
    assert e.settings.time_limit_minutes == 60 and e.tables_written == ()
    assert e.unit_name == "life-rc-backup-weekly" == A.BACKUP_WEEKLY_UNIT
    assert not e.default_install and e.after is None and not e.hourly
    for gone in ("RC-Build-Backup", "RC-Build-Backup-Heavy"):
        with pytest.raises(A.UnknownTask):
            A.entry(gone)
    assert not hasattr(A, "BUILDER_BACKUP_UNIT") and not hasattr(A, "BUILDER_BACKUP_HEAVY_UNIT")


def _minutes_of(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def test_linux_renders_the_builder_rows_as_life_rc_builder_units(tmp_path):
    sc = _FakeSystemctl()
    s = A.LinuxScheduler(runner=sc, python="/srv/rc/.venv/bin/python", workdir="/srv/rc", unit_dir=tmp_path / "u")
    s.install(A.entry("RC-Build"))
    s.install(A.entry("RC-Backup-Weekly"))
    files = sorted(p.name for p in (tmp_path / "u").iterdir())
    assert files == ["life-rc-backup-weekly.service", "life-rc-backup-weekly.timer",
                     "life-rc-builder.service", "life-rc-builder.timer"]
    service = (tmp_path / "u" / "life-rc-builder.service").read_text(encoding="utf-8")
    timer = (tmp_path / "u" / "life-rc-builder.timer").read_text(encoding="utf-8")
    assert 'ExecStart="/srv/rc/.venv/bin/python" "-m" "RevenueChaser.core.automations" "run" "RC-Build"' in service
    assert "Type=oneshot" in service and "TimeoutStartSec=120min" in service
    assert "OnCalendar=*-*-* *:05:00" in timer and "Unit=life-rc-builder.service" in timer
    backup_service = (tmp_path / "u" / "life-rc-backup-weekly.service").read_text(encoding="utf-8")
    backup_timer = (tmp_path / "u" / "life-rc-backup-weekly.timer").read_text(encoding="utf-8")
    assert '"run" "RC-Backup-Weekly"' in backup_service and "TimeoutStartSec=60min" in backup_service
    assert "OnCalendar=Sun *-*-* 03:30:00" in backup_timer and "Unit=life-rc-backup-weekly.service" in backup_timer
    for text in (service, timer, backup_service, backup_timer):      # valid unit text: sections, key=value lines
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert lines[0] == "[Unit]"
        assert all(ln.startswith("[") and ln.endswith("]") or "=" in ln for ln in lines)
    assert ["enable", "--now", "life-rc-builder.timer"] in sc.calls
    assert ["enable", "--now", "life-rc-backup-weekly.timer"] in sc.calls
    sc.calls.clear()
    s.set_enabled("RC-Build", False)
    s.run("RC-Build")
    s.remove("RC-Build")
    assert sc.calls == [["disable", "--now", "life-rc-builder.timer"], ["start", "--no-block", "life-rc-builder.service"],
                        ["disable", "--now", "life-rc-builder.timer"], ["daemon-reload"]]
    assert sorted(p.name for p in (tmp_path / "u").iterdir()) == ["life-rc-backup-weekly.service",
                                                                   "life-rc-backup-weekly.timer"]


def test_windows_installs_the_builder_as_an_hourly_trigger_with_no_limit():
    """#527: a zero ExecutionTimeLimit is Task Scheduler's "no limit"; the backup keeps its minutes."""
    ps = FakePS()
    _win(ps).install(A.entry("RC-Build"))
    s = ps.scripts[0]
    assert "-Once -At '00:05' -RepetitionInterval (New-TimeSpan -Hours 1)" in s
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 120)" in s


def test_a_chain_step_cannot_wait_on_an_hourly_row():
    step = A.AutomationEntry(name="RC-X", label="x", args=("-c", "pass"), time="00:30", description="d",
                             after="RC-Build")
    with pytest.raises(ValueError, match="hourly"):
        A.check_chain(A.REGISTRY + (step,))


def test_the_spx_gamma_fetch_runs_on_weekdays_after_the_stored_close():
    """#291: the scheduled SPX Gamma Fetch keeps a raw day (all expiries) for every session. The builder stores the
    market series on the first pass of each trading day (00:05 New York, ``rcdata/build/due.py``), so 06:30
    Amsterdam Monday to Friday finds the previous session's Cboe SPX close: Monday fetches Friday. A Saturday run
    would never find Friday's close (no trading day, no builder pass). Not chained: the CLI waits for that close
    itself (exit 3, no UW call)."""
    e = A.entry("RC-SPX-Gamma-Fetch")
    assert e.args == ("-m", "RevenueChaser.module_spx_gamma", "--nightly")
    assert e.time == "06:30" and e.days == A._WEEKDAYS and e.after is None and not e.hourly
    assert e.tables_written == () and e.default_install is False
    assert e.settings.start_when_available is False
    assert e.schedule == "weekly Monday, Tuesday, Wednesday, Thursday, Friday 06:30 (local)"


def test_the_weekly_backup_is_the_only_row_running_rcdata_backup_and_takes_every_step():
    """One job, every step (avo, 8 Nov 2026): a new step or DuckDB kind lands in the weekly run by itself."""
    rows = [e for e in A.REGISTRY if list(e.args)[:3] == ["-m", "RevenueChaser.rcdata", "backup"]]
    assert [e.name for e in rows] == ["RC-Backup-Weekly"]
    assert "--only" not in rows[0].args and "--subdir" not in rows[0].args


def test_a_task_with_no_time_limit_says_so_and_installs_none():
    """#527: ``no_time_limit`` reads "no time limit", installs ``TimeoutStartSec=infinity`` and a zero (unlimited)
    Task Scheduler limit, and a chained task after it is not held to a limit it does not have."""
    s = A.TaskSettings(no_time_limit=True)
    assert s.limit_minutes is None
    assert "no time limit" in s.summary() and "h limit" not in s.summary()
    assert A.TaskSettings(wake_to_run=True, start_when_available=False).summary() == "skips a missed run, 4 h limit"
    # #579: the jobs run as systemd units, so no Task Scheduler words reach the Settings page
    assert not any(w in e.settings.summary() for e in A.REGISTRY for w in ("battery", "AC power", "PC", "logged on"))
    assert A.TaskSettings(time_limit_minutes=50).limit_minutes == 50
    assert A.TaskSettings(time_limit_hours=2).limit_minutes == 120


def test_the_ibkr_login_check_limit_is_the_installed_unit_s_five_minutes():
    """#527: the registry said 1 h while homesrv runs ``TimeoutStartSec=5min`` (home-server
    units/life-rc-ibkr-login-check.service); the board shows the registry, so it follows the unit."""
    assert A.entry("RC-IBKR-Login-Check").settings.limit_minutes == 5


@pytest.mark.parametrize("name,switch,runs", [
    ("RC-ValueChain-Sync", "fetch", False), ("RC-Build", "fetch", False), ("RC-Daily-Bars", "fetch", False),
    ("RC-Backup-Weekly", "fetch", True), ("RC-IBKR-Login-Check", "fetch", True),
    ("RC-Backup-Weekly", "backup", False), ("RC-Build", "backup", True),
])
def test_a_switched_off_task_does_not_start_its_command(monkeypatch, name, switch, runs):
    """#527: every RC systemd unit runs ``core.automations run <name>``, so the board's two switches are read here.
    Fetch stops every task but the backups and the IBKR login check; backup stops only the backups. A stopped task
    exits 0 (it is not a failed job) and says why in its log."""
    from RevenueChaser.rcdata import ops
    ops.switch_set(switch, False, by="avo")
    started = []
    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: started.append(a) or type("R", (), {"returncode": 0})())
    assert A.run_task(name) == A.RESULT_OK
    assert bool(started) is runs
    if not runs:
        text = A.entry(name).log_path.read_text(encoding="utf-8")
        assert f"skipped: the {switch} switch is off (avo at " in text
