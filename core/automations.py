"""Automations registry: every scheduled task RevenueChaser owns, and the one installer.

The Settings board lists one row per entry of ``REGISTRY`` and lets the user install,
enable, disable, remove or run a task by its exact registry name. Nothing here runs by
itself: ``default_install`` only marks a row, and no code path installs a task on
import, dashboard start, Refresh, GET or in a test (``inventory.collect`` never calls
the scheduler; e2e installs ``FakeScheduler``).

Two sides:

- ``Scheduler``: the interface to the OS scheduler. ``WindowsScheduler`` drives the
  PowerShell ``ScheduledTasks`` cmdlets, one registry task per call; ``LinuxScheduler``
  drives systemd user timers, one ``.service`` and ``.timer`` pair per registry task (#158);
  ``FakeScheduler`` backs the tests and the e2e dashboard; ``UnavailableScheduler`` is the
  answer on a host with neither (a state, not an error).
- ``run_task``: the command every task actually runs
  (``python -m RevenueChaser.core.automations run <name>``). It appends the job's
  output and exit code to the entry's ``log_path`` so a red row has a reason.

Registry names are stable: a task that writes tables records them as ``job_runs.registry_name`` (#129 A1).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable

from RevenueChaser.core import config

ACTIONS = ("install", "enable", "disable", "remove", "run")

# Documented Task Scheduler last-result codes.
RESULT_OK = 0
RESULT_RUNNING = 0x41301      # SCHED_S_TASK_RUNNING
RESULT_NOT_YET_RUN = 0x41303  # SCHED_S_TASK_HAS_NOT_RUN

_LOG_MAX_BYTES = 256 * 1024
_TAIL_LINES = 5
_TAIL_WIDTH = 200
_EXIT_RE = re.compile(r"^=== (\S+) exit (-?\d+)\s*$")
# ``off-machine copy OK|FAILED <iso>: ...``: the line scripts/backup_store.py prints after its off-machine step.
_OFFMACHINE_RE = re.compile(r"^off-machine copy (OK|FAILED) (\S+?)(?::\s|\s|$)")
OFFMACHINE_STALE_HOURS = 48          # one missed night is tolerated, two are not
# ``backup sizes <iso>: local <n> bytes[, off-machine <n> bytes]``: the last line of a backup run (#309); the
# off-machine part is missing when no folder is set or no copy was recorded yet.
_SIZES_RE = re.compile(r"^backup sizes (\S+?): local (\d+) bytes(?:, off-machine (\d+) bytes)?\s*$")
# Exit code of a chained task whose predecessor's run of tonight did not exit 0 (failed, blocked, missing or overdue).
CHAIN_BLOCKED = 5


# --- registry ---------------------------------------------------------------

@dataclass(frozen=True)
class TaskSettings:
    """The Windows task settings a row installs with (the defaults silently skip a laptop)."""
    start_when_available: bool = True
    allow_on_battery: bool = True
    wake_to_run: bool = False
    run_only_when_logged_on: bool = True
    time_limit_hours: int = 4
    restart_count: int = 2
    restart_minutes: int = 5

    def summary(self) -> str:
        parts = ["runs on battery" if self.allow_on_battery else "AC power only"]
        if self.wake_to_run:
            parts.append("wakes the PC")
        if self.start_when_available:
            parts.append("catches up a missed run")
        parts.append("only when logged on" if self.run_only_when_logged_on else "logged on or not")
        parts.append(f"{self.time_limit_hours} h limit")
        return ", ".join(parts)


@dataclass(frozen=True)
class AutomationEntry:
    name: str                      # the Task Scheduler name, always ``RC-...``
    label: str
    args: tuple[str, ...]          # ``python <args>``
    time: str                      # local HH:MM
    description: str
    tables_written: tuple[str, ...] = ()
    offpeak_gated: bool = False
    default_install: bool = False  # a marker only: nothing installs on its own
    settings: TaskSettings = field(default_factory=TaskSettings)
    offmachine: bool = False       # the row also shows the age of the last off-machine copy (#275)
    days: tuple[str, ...] = ()     # weekly on these days (Task Scheduler names, e.g. "Saturday"); empty = daily
    after: str | None = None       # the nightly chain (#129): run only once this registry task's run of tonight exited 0
    chain_wait_minutes: int = 120  # how long a chained task waits for that run before it gives up

    @property
    def schedule(self) -> str:
        if self.days:
            text = f"weekly {', '.join(self.days)} {self.time} (local)"
        else:
            text = f"daily {self.time} (local)"
        return f"{text}, after {self.after}" if self.after else text

    @property
    def command(self) -> str:
        return "python " + " ".join(self.args)

    @property
    def log_path(self) -> Path:
        return config.OUTPUT_DIR / "logs" / f"{self.name}.log"


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
_TUE_SAT = ("Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
# A nightly inputs step (C25a/K11, #383): see ``check_inputs_steps`` below.
INPUTS_CLI = "RevenueChaser.scripts.nightly_inputs"
INPUTS_PREDECESSOR = "RC-Daily-Bars"
INPUTS_CAP_FLAGS = ("--max-uw-calls", "--max-fred-calls", "--max-yf-calls")

REGISTRY: tuple[AutomationEntry, ...] = (
    AutomationEntry(
        name="RC-Store-Backup", label="Daily store backup",
        args=("-m", "RevenueChaser.scripts.backup_store", "--keep", "5"), time="04:00",
        description="Verified daily backup of the durable .store/ (raw, edgar, history, prices) and the SPX Gamma tree; keeps 5 daily raw and 5 daily history snapshots, the newest 12 weekly raw copies, and 2 daily prices snapshots. Every 7 days it re-reads the backup folder against the hashes taken at copy time.",
        default_install=True,
        settings=TaskSettings(wake_to_run=True, time_limit_hours=2), offmachine=True),
    AutomationEntry(
        name="RC-Relatedness-OffPeak", label="Relatedness (off-peak)",
        args=("-m", "RevenueChaser.module_relatedness", "--run", "--cap", "8"), time="12:30",
        description="Relatedness profiling through DeepSeek; defers itself outside off-peak hours.",
        tables_written=("relatedness_profiles", "relatedness_attributes", "relatedness_scores",
                        "run_registry", "llm_cache", "chain_filings", "chain_meta", "edgar_documents",
                        "result_runs", "relatedness_scores_history"),
        offpeak_gated=True),
    AutomationEntry(
        name="RC-ValueChain-Sync", label="Value Chain filing sync",
        args=("-m", "RevenueChaser.module_value_chain", "--sync-filings"), time="13:00",
        description="Detects newer 10-K/20-F filings and refreshes the raw doc and corpus; stores the SEC "
                    "submissions of every universe CIK (#299).",
        tables_written=("chain_filings", "chain_mentions", "chain_meta", "edgar_documents", "sec_submissions",
                        "sec_submissions_cik", "source_checks", "result_runs", "value_chain_edges_history")),
    AutomationEntry(
        name="RC-Daily-Bars", label="Daily price bars",
        args=("-m", "RevenueChaser.scripts.daily_bars", "--nightly", "--sec-files", "--index-levels"), time="22:35",
        description="Writes each scope ticker's bars up to the latest complete New York session (UW ohlc/1d, "
                    "yfinance fallback), yfinance adj_close, dividends and splits; no calls when current. First, "
                    "best effort, on weekdays: the SEC exchange and fund files (#301, at most 2 SEC requests a "
                    "night). Last, best effort: the ^GSPC daily levels (D03, #293; at most 1 yfinance request a "
                    "night, none when current or before the attended one-time fill). Neither step's failure "
                    "changes the exit code.",
        tables_written=("price_bars_daily", "price_actions", "price_checks", "sec_exchange_file", "sec_mf_file",
                        "index_levels_daily")),
    AutomationEntry(
        name="RC-Screeners-Daily", label="Daily screeners Fetch",
        args=("-m", "RevenueChaser.app.universe_cli"), time="23:00", days=_WEEKDAYS, after="RC-Daily-Bars",
        description="Step S2 of the nightly chain (#129): once tonight's daily bars run exited 0, the universe "
                    "refresh (AG with the universe pull, then Swing, then Fundamentals) and the forecast freeze "
                    "(#128, which writes nothing inside the US session). A busy universe lock exits 3.",
        tables_written=("equity_universe", "equity_universe_provenance", "quote_snapshot", "source_checks",
                        "ticker_payload_cache", "edgar_financials", "swing_ohlc", "swing_info", "ticker_scores",
                        "swing_scores", "swing_gates", "fundamentals_scores", "fundamentals_gates",
                        "vendor_financials", "vendor_valuation", "ownership_snapshot", "forecast_ledger",
                        "result_runs", "ag_scores_history", "swing_scores_history", "swing_gates_history",
                        "fundamentals_scores_history", "fundamentals_gates_history", "job_runs"),
        settings=TaskSettings(wake_to_run=True)),
    AutomationEntry(
        name="RC-Forecast-Outcomes", label="Forecast outcomes",
        args=("-m", "RevenueChaser.scripts.forecast_outcomes", "--nightly"), time="23:45", after="RC-Daily-Bars",
        description="Once tonight's daily bars run exited 0, recomputes the 30- and 90-day outcome (reference open, "
                    "MFE, MAE, open/closed) of every frozen pick not closed yet, from the stored bars; no calls "
                    "(#380 S0). 23:45 keeps it clear of RC-Screeners-Daily, the other raw.duckdb writer. A busy "
                    "forecast-outcomes lock exits 3, and nothing was written.",
        tables_written=("forecast_outcomes",)),
    AutomationEntry(
        name="RC-Forecast-Baselines", label="Forecast baselines",
        args=("-m", "RevenueChaser.scripts.forecast_baselines", "--nightly"), time="23:50",
        after="RC-Forecast-Outcomes",
        description="Once tonight's outcomes run exited 0, recomputes the baselines still open: what each freeze "
                    "date's own universe did over the same 30- and 90-day windows, and the same names split into ten "
                    "trailing-volatility deciles, from the stored bars and the universe change history; no calls "
                    "(#25 HR-1). Chained on RC-Forecast-Outcomes, the other raw.duckdb writer of the 23:45 slot, so "
                    "the two never run together; 23:50 is its own trigger.",
        tables_written=("forecast_baselines",)),
    AutomationEntry(
        name="RC-Index-Lists", label="Index lists (weekly)",
        args=("-m", "RevenueChaser.scripts.index_lists", "--weekly"), time="10:00", days=("Saturday",),
        description="Writes the S&P 500, Dow and Nasdaq member lists (3 UW requests) and the SmallCap 600 list "
                    "from the last accepted S&P 1500 snapshot; a list under its floor is kept as it was, and the "
                    "change history records each join and leave.",
        tables_written=("index_constituents", "index_constituents_changes")),
    AutomationEntry(
        name="RC-Earnings-Inputs", label="Earnings inputs (nightly)",
        args=("-m", INPUTS_CLI, "earnings", "--max-uw-calls", "200", "--max-fred-calls", "10",
              "--max-yf-calls", "2000", "--max-cboe-calls", "20", "--max-cftc-calls", "5"),
        time="01:30", days=_TUE_SAT, after="RC-Daily-Bars",
        description="The Earnings board's nightly inputs (E0, #372), after tonight's daily bars run exited 0: the "
                    "UW earnings calendar of the next four weeks, the economic calendar, 8 FRED series, short "
                    "interest of the calendar and 13F tickers (UW, then yfinance), yfinance analyst estimates, the "
                    "Cboe vol indices (VIX family, VVIX, OVX) with MOVE from yfinance, and the weekly CFTC "
                    "E-mini S&P 500 TFF row. At most 200 UW, 10 FRED, 2000 yfinance, 20 Cboe and 5 CFTC read calls "
                    "a night, no SEC call; one sweep failing leaves the others. 01:30 Tuesday to Saturday keys each "
                    "run to the US session just closed; a run inside the US session writes nothing. A busy step "
                    "lock exits 3, and nothing was written.",
        tables_written=("earnings_calendar", "short_interest", "economic_calendar", "macro_series",
                        "estimate_snapshots", "vol_index_daily", "cftc_cot"),
        settings=TaskSettings(wake_to_run=True)),
    AutomationEntry(
        name="RC-Open-Issues-Check", label="Open issues registry check",
        args=("-m", "RevenueChaser.scripts.open_issues_sync", "--check"), time="09:30",
        description="Checks docs/open_issues.json against the open issues of the public repo through the GitHub API "
                    "(read anonymously, 1-2 requests) and writes nothing, no table and no store file. Exit 1 means "
                    "the registry is stale and the open-issues panel is missing rows; if this clone is behind "
                    "main, git pull first (docs/open_issues.json is tracked, so --write on a stale clone sets "
                    "up a pull conflict), otherwise python -m RevenueChaser.scripts.open_issues_sync --write.",
        settings=TaskSettings(restart_count=0)),
    AutomationEntry(
        name="RC-IBKR-Login-Check", label="IBKR Gateway login check",
        args=("-m", "RevenueChaser.scripts.ibkr_login_check"), time="09:15",
        description="Looks at whether the IBKR Gateway is still logged in: one TCP connection to the "
                    "configured host and port and the TWS API version handshake, and nothing else - no "
                    "account, position, order or execution is ever asked for. Writes no table and no store "
                    "file, only its own record under .cache/ibkr_login/, which the Data Health card reads "
                    "(#162). It exits 0 whatever it finds: a session that expired on its weekly cycle is not "
                    "a failed job. Off until RC_IBKR_LOGIN_CHECK is set, and then it contacts nothing but "
                    "RC_IBKR_HOST:RC_IBKR_PORT (127.0.0.1:4001 by default).",
        settings=TaskSettings(restart_count=0, time_limit_hours=1)),
)
_BY_NAME = {e.name: e for e in REGISTRY}


def _minutes(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def check_chain(entries: tuple[AutomationEntry, ...]) -> None:
    """A chain step names an earlier registry entry (so no cycles) that runs daily (``chain_cutoff`` reads its time
    as a daily one, so a step after a weekly task would be blocked on every other day), is due 0 to under 12 h after
    its predecessor on the clock (a step due before it would be released by last night's run), and waits a positive
    time shorter than its own time limit."""
    seen: dict[str, AutomationEntry] = {}
    for e in entries:
        if e.after is not None:
            if e.after == e.name:
                raise ValueError(f"{e.name} is chained on itself")
            if e.after not in seen:
                known = any(x.name == e.after for x in entries)
                raise ValueError(f"{e.name} is chained on {e.after}, which is "
                                 + ("not an earlier entry" if known else "unknown"))
            if seen[e.after].days:
                raise ValueError(f"{e.name} is chained on {e.after}, which runs weekly "
                                 f"({', '.join(seen[e.after].days)}); a predecessor must run daily")
            gap = (_minutes(e.time) - _minutes(seen[e.after].time)) % (24 * 60)
            if gap >= 12 * 60:
                raise ValueError(f"{e.name} at {e.time} is not due within 12 h after {e.after} at "
                                 f"{seen[e.after].time} on the clock")
            if e.chain_wait_minutes <= 0:
                raise ValueError(f"{e.name} must wait a positive number of minutes")
            if e.chain_wait_minutes >= e.settings.time_limit_hours * 60:
                raise ValueError(f"{e.name} waits {e.chain_wait_minutes} min, not less than its time limit")
        seen[e.name] = e


check_chain(REGISTRY)


# --- nightly inputs steps (C25a/K11, #383) ------------------------------------------------------------------------
#
# An inputs step runs ``python -m INPUTS_CLI <step> <cap flags>`` (``core/nightly_inputs.py``): independent sweeps
# under one session and one per-night call budget per provider. Its entry runs after RC-Daily-Bars (so the session's
# bars and the day's calls are done before it starts), names a step ``nightly_inputs.STEPS`` knows, lists the tables
# it writes, and states every cap in its command, so the Settings row shows the night's call limits.

def check_inputs_steps(entries: tuple[AutomationEntry, ...]) -> None:
    from RevenueChaser.core import nightly_inputs

    for e in entries:
        args = list(e.args)
        if args[:2] != ["-m", INPUTS_CLI]:
            continue
        if e.after != INPUTS_PREDECESSOR:
            raise ValueError(f"{e.name} is an inputs step; it must run after {INPUTS_PREDECESSOR}")
        step = args[2] if len(args) > 2 else None
        if step not in nightly_inputs.STEPS:
            raise ValueError(f"{e.name} names unknown inputs step {step!r}")
        if not e.tables_written:
            raise ValueError(f"{e.name} is an inputs step; it must list the tables it writes")
        for flag in INPUTS_CAP_FLAGS:
            value = args[args.index(flag) + 1] if flag in args[:-1] else None
            if value is None or not value.isdigit():
                raise ValueError(f"{e.name} must state {flag} as a whole number of calls (got {value!r})")


check_inputs_steps(REGISTRY)


class AutomationError(Exception):
    http_status = 500


class UnknownTask(AutomationError):
    http_status = 404


class UnknownAction(AutomationError):
    http_status = 400


class SchedulerUnavailable(AutomationError):
    http_status = 409


def entry(name: str) -> AutomationEntry:
    try:
        return _BY_NAME[name]
    except (KeyError, TypeError):
        raise UnknownTask("unknown automation") from None


# --- scheduler interface ------------------------------------------------------

@dataclass
class TaskStatus:
    state: str = "ready"             # ready | disabled | running
    last_run: str | None = None      # ISO text
    next_run: str | None = None
    last_result: int | None = None
    missed_runs: int = 0


class Scheduler:
    """What the panel needs from the OS scheduler. Names are always registry names."""
    available = True

    def status(self, name: str) -> TaskStatus | None:
        """``None`` when the task is not installed."""
        raise NotImplementedError

    def install(self, e: AutomationEntry) -> None:
        raise NotImplementedError

    def set_enabled(self, name: str, enabled: bool) -> None:
        raise NotImplementedError

    def remove(self, name: str) -> None:
        raise NotImplementedError

    def run(self, name: str) -> None:
        raise NotImplementedError


class UnavailableScheduler(Scheduler):
    available = False

    def status(self, name):
        return None

    def _no(self, *a, **k):
        raise SchedulerUnavailable("Task Scheduler is not available on this host")

    install = set_enabled = remove = run = _no


class FakeScheduler(Scheduler):
    """In-memory scheduler; ``calls`` records every call, ``mutations`` the mutating ones."""

    def __init__(self, available: bool = True, tasks: dict[str, TaskStatus] | None = None):
        self.available = available
        self.tasks: dict[str, TaskStatus] = dict(tasks or {})
        self.calls: list[tuple] = []

    @property
    def mutations(self) -> list[tuple]:
        return [c for c in self.calls if c[0] != "status"]

    def status(self, name):
        self.calls.append(("status", name))
        return self.tasks.get(name)

    def install(self, e):
        self.calls.append(("install", e.name))
        self.tasks[e.name] = TaskStatus(state="ready", last_result=RESULT_NOT_YET_RUN,
                                        next_run=f"{e.time}")

    def set_enabled(self, name, enabled):
        self.calls.append(("enable" if enabled else "disable", name))
        if name in self.tasks:
            self.tasks[name].state = "ready" if enabled else "disabled"

    def remove(self, name):
        self.calls.append(("remove", name))
        self.tasks.pop(name, None)

    def run(self, name):
        self.calls.append(("run", name))
        if name in self.tasks:
            self.tasks[name].state = "running"


def _ps_quote(value: str) -> str:
    """A PowerShell single-quoted literal (an embedded ``'`` is doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def package_parent() -> Path:
    """The dir whose child is ``RevenueChaser``: the tasks' working directory."""
    return Path(__file__).resolve().parents[2]


# Every script starts with this. PowerShell 5.1 writes a CLIXML progress record to stderr
# when it loads the ScheduledTasks module, and its default output encoding is the OEM code
# page (a Turkish "ı" is 0x8D there), so: no progress records, UTF-8 out.
_PS_PRELUDE = ("$ProgressPreference = 'SilentlyContinue'; "
               "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8")


def _powershell(script: str) -> tuple[int, str, str]:
    """``(returncode, stdout, stderr)``. Any failure to run becomes an ``AutomationError``."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script], capture_output=True, encoding="utf-8-sig", errors="replace",
            timeout=60, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutomationError(f"could not run PowerShell: {exc}") from None
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def windowless_python(python: str) -> str:
    """``pythonw.exe`` next to ``python.exe`` when it exists, so a run opens no console."""
    p = Path(python)
    if p.name.lower() == "python.exe":
        w = p.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return python


class WindowsScheduler(Scheduler):
    """PowerShell ``ScheduledTasks`` cmdlets, exact registry names only, one task per call."""

    def __init__(self, runner: Callable[[str], tuple[int, str, str]] = _powershell,
                 python: str | None = None, workdir: Path | str | None = None):
        self._run = runner
        self._python = windowless_python(python or sys.executable)
        self._workdir = str(workdir or package_parent())

    def _exec(self, script: str) -> str:
        """Run one script; return its stdout. A non-zero exit raises with stderr's tail."""
        rc, out, err = self._run(_PS_PRELUDE + "\n" + script)
        if rc != 0:
            raise AutomationError(((err or out or "").strip() or "scheduler call failed")[-400:])
        return out

    def status(self, name):
        n = _ps_quote(entry(name).name)
        script = (
            f"$t = Get-ScheduledTask -TaskName {n} -ErrorAction SilentlyContinue\n"
            "if (-not $t) { '{\"installed\":false}'; exit 0 }\n"
            f"$i = Get-ScheduledTaskInfo -TaskName {n}\n"
            "[pscustomobject]@{installed=$true; state=[string]$t.State;"
            " last_run=$(if ($i.LastRunTime) { $i.LastRunTime.ToString('o') });"
            " next_run=$(if ($i.NextRunTime) { $i.NextRunTime.ToString('o') });"
            " last_result=[int64]$i.LastTaskResult; missed=[int]$i.NumberOfMissedRuns}"
            " | ConvertTo-Json -Compress")
        return parse_status(self._exec(script))

    def install(self, e):
        e = entry(e.name)
        s = e.settings
        flags = []
        if s.start_when_available:
            flags.append("-StartWhenAvailable")
        if s.allow_on_battery:
            flags += ["-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries"]
        if s.wake_to_run:
            flags.append("-WakeToRun")
        logon = "Interactive" if s.run_only_when_logged_on else "S4U"
        script = "\n".join([
            "$ErrorActionPreference = 'Stop'",
            f"$a = New-ScheduledTaskAction -Execute {_ps_quote(self._python)}"
            f" -Argument {_ps_quote(' '.join(('-m', 'RevenueChaser.core.automations', 'run', e.name)))}"
            f" -WorkingDirectory {_ps_quote(self._workdir)}",
            (f"$tr = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {','.join(e.days)} -At {_ps_quote(e.time)}"
             if e.days else f"$tr = New-ScheduledTaskTrigger -Daily -At {_ps_quote(e.time)}"),
            f"$s = New-ScheduledTaskSettingsSet {' '.join(flags)}"
            f" -ExecutionTimeLimit (New-TimeSpan -Hours {int(s.time_limit_hours)})"
            + (f" -RestartCount {int(s.restart_count)}"
               f" -RestartInterval (New-TimeSpan -Minutes {int(s.restart_minutes)})" if s.restart_count else ""),
            "$p = New-ScheduledTaskPrincipal"
            " -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
            f" -LogonType {logon}",
            f"Register-ScheduledTask -TaskName {_ps_quote(e.name)} -Action $a -Trigger $tr"
            f" -Settings $s -Principal $p -Description {_ps_quote(e.description)} -Force | Out-Null",
        ])
        self._exec(script)

    def set_enabled(self, name, enabled):
        verb = "Enable" if enabled else "Disable"
        self._exec(f"{verb}-ScheduledTask -TaskName {_ps_quote(entry(name).name)} | Out-Null")

    def remove(self, name):
        self._exec("Unregister-ScheduledTask -TaskName "
                   f"{_ps_quote(entry(name).name)} -Confirm:$false")

    def run(self, name):
        self._exec(f"Start-ScheduledTask -TaskName {_ps_quote(entry(name).name)}")


def parse_status(text: str) -> TaskStatus | None:
    import json
    # A BOM can still sit at the start of stdout (utf-8-sig only strips the first one).
    lines = [ln.strip().lstrip("\ufeff") for ln in (text or "").splitlines()
             if ln.strip().lstrip("\ufeff").startswith("{")]
    try:
        d = json.loads(lines[-1])
        if not isinstance(d, dict):
            raise ValueError
    except (ValueError, IndexError):
        raise AutomationError("unreadable scheduler answer") from None
    if not d.get("installed"):
        return None

    def when(v):
        # Task Scheduler reports 1999-11-30 / 0001-01-01 for "never".
        return None if not v or str(v)[:4] in ("1999", "0001", "1601") else str(v)

    state = str(d.get("state") or "").lower()
    return TaskStatus(
        state=state if state in ("disabled", "running") else "ready",
        last_run=when(d.get("last_run")), next_run=when(d.get("next_run")),
        last_result=d.get("last_result"), missed_runs=int(d.get("missed") or 0))


# --- Linux: systemd user timers (#158) ----------------------------------------------
#
# One ``<name>.service`` (Type=oneshot, runs ``automations run <name>``) and one ``<name>.timer`` per registry entry,
# both under the user's systemd folder and both named by the exact registry name. ``run_task`` and the chain read the
# logs, not the scheduler, so they behave the same under either scheduler. A user timer runs only while the user's
# systemd instance does: on a server that runs logged off, ``loginctl enable-linger <user>`` once, by hand.

_SYSTEMD_DAYS = {"Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed", "Thursday": "Thu", "Friday": "Fri",
                 "Saturday": "Sat", "Sunday": "Sun"}
_SHOW_TIMER = ("ActiveState", "Result", "NextElapseUSecRealtime", "LastTriggerUSec")
_SHOW_SERVICE = ("ActiveState", "Result", "ExecMainStatus", "ExecMainStartTimestamp")


# The variables a unit file may carry, and nothing else: unit files are world-readable and ``.env`` loads the API
# keys into the environment, so the environment is never copied. The data roots are written as this process resolved
# them, so a job systemd starts reads and writes the store and logs the panel reads; without them it would fall back
# to the checkout's own .store/.cache (``config._root``). The others are passed only when set.
UNIT_ENVIRONMENT = ("RC_STORE_DIR", "RC_CACHE_DIR", "RC_EDGAR_DOCS_DIR", "RC_DISCORD_MEDIA_DIR", "SPX_GAMMA_DATA_ROOT",
                    "RAW_STORE_BACKUP_DIR", "RAW_STORE_BACKUP_WEEKLY", "RAW_STORE_BACKUP_MIRROR", "TZ")


def _unit_environment() -> list[tuple[str, str]]:
    resolved = config.resolved_roots()
    out = []
    for name in UNIT_ENVIRONMENT:
        value = resolved.get(name) or os.environ.get(name)
        if value not in (None, ""):
            out.append((name, str(value)))
    return out


def _env_line(name: str, value: str) -> str:
    v = _unit_value(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{name}={v}"'


def _systemctl(args: list[str]) -> tuple[int, str, str]:
    """``systemctl --user <args>`` as ``(returncode, stdout, stderr)``. Any failure to run becomes an
    ``AutomationError``."""
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR") and hasattr(os, "getuid"):
        # A dashboard started outside a login session has no XDG_RUNTIME_DIR, and systemctl --user then cannot
        # find the user's bus. The user manager's runtime folder is always this one.
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    try:
        proc = subprocess.run(["systemctl", "--user", *args], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AutomationError(f"could not run systemctl: {exc}") from None
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _user_unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or ""
    if not Path(base).is_absolute():                 # the XDG spec: a relative value is invalid and ignored
        base = str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def on_calendar(e: AutomationEntry) -> str:
    """The entry's ``time`` and ``days`` as a systemd ``OnCalendar=`` spec, in local time like Task Scheduler's."""
    days = ",".join(_SYSTEMD_DAYS[d] for d in e.days)
    return f"{days + ' ' if days else ''}*-*-* {e.time}:00"


def _unit_value(value: str) -> str:
    """A value for a unit file line: one line, with ``%`` doubled so systemd expands no specifier in it."""
    value = str(value)
    if "\n" in value or "\r" in value or value.endswith("\\"):
        raise AutomationError("a unit file value cannot hold a line break or end in a backslash")
    return value.replace("%", "%%")


def _exec_arg(value: str) -> str:
    """One ``ExecStart=`` word: double-quoted, with ``\\``, ``"``, ``%`` and ``$`` escaped."""
    v = _unit_value(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{v}"'


def _show_value(v: str | None) -> str | None:
    """A ``systemctl show --timestamp=unix`` time as local ISO text; empty, ``n/a`` or 0 is ``None``."""
    v = (v or "").strip()
    if v.startswith("@"):
        v = v[1:]
    try:
        ts = float(v)
    except ValueError:
        return None
    if ts <= 0:
        return None
    if ts > 1e11:                    # a raw microsecond count, not seconds
        ts /= 1e6
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat()


def parse_show(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """``systemctl show <timer> <service>`` -> the two property blocks, in that order."""
    blocks: list[dict[str, str]] = [{}]
    for line in (text or "").splitlines():
        if not line.strip():
            if blocks[-1]:
                blocks.append({})
            continue
        key, sep, value = line.partition("=")
        if sep:
            blocks[-1][key.strip()] = value.strip()
    blocks = [b for b in blocks if b]
    if len(blocks) != 2 or "ActiveState" not in blocks[0] or "Result" not in blocks[1]:
        raise AutomationError("unreadable scheduler answer")
    return blocks[0], blocks[1]


class LinuxScheduler(Scheduler):
    """systemd user timers, exact registry names only, one unit pair per entry."""

    def __init__(self, runner: Callable[[list[str]], tuple[int, str, str]] = _systemctl,
                 python: str | None = None, workdir: Path | str | None = None, unit_dir: Path | str | None = None):
        self._run = runner
        self._python = python or sys.executable
        self._workdir = str(workdir or package_parent())
        self.unit_dir = Path(unit_dir) if unit_dir else _user_unit_dir()

    def _exec(self, *args: str) -> str:
        rc, out, err = self._run(list(args))
        if rc != 0:
            raise AutomationError(((err or out or "").strip() or "scheduler call failed")[-400:])
        return out

    def _files(self, name: str) -> tuple[Path, Path]:
        return self.unit_dir / f"{name}.service", self.unit_dir / f"{name}.timer"

    def status(self, name):
        name = entry(name).name
        service, timer = self._files(name)
        try:
            if not (service.is_file() and timer.is_file()):
                return None
        except OSError as exc:
            raise AutomationError(f"the unit folder could not be read: {exc}") from None
        out = self._exec("show", "--timestamp=unix",
                         "--property=" + ",".join(sorted(set(_SHOW_TIMER + _SHOW_SERVICE))),
                         timer.name, service.name)
        t, sv = parse_show(out)
        if t.get("ActiveState") == "failed" or t.get("Result", "success") != "success":
            # A failed timer never fires again until it is reset: an error row, never "disabled" (which it is not).
            raise AutomationError(f"the timer failed ({t.get('Result') or t.get('ActiveState')}); "
                                  f"systemctl --user status {timer.name}")
        started = _show_value(sv.get("ExecMainStartTimestamp"))
        triggered = _show_value(t.get("LastTriggerUSec"))
        if sv.get("ActiveState") in ("activating", "active", "reloading", "deactivating"):
            state, result = "running", RESULT_RUNNING
        else:
            state = "ready" if t.get("ActiveState") == "active" else "disabled"
            if started:
                try:
                    code = int(sv.get("ExecMainStatus") or 0)
                except ValueError:
                    raise AutomationError("unreadable scheduler answer") from None
                result = code if code or sv.get("Result") == "success" else 1
            elif triggered:
                result = None            # the user manager restarted since: the exit code is gone, the logs keep it
            else:
                result = RESULT_NOT_YET_RUN
        return TaskStatus(state=state, last_run=started or triggered,
                          next_run=_show_value(t.get("NextElapseUSecRealtime")), last_result=result)

    def install(self, e):
        e = entry(e.name)
        s = e.settings
        service_text = "\n".join([
            "[Unit]",
            f"Description={_unit_value(e.label)} (RevenueChaser {e.name})",
            "",
            "[Service]",
            "Type=oneshot",
            f"WorkingDirectory={_unit_value(self._workdir)}",
            *(_env_line(k, v) for k, v in _unit_environment()),
            "ExecStart=" + " ".join(_exec_arg(a) for a in
                                    (self._python, "-m", "RevenueChaser.core.automations", "run", e.name)),
            f"TimeoutStartSec={int(s.time_limit_hours)}h",
            "",
        ])
        timer_text = "\n".join([
            "[Unit]",
            f"Description={_unit_value(e.schedule)} (RevenueChaser {e.name})",
            "",
            "[Timer]",
            f"OnCalendar={on_calendar(e)}",
            f"Unit={e.name}.service",
            f"Persistent={'true' if s.start_when_available else 'false'}",
            # No WakeSystem=: a user timer cannot arm a wake alarm (CAP_WAKE_ALARM), and asking for one leaves the
            # timer failed and never run. ``wake_to_run`` is a Task Scheduler setting only.
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])
        service, timer = self._files(e.name)
        try:
            self.unit_dir.mkdir(parents=True, exist_ok=True)
            for path, text in ((service, service_text), (timer, timer_text)):
                tmp = path.with_name(path.name + ".tmp")
                try:
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(path)
                finally:
                    tmp.unlink(missing_ok=True)
        except OSError as exc:
            try:
                self._unlink(e.name)
            except AutomationError:
                pass
            raise AutomationError(f"the unit files could not be written: {exc}") from None
        try:
            self._exec("daemon-reload")
            self._exec("enable", "--now", timer.name)
        except AutomationError:
            # Roll back: a half-installed task reads as an error row, which the panel offers no button for.
            self._unlink(e.name)
            self._try("daemon-reload")
            raise

    def _try(self, *args: str) -> AutomationError | None:
        try:
            self._exec(*args)
        except AutomationError as exc:
            return exc
        return None

    def _unlink(self, name: str) -> None:
        try:
            for path in self._files(name):
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise AutomationError(f"the unit files could not be removed: {exc}") from None

    def set_enabled(self, name, enabled):
        self._exec("enable" if enabled else "disable", "--now", f"{entry(name).name}.timer")

    def remove(self, name):
        """Disable, delete both files, reload. The files go even when systemctl fails (no bus), so Remove can always
        clean up what Install left; the first failure is still raised, so the panel says so."""
        name = entry(name).name
        failed = self._try("disable", "--now", f"{name}.timer")
        try:
            self._unlink(name)
        except AutomationError as exc:
            failed = failed or exc
        reloaded = self._try("daemon-reload")
        failed = failed or reloaded
        if failed:
            raise failed

    def run(self, name):
        self._exec("start", "--no-block", f"{entry(name).name}.service")


# The Linux backend is opt-in: a server that already runs these jobs as system timers must not offer Install on every
# row, which would add a second schedule beside the first (the paid RC-Relatedness-OffPeak among them).
SCHEDULER_ENV = "RC_SCHEDULER"
SCHEDULER_SYSTEMD_USER = "systemd-user"


def _default_scheduler() -> Scheduler:
    if sys.platform == "win32":
        return WindowsScheduler()
    if (sys.platform.startswith("linux") and os.environ.get(SCHEDULER_ENV, "").strip() == SCHEDULER_SYSTEMD_USER
            and shutil.which("systemctl")):
        return LinuxScheduler()
    return UnavailableScheduler()


_scheduler: Scheduler | None = None
_lock = threading.Lock()


def get_scheduler() -> Scheduler:
    global _scheduler
    with _lock:
        if _scheduler is None:
            _scheduler = _default_scheduler()
        return _scheduler


def set_scheduler(s: Scheduler | None) -> None:
    global _scheduler
    with _lock:
        _scheduler = s


# --- rows ---------------------------------------------------------------------

def static_row(e: AutomationEntry) -> dict:
    """The fixed fields of a row; reading them never touches the scheduler."""
    return {
        "name": e.name, "label": e.label, "schedule": e.schedule, "command": e.command,
        "tables_written": list(e.tables_written), "offpeak_gated": e.offpeak_gated,
        "default_install": e.default_install, "settings": e.settings.summary(),
        "log_path": str(e.log_path), "description": e.description, "after": e.after,
    }


def static_rows() -> list[dict]:
    return [static_row(e) for e in REGISTRY]


def map_result(code: int | None) -> tuple[str, str]:
    """``(kind, text)``: ok | neutral | running | red | none."""
    if code is None:
        return "none", "—"
    if code == RESULT_OK:
        return "ok", "ok"
    if code == RESULT_NOT_YET_RUN:
        return "neutral", "not run yet"
    if code == RESULT_RUNNING:
        return "running", "running"
    return "red", f"failed (0x{code & 0xFFFFFFFF:X})"


def _read_tail(path: Path, nbytes: int = 8192) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def log_tail(path: Path) -> list[str]:
    lines = [ln.rstrip() for ln in _read_tail(path).splitlines() if ln.strip()]
    return [ln[:_TAIL_WIDTH] for ln in lines[-_TAIL_LINES:]]


def last_success(path: Path) -> str | None:
    """ISO time of the last ``=== <iso> exit 0`` footer in the log, if any."""
    best = None
    for ln in _read_tail(path, 64 * 1024).splitlines():
        m = _EXIT_RE.match(ln)
        if m and int(m.group(2)) == 0:
            best = m.group(1)
    return best


def last_offmachine(path: Path) -> tuple[str | None, str | None, bool | None]:
    """``(iso of the last OK copy, iso of the newest attempt, whether that attempt succeeded)`` from the backup log."""
    last_ok = newest = newest_ok = None
    for ln in _read_tail(path, 64 * 1024).splitlines():
        m = _OFFMACHINE_RE.match(ln)
        if m:
            newest, newest_ok = m.group(2), m.group(1) == "OK"
            if newest_ok:
                last_ok = newest
    return last_ok, newest, newest_ok


def _hours_since(iso: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return float("inf")
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 3600


def offmachine_state(e: AutomationEntry, now: datetime) -> dict | None:
    """The off-machine line of a row: ``{configured, kind: ok|red, text}``; None for rows that have none.

    Red when no folder is set (no copy leaves the machine), when no copy was made yet, when the newest attempt
    failed, or when the last good copy is older than ``OFFMACHINE_STALE_HOURS``.
    """
    if not e.offmachine:
        return None
    if not str(config.RAW_STORE_BACKUP_MIRROR).strip():
        return {"configured": False, "kind": "red",
                "text": "not set: RAW_STORE_BACKUP_MIRROR is empty, so no copy leaves this machine"}
    last_ok, newest, newest_ok = last_offmachine(e.log_path)
    if newest_ok is None:
        return {"configured": True, "kind": "red", "text": "no copy yet"}
    if newest_ok is False:
        tail = f"last OK {_age(last_ok, now)}" if last_ok else "no copy has succeeded yet"
        return {"configured": True, "kind": "red", "text": f"FAILED {_age(newest, now)}; {tail}"}
    if _hours_since(last_ok, now) >= OFFMACHINE_STALE_HOURS:
        return {"configured": True, "kind": "red",
                "text": f"last OK {_age(last_ok, now)}, older than {OFFMACHINE_STALE_HOURS} h"}
    return {"configured": True, "kind": "ok", "text": f"OK, {_age(last_ok, now)}"}


def last_backup_sizes(path: Path) -> tuple[str, int, int | None] | None:
    """``(iso, local bytes, off-machine bytes or None)`` of the newest sizes line, or None."""
    found = None
    for ln in _read_tail(path, 64 * 1024).splitlines():
        m = _SIZES_RE.match(ln)
        if m:
            found = (m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None)
    return found


def size_part(label: str, nbytes: int, limit_gb: float, env: str) -> tuple[str, bool]:
    """``("<label> 12.78 GB", False)``, or with ``, above <limit> GB (<env>)`` and True when ABOVE the limit."""
    gb = nbytes / 1e9
    if gb > limit_gb:
        return f"{label} {gb:.2f} GB, above {limit_gb:g} GB ({env})", True
    return f"{label} {gb:.2f} GB", False


def backup_size_state(e: AutomationEntry, now: datetime) -> dict | None:
    """The size line of the backup row (#309 part 3): ``{kind: ok|warn|none, text}``; None for other rows.

    Read from the job's own log line, so the page never walks either folder. ``warn`` when a folder is above its
    threshold (``RAW_STORE_BACKUP_WARN_GB`` local, ``RAW_STORE_BACKUP_MIRROR_WARN_GB`` off-machine)."""
    if not e.offmachine:
        return None
    found = last_backup_sizes(e.log_path)
    if found is None:
        return {"kind": "none", "text": "not measured yet: the next backup run logs it"}
    iso, local, off = found
    text, warn = size_part("local", local, config.RAW_STORE_BACKUP_WARN_GB, "RAW_STORE_BACKUP_WARN_GB")
    if off is not None:
        part, off_warn = size_part("off-machine", off, config.RAW_STORE_BACKUP_MIRROR_WARN_GB,
                                   "RAW_STORE_BACKUP_MIRROR_WARN_GB")
        text, warn = f"{text}, {part}", warn or off_warn
    return {"kind": "warn" if warn else "ok", "text": f"{text} ({_age(iso, now)})"}


def _age(iso: str | None, now: datetime) -> str:
    if not iso:
        return "never"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return "—"
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    secs = max(0, int((now - t).total_seconds()))
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def live_row(e: AutomationEntry, sched: Scheduler, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    row = static_row(e)
    row["offmachine"] = offmachine_state(e, now)
    row["backup_size"] = backup_size_state(e, now)
    try:
        st = sched.status(e.name) if sched.available else None
    except AutomationError as exc:
        # One unreadable task is an error row, never "not installed" (which offers Install)
        # and never a failure of the whole list.
        row.update(state="error", result_kind="red", result_text=str(exc)[:400],
                   last_run=None, next_run=None, missed_runs=0,
                   last_success=last_success(e.log_path),
                   last_success_age=_age(last_success(e.log_path), now),
                   log_tail=log_tail(e.log_path))
        return row
    if not sched.available:
        state = "unavailable"
    elif st is None:
        state = "not_installed"
    elif st.state == "disabled":
        state = "disabled"
    elif st.state == "running" or st.last_result == RESULT_RUNNING:
        state = "running"
    else:
        state = "enabled"
    kind, text = map_result(st.last_result if st else None)
    ok_at = last_success(e.log_path)
    if ok_at is None and st and st.last_result == RESULT_OK:
        ok_at = st.last_run
    row.update(
        state=state, result_kind=kind, result_text=text,
        last_run=st.last_run if st else None, next_run=st.next_run if st else None,
        missed_runs=st.missed_runs if st else 0,
        last_success=ok_at, last_success_age=_age(ok_at, now),
        log_tail=log_tail(e.log_path))
    return row


def live_state(sched: Scheduler | None = None, now: datetime | None = None) -> dict:
    sched = sched or get_scheduler()
    return {"available": bool(sched.available),
            "rows": [live_row(e, sched, now) for e in REGISTRY]}


def apply(name: str, action: str, sched: Scheduler | None = None) -> dict:
    """Run one panel action on one registry task and return its fresh row."""
    e = entry(name)
    if action not in ACTIONS:
        raise UnknownAction("unknown action")
    sched = sched or get_scheduler()
    if not sched.available:
        raise SchedulerUnavailable("Task Scheduler is not available on this host")
    if action == "install":
        sched.install(e)
    elif action == "enable":
        sched.set_enabled(e.name, True)
    elif action == "disable":
        sched.set_enabled(e.name, False)
    elif action == "remove":
        sched.remove(e.name)
    else:
        sched.run(e.name)
    return live_row(e, sched)


# --- the nightly chain (#129 slice E) ----------------------------------------------
#
# A chained entry stays its own Task Scheduler task with its own daily trigger, which is its
# not-before time. When it starts, it waits for its predecessor's run of tonight, read from the
# predecessor's log: the newest run that started at or after the predecessor's last scheduled
# time. That run exiting 0 releases the step; any other exit (a failure, or a step blocked in
# turn) blocks it at once; no such run within ``chain_wait_minutes`` blocks it too. A blocked
# step does not run its command and exits ``CHAIN_BLOCKED``, so its row is red with the reason
# in the log. The check runs when the task starts, not when it was scheduled, so a catch-up
# after a sleeping night waits for the predecessor's own catch-up.

CHAIN_POLL_SECONDS = 60
_START_RE = re.compile(r"^=== (\S+) start ")


@dataclass(frozen=True)
class RunRecord:
    start: datetime
    end: datetime | None   # None while the run is going (or when it died without a footer)
    rc: int | None


def chain_cutoff(hhmm: str, now: datetime, tz: tzinfo | None = None) -> datetime:
    """The last time at or before ``now`` that a daily ``hhmm`` task (local time) was due.

    ``tz`` None means the machine's local zone, which is what Task Scheduler triggers on.
    """
    h, m = (int(x) for x in hhmm.split(":"))
    if tz is None:
        local_now = now.astimezone()
        at = lambda d: datetime.combine(d, dtime(h, m)).astimezone()  # noqa: E731
    else:
        local_now = now.astimezone(tz)
        at = lambda d: datetime.combine(d, dtime(h, m), tzinfo=tz)  # noqa: E731
    cut = at(local_now.date())
    return cut if cut <= local_now else at(local_now.date() - timedelta(days=1))


def _parse_iso(text: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def last_run(path: Path) -> RunRecord | None:
    """The newest run in a task's log: its start, and its exit if a footer follows it."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rec: RunRecord | None = None
    for ln in text.splitlines():
        m = _START_RE.match(ln)
        if m and (t := _parse_iso(m.group(1))) is not None:
            rec = RunRecord(start=t, end=None, rc=None)
            continue
        m = _EXIT_RE.match(ln)
        if m and rec is not None:   # the last footer of a run wins: run_task writes it after the child's output
            rec = RunRecord(start=rec.start, end=_parse_iso(m.group(1)), rc=int(m.group(2)))
    return rec


def wait_for_chain(e: AutomationEntry, *, clock: Callable[[], datetime] | None = None,
                   sleep: Callable[[float], None] | None = None,
                   tz: tzinfo | None = None) -> tuple[bool, str]:
    """``(released, log line)`` for a chained entry; see the block comment above."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    sleep = sleep or _time.sleep
    pred = entry(e.after)
    started = clock()
    cutoff = chain_cutoff(pred.time, started, tz)
    deadline = started + timedelta(minutes=e.chain_wait_minutes)
    while True:
        r = last_run(pred.log_path)
        tonight = r is not None and r.start >= cutoff
        if tonight and r.rc is not None:
            line = f"{pred.name} exit {r.rc} at {r.end.isoformat() if r.end else '?'}"
            return r.rc == RESULT_OK, line
        if clock() >= deadline:
            state = "still running" if tonight else "no run started"
            return False, (f"{pred.name} has not succeeded since {cutoff.isoformat()} "
                           f"(waited {e.chain_wait_minutes} min; {state})")
        sleep(CHAIN_POLL_SECONDS)


# --- the command a task runs ----------------------------------------------------
#
# A task's command runs as a subprocess, so the dashboard's job registry (app/jobs.py) never sees it. A task that
# writes tables (``tables_written``) records its own ``job_runs`` row (#129 A1): ``trigger`` "scheduled",
# ``registry_name``, upserted when the command starts and when it ends ("done" on exit 0, else "error" with the exit
# code or the chain's reason). The command gets the row's id in ``RC_RUN_ID`` and the registry name in
# ``RC_RUN_SOURCE`` (``rawstore.task_run``), so the run ids and change rows it writes join that row. A task that
# writes no table (the backup) records nothing: it must leave the store exactly as it copied it. A row that cannot
# be written is a ``job_runs: not recorded`` line in the task's log, never a different exit code.

def _record_run(row: dict) -> None:
    from RevenueChaser.core import rawstore
    rawstore.write_rows("job_runs", [row])


def run_task(name: str, python: str | None = None, cwd: Path | str | None = None, *,
             clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] | None = None,
             tz: tzinfo | None = None) -> int:
    """Run a registry entry's command, appending its output and exit code to its log.

    A chained entry first waits for its predecessor (``wait_for_chain``); blocked, it exits
    ``CHAIN_BLOCKED`` without running its command. A task that writes tables records its ``job_runs`` row
    (see the block comment above).
    """
    import os
    import uuid

    e = entry(name)
    log = e.log_path
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log.stat().st_size > _LOG_MAX_BYTES:
            log.replace(log.with_suffix(".log.1"))
    except OSError:
        pass
    stamp = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    epoch = lambda: datetime.now(timezone.utc).timestamp()  # noqa: E731
    run_id = uuid.uuid4().hex[:12]
    row = {"run_id": run_id, "job_id": run_id, "module": e.name, "lane": None, "trigger": "scheduled",
           "source": e.name, "pid": os.getpid(), "registry_name": e.name, "params": {"args": list(e.args)},
           "queued_at": epoch(), "started_at": None, "finished_at": None, "status": "queued",
           "error": None, "message": None, "counts": None}

    with open(log, "a", encoding="utf-8") as fh:
        # A previous run whose end write was skipped is still ``running`` until something starts (#82).
        try:
            if e.tables_written:
                from RevenueChaser.app import jobs
                jobs.reconcile_lost_runs()
        except Exception as exc:  # noqa: BLE001 - bookkeeping, never a different exit code
            fh.write(f"job_runs: could not reconcile ({str(exc)[:200]})\n")
            fh.flush()

        def record(**fields) -> None:
            if not e.tables_written:
                return
            row.update(fields)
            try:
                _record_run(dict(row))
            except Exception as exc:  # noqa: BLE001 - the run log is bookkeeping, never a reason to fail a task
                fh.write(f"job_runs: not recorded ({str(exc)[:200]})\n")
                fh.flush()

        fh.write(f"=== {stamp()} start {e.name}\n")
        fh.flush()
        if e.after:
            fh.write(f"chain: waiting for {e.after} (up to {e.chain_wait_minutes} min)\n")
            fh.flush()
            released, line = wait_for_chain(e, clock=clock, sleep=sleep, tz=tz)
            if not released:
                fh.write(f"chain: blocked: {line}\n")
                record(status="error", finished_at=epoch(), error=f"chain blocked: {line}",
                       message=f"exit {CHAIN_BLOCKED}")
                fh.write(f"=== {stamp()} exit {CHAIN_BLOCKED}\n")
                return CHAIN_BLOCKED
            fh.write(f"chain: {line}\n")
            fh.flush()
        record(status="running", started_at=epoch())
        env = None
        if e.tables_written:
            from RevenueChaser.core import rawstore
            env = {**os.environ, rawstore.RUN_ID_ENV: run_id, rawstore.RUN_SOURCE_ENV: e.name}
        try:
            rc = subprocess.run([python or sys.executable, *e.args],
                                cwd=str(cwd or package_parent()),
                                stdout=fh, stderr=subprocess.STDOUT, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                ).returncode
        except OSError as exc:
            fh.write(f"could not start: {exc}\n")
            rc = 1
        fh.flush()
        record(status="done" if rc == RESULT_OK else "error", finished_at=epoch(),
               error=None if rc == RESULT_OK else f"exit {rc}", message=f"exit {rc}")
        fh.write(f"=== {stamp()} exit {rc}\n")
    return rc


def main(argv: list[str] | None = None) -> int:
    """``run <name>`` is what a task executes; ``install|remove <name>`` and ``list`` are
    the thin-caller entry points of the ``.ps1`` files (they act only on registry names).
    ``names`` prints the registry names one per line and never asks the scheduler, so a script that
    freezes or lists the tasks reads them from here instead of keeping its own copy (#164)."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 2 and args[0] == "run":
            return run_task(args[1])
        if len(args) == 2 and args[0] in ("install", "remove"):
            row = apply(args[1], args[0])
            print(f"{args[0]}: {row['name']} -> {row['state']}")
            return 0
        if args == ["names"]:
            print("\n".join(e.name for e in REGISTRY))
            return 0
        if args == ["list"]:
            for r in live_state()["rows"]:
                print(f"{r['name']:<26} {r['state']:<14} {r['schedule']}")
            return 0
    except AutomationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("usage: python -m RevenueChaser.core.automations "
          "run|install|remove <registry-name> | list | names", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
