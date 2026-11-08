"""What a run did, for ``job_runs.counts`` and the Runs tab's column (#136).

Two writers record a run: ``app/jobs.py`` for a dashboard job and ``core/automations.py`` for a scheduled
task. They get their numbers from different places — a dashboard runner hands them over in Python, a
scheduled step prints them on stdout — so this module holds the one rule for what the column may contain,
and both call it. Without that the same work records different shapes depending on who started it.

**The rule: counts are numbers the step itself reported.** :func:`numeric` keeps a result's own integer
fields and nested mappings whose values are all integers (``rows_by_source``: ``{"uw": n, "yfinance": m}``,
the breakdown an operator actually wants beside the total). Everything else is dropped — a run id, a session
date, a duration in seconds, a float ratio, a list of examples, a nested diagnostics blob — because the
column is read as "how much did this run do" and a string or a statistics bundle there reads as a count
that was never measured. ``bool`` goes too, although it is an ``int``: ``locked`` and ``no_fetch`` are flags,
not amounts.

Nothing is computed, summed or renamed. If a step does not report a number, the column stays NULL, which
says "unknown" honestly; an invented or reshaped figure would not.
"""
from __future__ import annotations

import json
from pathlib import Path

TAIL_BYTES = 64 * 1024     # a result object is one line; a long log's earlier output is not read


def _all_ints(value) -> bool:
    return isinstance(value, dict) and bool(value) and all(
        isinstance(v, int) and not isinstance(v, bool) for v in value.values())


def numeric(result) -> dict | None:
    """The counts in ``result``, per the module's rule, or ``None`` when it reports none.

    ``bool`` is excluded by ``not isinstance(v, bool)`` rather than by ``type(v) is int``, so an ``int``
    subclass a provider or pandas hands back is still a count.
    """
    if not isinstance(result, dict):
        return None
    counts = {k: v for k, v in result.items()
              if (isinstance(v, int) and not isinstance(v, bool)) or _all_ints(v)}
    return counts or None


def from_log(log: Path, start: int) -> dict | None:
    """The counts a scheduled step printed, read back from the part of its log this run appended.

    A step's CLI prints its result as one JSON object on stdout, which ``automations.run_task`` streams
    straight into the task's log, so the numbers are already there as text. This reads the last JSON object
    in ``log`` from byte ``start`` on and puts it through :func:`numeric` — the printed object is a whole
    result (run id, session date, nested sweep statistics, sometimes a provider name and no counts at all),
    not a counts dict, so taking it unfiltered would fill the column with text.

    ``start`` is the log's size before the command ran, so an earlier run's result object in the same
    appended-to log is never recorded as this run's. Never raises: the run log is bookkeeping, and this is
    read after the command's exit code is already decided. A result line longer than ``TAIL_BYTES`` has its
    head cut, fails the ``{`` check and yields ``None``, as does a log with no JSON — the rule above.
    """
    try:
        size = log.stat().st_size
        with open(log, "rb") as fh:
            fh.seek(max(start, size - TAIL_BYTES))      # never before ``start``: not an earlier run's output
            text = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        counts = numeric(obj)
        if counts:
            return counts
    return None
