"""Nightly inputs steps (C25a/K11, #383): one scheduled task that runs a list of independent sweeps.

A step is a registry entry (``core/automations.py``) whose command is
``python -m RevenueChaser.scripts.nightly_inputs <step> --max-uw-calls N --max-fred-calls N --max-yf-calls N``.
``STEPS`` maps the step name to the function that returns its sweeps; a sweep fetches one input and writes its own
tables. The contract every step keeps:

- **One session per run.** The run's New York session is resolved once (rule B, ``calendar.session_for``) and every
  sweep stamps it. A run inside the US session (``intraday``) runs no sweep, spends no call and exits 0: its bars are
  unfinished, and the next night's run writes the session. A run outside the holiday table runs nothing and exits 1.
- **Sweeps are independent.** Each runs in its own ``try``; one that raises is ``failed`` with its error and the
  others still run. Sweeps share data through the store, never through memory, so a sweep that reads what an earlier
  one wrote falls back to the rows already stored when that one failed.
- **Calls are capped per night, per provider.** One ``CallBudget`` per provider (``uw``, ``fred``, ``yfinance``,
  ``cboe``, ``cftc``), shared by every sweep of the run; a cap of 0 makes no call. A request that would pass the cap is
  refused before it is sent, and one log line says so. The count is per session, not per process: a second run of the
  same night (a manual run, a catch-up) starts from what the first one spent (``<step>_calls.json`` under
  ``config.OUTPUT_DIR``). A UW call is one ``get_result`` (the house convention, as ``prices.UW_NIGHTLY_CAP``): its
  retries of a 429 or 5xx are the same call.
- **job_runs and run ids as today.** The run id is ``ledger.new_run_id()``, so under the scheduler every row joins the
  task's ``job_runs`` row (``rawstore.task_run``). Writes go through ``record_first`` (append tables only: the first
  write of a key wins).
- **Exit code.** 0 when every sweep finished (``ok``, ``partial``, ``skipped``); 1 when one failed or the run had no
  session. A capped sweep is ``partial``, not failed.
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from RevenueChaser.core import calendar, config, ledger, rawstore
from RevenueChaser.core.providers.result import ProviderResult
from RevenueChaser.core.providers.unusual_whales import UWClient

PROVIDERS = ("uw", "fred", "yfinance", "cboe", "cftc")
STATUSES = ("ok", "partial", "skipped", "failed")

# step name -> "module:function" returning the step's sweeps (imported only when the step runs).
STEPS: dict[str, str] = {
    "earnings": "RevenueChaser.module_earnings_radar.inputs:sweeps",     # RC-Earnings-Inputs (E0, #372)
}


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


# --- call caps ------------------------------------------------------------------------------------------------------

class CallBudget:
    """At most ``limit`` calls to one provider this night; ``before`` were spent by an earlier run of the night."""

    def __init__(self, provider: str, limit: int, *, before: int = 0, log: Callable[[str], None] | None = None):
        if int(limit) < 0:
            raise ValueError(f"{provider}: a call cap cannot be negative ({limit})")
        self.provider, self.limit, self.before = provider, int(limit), max(0, int(before))
        self.used = self.refused = 0
        self._log = log
        self._told = False
        self._lock = threading.Lock()

    @property
    def left(self) -> int:
        return max(0, self.limit - self.before - self.used)

    def take(self, n: int = 1) -> bool:
        """Reserve ``n`` calls; False (and nothing reserved) when they would pass the cap."""
        with self._lock:
            if self.before + self.used + n <= self.limit:
                self.used += n
                return True
            self.refused += n
            tell, self._told = not self._told, True
        if tell and self._log:
            self._log(f"{self.provider}: call cap {self.limit} reached ({self.before + self.used} spent tonight); "
                      f"no further {self.provider} calls this run")
        return False

    def as_dict(self) -> dict:
        return {"limit": self.limit, "before": self.before, "used": self.used, "refused": self.refused}


class CappedUWClient(UWClient):
    """A ``UWClient`` whose every request first takes one call from ``budget``.

    Refused without a request (a ``failed`` result with ``attempts`` 0): no key (``missing_key``), the key was
    rejected earlier in the run (``auth_rejected``), or the night's cap is spent (``call_cap``). ``_batched`` and the
    ``*_result`` methods all go through ``get_result``, so every chunk counts.
    """

    def __init__(self, budget: CallBudget, key: str | None = None, sleep=None):
        super().__init__(key=key, sleep=sleep)
        self.budget = budget

    def get_result(self, ep: str, params: dict | None = None) -> ProviderResult:
        reason = "missing_key" if not self.key else "auth_rejected" if self.auth_error else None
        if reason is None and not self.budget.take():
            reason = "call_cap"
        if reason is not None:
            return ProviderResult("failed", {}, {"endpoint": ep, "status": None, "error": reason, "attempts": 0}, ep)
        return super().get_result(ep, params)


# --- the step -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sweep:
    """One input of a step: ``run(ctx)`` fetches it, writes ``tables`` and returns a result dict with ``status``."""
    name: str
    tables: tuple[str, ...]
    run: Callable[["StepRun"], dict]


@dataclass
class StepRun:
    """What a sweep gets: the run's identity and session, the budgets, the shared UW client and the log."""
    step: str
    run_id: str
    run_at: float
    session_date: date
    budgets: dict[str, CallBudget]
    log: Callable[[str], None]
    uw_factory: Callable[[CallBudget], UWClient] = CappedUWClient
    _uw: UWClient | None = field(default=None, repr=False)

    def budget(self, provider: str) -> CallBudget:
        return self.budgets[provider]

    def uw(self) -> UWClient:
        """One capped UW client for the whole run, built on first use."""
        if self._uw is None:
            self._uw = self.uw_factory(self.budgets["uw"])
        return self._uw

    def stamps(self, fetched_at: float | None = None) -> dict:
        return {"run_id": self.run_id, "run_at": self.run_at,
                "fetched_at": time.time() if fetched_at is None else fetched_at}

    def close(self) -> None:
        if self._uw is not None:
            getattr(self._uw, "close", lambda: None)()


