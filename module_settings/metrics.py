"""``GET /metrics``: the Data/Health numbers in Prometheus text format, for Grafana on homesrv (#527 plan B1).

The RC page stays the library (what data exists, settings, complex presses); the live view of health and fetch
moves to Grafana (avo, 6 Nov 2026: "health ve fetch takibi, datanin sagligi grafana da olur"). Prometheus scrapes
this every 60 s, so it reads only what is cheap:

- the ops store (``rcdata/ops.py``: checks, schedules, queue, runs, switches, provider calls), never the serving
  DuckDB files: no ``newdata.collect``, no ``_serving_facts``, no store session (``app/dashboard.py`` skips it for
  this route);
- the backup manifests (small JSON), the IBKR login check's own record (no socket), ``/proc`` for running jobs;
- the builder timer (``systemctl show``, up to 10 s), which is read in the background at most once a minute and
  served from the last reading, so a scrape never waits on systemd.

Every reader that fails leaves its family empty and counts one ``rc_metrics_read_errors_total{part}``; the page
never fails a scrape. Thresholds are exported from ``control.py`` (``rc_threshold``) so a Grafana rule reads the
same number the page does instead of a retyped copy.

Metric names are a contract with the Grafana boards (home-server ``grafana/make_dashboards.py``); renaming one is a
change on both sides.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable

from RevenueChaser.module_settings import control

#: The result of a dataset's newest check, higher is worse (as homeops/metrics.py does for its jobs).
RESULT_CODE = {"changed": 0, "unchanged": 0, "skipped": 1, "failed": 2}
#: ``control.running_jobs``'s "who" words as a closed set, so a label can never carry free text.
WHO = ("systemd", "claude", "unknown", "lost")
STATES = ("fresh", "unchanged", "late", "stale", "never")
#: The IBKR gateway's login states (core/ibkr_login.py), as label values; anything else reads ``unknown``.
IBKR_STATES = ("up", "silent", "down", "unknown", "not_set")
#: The parts ``collect`` reads one by one; each starts its error counter at 0 so a first failure is an increase.
PARTS = ("queue", "providers", "datasets", "builder", "timer", "backups", "switches", "ibkr")
TIMER_EVERY_S = 60.0

HELP = {
    "rc_queue_requests": ("gauge", "Build requests in the queue now, by status (queued, running)."),
    "rc_run_elapsed_seconds": ("gauge", "Seconds a running build request has run, by request id, dataset and who "
                               "started it."),
    "rc_run_limit_seconds": ("gauge", "The time limit of a running build request's unit, when it has one."),
    "rc_provider_calls_today": ("gauge", "Calls to a provider on the current New York day."),
    "rc_provider_day_cap": ("gauge", "Our own day cap for a provider (absent: no cap)."),
    "rc_provider_failures_today": ("gauge", "Failed calls to a provider on the current New York day."),
    "rc_dataset_state": ("gauge", "1 for the dataset's current state (fresh, unchanged, late, stale, never)."),
    "rc_dataset_last_check_result": ("gauge", "The newest check's result: 0 changed or unchanged, 1 skipped, "
                                     "2 failed (higher is worse)."),
    "rc_dataset_checked_timestamp_seconds": ("gauge", "When the dataset's newest check ran (its New York day, "
                                             "00:00 UTC), epoch seconds."),
    "rc_dataset_paused": ("gauge", "1 when the dataset's schedule is paused."),
    "rc_dataset_open_issues": ("gauge", "Open build issues of the dataset."),
    "rc_builder_last_run_timestamp_seconds": ("gauge", "When the builder last started a run, epoch seconds."),
    "rc_builder_timer_on": ("gauge", "1 when the hourly builder timer is on, 0 when off (absent: not known)."),
    "rc_backup_newest_timestamp_seconds": ("gauge", "The newest backup of a kind, epoch seconds."),
    "rc_switch_on": ("gauge", "1 when a switch (fetch, backup) is on."),
    "rc_ibkr_state": ("gauge", "1 for the IBKR gateway's current login state (up, silent, down, unknown, "
                      "not_set)."),
    "rc_threshold": ("gauge", "The thresholds the Data/Health page alarms at, from module_settings/control.py."),
    "rc_metrics_read_errors_total": ("counter", "Readers that failed since the dashboard started, by part."),
    "rc_metrics_build_seconds": ("gauge", "How long building this answer took."),
}

_errors: dict[str, int] = {p: 0 for p in PARTS}
_errors_lock = threading.Lock()


def _failed(part: str) -> None:
    with _errors_lock:
        _errors[part] = _errors.get(part, 0) + 1


class _Timer:
    """The builder timer, read in a background thread at most once per ``TIMER_EVERY_S``."""

    def __init__(self, reader: Callable[[], dict] | None = None, clock: Callable[[], float] = time.monotonic):
        self._reader = reader
        self._clock = clock
        self._value: dict | None = None
        self._read_at: float | None = None
        self._busy = False
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        try:
            value = control._timer(self._reader)
        except Exception:  # noqa: BLE001 - _timer already turns a failure into Nones; this is belt and braces
            value = dict(control.NO_TIMER)
        with self._lock:
            self._value, self._busy = value, False

    def get(self, wait: bool = False) -> dict | None:
        """The last reading (None before the first one finishes). Starts a refresh when the last is old."""
        with self._lock:
            due = not self._busy and (self._read_at is None or self._clock() - self._read_at >= TIMER_EVERY_S)
            if due:
                self._busy, self._read_at = True, self._clock()
        if due:
            worker = threading.Thread(target=self._refresh, name="rc-metrics-timer", daemon=True)
            worker.start()
            if wait:
                worker.join()
        with self._lock:
            return self._value


TIMER = _Timer()


def _who(words: str) -> str:
    if words.startswith("systemd"):
        return "systemd"
    if words == "Claude session":
        return "claude"
    if words.startswith("process gone"):
        return "lost"
    return "unknown"


def _day_epoch(value) -> float | None:
    day = control._day(value)
    return None if day is None else datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()


def _iso_epoch(value) -> float | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def collect(now: datetime | None = None, *, datasets: dict | None = None, timer: dict | None = None,
            ibkr: Callable[[], dict | None] | None = None, cgroup=None, alive=None) -> dict[str, list]:
    """``{family: [(labels, value), ...]}``. Each part is read on its own; a part that fails is left empty and
    counted, and a dataset that fails is left out of the dataset families and counted under ``datasets``. ``timer`` is the builder timer's reading (default: ``TIMER.get()``)."""
    from RevenueChaser.module_settings import newdata
    from RevenueChaser.rcdata import ops
    now = now or datetime.now(timezone.utc)
    out: dict[str, list] = {name: [] for name in HELP}

    def part(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - one unreadable part never fails the scrape
            _failed(name)

    def queue() -> None:
        q = ops.read_queue(finished=0)
        active = q.get("active") or []
        for status in ("queued", "running"):
            out["rc_queue_requests"].append(({"status": status}, sum(1 for r in active if r.get("status") == status)))
        from RevenueChaser.core import procs
        for r in control.running_jobs(active, control._read_runs_now(), cgroup or control._read_cgroup,
                                      alive or procs.pid_alive, now):
            # The request id keeps two runs of one dataset two series (a duplicate sample would be dropped).
            ident = {"id": str(r.get("id")), "dataset": r["dataset"]}
            if r["elapsed_s"] is not None:
                out["rc_run_elapsed_seconds"].append(({**ident, "who": _who(r["who"])}, round(r["elapsed_s"], 1)))
            if r["limit_s"]:
                out["rc_run_limit_seconds"].append((ident, r["limit_s"]))

    def providers() -> None:
        for p in newdata._spend(now)["providers"]:
            name = {"provider": str(p["provider"])}
            out["rc_provider_calls_today"].append((name, p["today"]))
            out["rc_provider_failures_today"].append((name, p["failures_today"]))
            if p.get("cap"):
                out["rc_provider_day_cap"].append((name, p["cap"]))

    def dataset_states() -> None:
        for name, ds in (datasets if datasets is not None else newdata._datasets()).items():
            # A dataset that cannot be read is counted and left out whole; the ones after it still report.
            rows: dict[str, list] = {}
            try:
                one_dataset(name, ds, rows)
            except Exception:  # noqa: BLE001 - one dataset never drops the rest
                _failed("datasets")
                continue
            for family, got in rows.items():
                out[family].extend(got)

    def one_dataset(name, ds, out) -> None:
        """One dataset's rows into ``out`` (its own dict, merged only when the whole dataset read)."""
        for family in ("rc_dataset_state", "rc_dataset_last_check_result", "rc_dataset_checked_timestamp_seconds",
                       "rc_dataset_paused", "rc_dataset_open_issues"):
            out[family] = []
        label = {"dataset": name, "kind": str(getattr(ds, "kind", "") or "")}
        recent = ops.recent_checks(name, limit=1)
        last = recent[0] if recent else None
        schedule = ops.read_schedule(name)
        state, _, base = newdata.status_of(name, last, ops.latest_unskipped_check(name),
                                           newdata._cadence(name, schedule), now)
        for s in STATES:
            out["rc_dataset_state"].append(({**label, "state": s}, 1 if s == state else 0))
        if last is not None:
            out["rc_dataset_last_check_result"].append((label, RESULT_CODE.get(last["result"], 2)))
            when = _day_epoch(last.get("checked_on"))
            if when is not None:
                out["rc_dataset_checked_timestamp_seconds"].append((label, when))
        paused = bool(schedule and schedule.get("paused_at") is not None)
        out["rc_dataset_paused"].append((label, 1 if paused else 0))
        out["rc_dataset_open_issues"].append((label, ops.open_issues(name, limit=0)[0]))

    def builder() -> None:
        run = ops.last_builder_run()
        if run and run.get("started_at") is not None:
            out["rc_builder_last_run_timestamp_seconds"].append(({}, float(run["started_at"])))

    def timer_part() -> None:
        state = (timer if timer is not None else TIMER.get() or {}).get("state")
        if state in ("on", "off"):
            out["rc_builder_timer_on"].append(({}, 1 if state == "on" else 0))

    def backups() -> None:
        for k in newdata._backups(now)["kinds"]:
            when = _iso_epoch(k.get("newest"))
            if when is not None:
                out["rc_backup_newest_timestamp_seconds"].append(({"kind": k["kind"]}, when))

    def switches() -> None:
        for name, view in control.switches_view(control._read_switches(), now).items():
            if view["on"] is not None:
                out["rc_switch_on"].append(({"name": name}, 1 if view["on"] else 0))

    def gateway() -> None:
        if ibkr is not None:
            got = ibkr()
        else:
            from RevenueChaser.core import ibkr_login
            got = ibkr_login.collect(now=now)
        state = str((got or {}).get("state") or "unknown").replace(" ", "_").replace("-", "_")
        if state not in IBKR_STATES:          # the record is a file: any other text is "unknown", never a label
            state = "unknown"
        for s in IBKR_STATES:
            out["rc_ibkr_state"].append(({"state": s}, 1 if s == state else 0))

    parts = {"queue": queue, "providers": providers, "datasets": dataset_states, "builder": builder,
             "timer": timer_part, "backups": backups, "switches": switches, "ibkr": gateway}
    for name in PARTS:
        part(name, parts[name])
    for key, value in (("run_alert_share", control.RUN_ALERT), ("cap_alert_share", control.CAP_ALERT),
                       ("builder_silent_minutes", control.BUILDER_SILENT_MINUTES),
                       ("backup_alert_days", control.BACKUP_ALERT_DAYS),
                       ("claude_session_limit_seconds", control.CLAUDE_SESSION_LIMIT_S)):
        out["rc_threshold"].append(({"name": key}, value))
    with _errors_lock:
        out["rc_metrics_read_errors_total"] = [({"part": p}, n) for p, n in sorted(_errors.items())]
    return out


def _label_value(value) -> str:
    """A Prometheus label value: backslash, newline and double quote escaped (exposition format, not HTML)."""
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def render(families: dict[str, list], build_seconds: float | None = None) -> str:
    """Prometheus text exposition (version 0.0.4): ``# HELP`` and ``# TYPE`` for every family, even an empty one."""
    if build_seconds is not None:
        families = {**families, "rc_metrics_build_seconds": [({}, round(build_seconds, 4))]}
    lines = []
    for name, (kind, words) in HELP.items():
        lines.append(f"# HELP {name} {words}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in families.get(name) or []:
            inner = ",".join(f'{k}="{_label_value(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{inner}}} {_number(value)}" if inner else f"{name} {_number(value)}")
    return "\n".join(lines) + "\n"


def exposition(now: datetime | None = None) -> str:
    started = time.monotonic()
    families = collect(now)
    return render(families, time.monotonic() - started)


def datasets_list(datasets: dict | None = None) -> list[dict]:
    """``GET /api/build/datasets``: every dataset the builder knows, for an allow-list (Grafana buttons, B3)."""
    from RevenueChaser.module_settings import newdata
    from RevenueChaser.rcdata.build import base
    out = []
    for name, ds in sorted((datasets if datasets is not None else newdata._datasets()).items()):
        out.append({"name": name, "kind": str(getattr(ds, "kind", "") or ""), "attended": base.attended(ds),
                    "providers": list(getattr(ds, "providers", ()) or ())})
    return out