def _ledger_path(ledger_dir: Path | None, step: str) -> Path:
    return Path(ledger_dir or (config.OUTPUT_DIR / "nightly_inputs")) / f"{step}_calls.json"


def _spent_before(path: Path, session: date) -> dict[str, int]:
    """What earlier runs of this session spent; nothing for another session or an unreadable file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("session_date") != session.isoformat():
            return {}
        return {p: int(n) for p, n in (data.get("spent") or {}).items() if p in PROVIDERS}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_spent(path: Path, session: date, budgets: dict[str, CallBudget], log) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        spent = {p: b.before + b.used for p, b in budgets.items()}
        path.write_text(json.dumps({"session_date": session.isoformat(), "spent": spent}), encoding="utf-8")
    except OSError as exc:   # bookkeeping: never a reason to fail the run
        log(f"call ledger not saved ({exc})")


def run_step(step: str, sweeps, *, caps: dict[str, int], run_id: str | None = None, run_at: float | None = None,
             log: Callable[[str], None] | None = None, ledger_dir: Path | None = None,
             uw_factory: Callable[[CallBudget], UWClient] | None = None) -> dict:
    """Run ``sweeps`` in order under one session and one set of budgets; see the module docstring."""
    log = log or _stderr
    unknown = set(caps) - set(PROVIDERS)
    if unknown:
        raise ValueError(f"no call cap for {sorted(unknown)}; providers are {', '.join(PROVIDERS)}")
    for p in PROVIDERS:
        if int(caps.get(p, 0)) < 0:
            raise ValueError(f"{p}: a call cap cannot be negative ({caps[p]})")
    run_at = time.time() if run_at is None else float(run_at)
    run_id = run_id or ledger.new_run_id()
    res: dict = {"step": step, "run_id": run_id, "run_at": run_at, "session_date": None, "skipped": None,
                 "error": None, "sweeps": {}, "calls": {}}
    try:
        stamp = calendar.session_for(run_at)
    except ValueError as exc:
        res["error"] = f"no session: {exc}"
        log(f"inputs {step}: {res['error']}; nothing ran")
        return res
    res["session_date"] = stamp.session_date.isoformat()
    if stamp.quality != calendar.QUALITY_COMPLETE:
        res["skipped"] = "intraday"
        log(f"inputs {step}: run inside the US session {res['session_date']}; nothing ran, nothing written")
        return res

    path = _ledger_path(ledger_dir, step)
    before = _spent_before(path, stamp.session_date)
    budgets = {p: CallBudget(p, int(caps.get(p, 0)), before=before.get(p, 0), log=log) for p in PROVIDERS}
    ctx = StepRun(step=step, run_id=run_id, run_at=run_at, session_date=stamp.session_date, budgets=budgets,
                  log=log, uw_factory=uw_factory or CappedUWClient)
    try:
        for sw in sweeps:
            try:
                out = sw.run(ctx)
                if not isinstance(out, dict) or out.get("status") not in STATUSES:
                    out = {"status": "failed", "error": f"sweep returned no result ({out!r})"[:300]}
            except Exception as exc:  # noqa: BLE001 - one sweep's failure never stops the others
                out = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
            res["sweeps"][sw.name] = out
            if out["status"] == "failed":
                log(f"inputs {step}: {sw.name} failed: {out.get('error')}")
    finally:
        ctx.close()
        _save_spent(path, stamp.session_date, budgets, log)
        res["calls"] = {p: b.as_dict() for p, b in budgets.items()}
    return res


def exit_code(res: dict) -> int:
    if res.get("error"):
        return 1
    return 1 if any(s.get("status") == "failed" for s in res.get("sweeps", {}).values()) else 0


def summary_line(res: dict) -> str:
    head = f"inputs {res.get('step')}: session {res.get('session_date') or '-'}"
    if res.get("error"):
        return f"{head}; {res['error']}"
    if res.get("skipped"):
        return f"{head}; skipped ({res['skipped']})"
    parts = []
    for name, s in res.get("sweeps", {}).items():
        text = f"{name} {s.get('status')}"
        if s.get("written") is not None:
            text += f" {s['written']} written"
        if s.get("note"):
            text += f" ({s['note']})"
        parts.append(text)
    calls = ", ".join(f"{p} {c['before'] + c['used']}/{c['limit']}" for p, c in res.get("calls", {}).items())
    return f"{head}; {'; '.join(parts) or 'no sweeps'}; calls {calls}"


def load_step(name: str):
    """The sweeps of step ``name``; KeyError for a step ``STEPS`` does not list."""
    module, func = STEPS[name].split(":")
    return tuple(getattr(importlib.import_module(module), func)())


# --- writes ---------------------------------------------------------------------------------------------------------

def record_first(table: str, rows: list[dict]) -> dict:
    """Append ``rows`` to an append-only table in one transaction; the first write of a key wins.

    ``{"offered": rows passed, "written": rows that were new}``. A table written another way is refused: a sweep
    must never overwrite an earlier run's rows."""
    spec = rawstore.MANIFEST.get(table)
    if not spec or spec.get("write") != "append":
        raise ValueError(f"{table} is not an append-only table; record_first writes only those")
    if not rows:
        return {"offered": 0, "written": 0}

    def _op(tx):
        tx.ensure_table(table)
        before = tx.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        tx.write_rows(table, rows)
        return tx.execute(f"SELECT count(*) FROM {table}").fetchone()[0] - before

    return {"offered": len(rows), "written": int(rawstore.atomic(_op, db=rawstore._db_of(table)))}
