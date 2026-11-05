"""#136: a run records how much it did, for the Runs tab's ``counts`` column.

Two writers, two gaps, and this suite covers both.

**The scheduled writer** (``core/automations.run_task``) wrote a literal ``"counts": None`` — the only
occurrence of ``counts`` in that file — so no scheduled step could record counts whatever it printed. The
step's CLI already prints one JSON result object on stdout, which ``run_task`` streams into the task's log,
so the counts are there to be read: the row now takes the last JSON object the command printed.

**The dashboard writer** (``app/jobs.py:_counts_of``) stores a runner's returned ``counts``, and
``run_spxgamma_history`` proves it. But the long writers the issue names (identity, daily bars) never
return: they end by raising ``JobMessage`` so the UI shows what happened, and the ``except JobMessage``
branch never asked for counts. ``JobMessage`` now carries them, which is what makes adoption a one-line
change per runner instead of a return-type change that would break every caller using the returned path.

Offline; a log file in ``tmp_path`` and the temp store.
"""
from __future__ import annotations

import dataclasses
import json

from RevenueChaser.app import jobs
from RevenueChaser.core import automations, rawstore

OK_JSON = '{"rows": 93714, "written": 93714, "status": {"open": 93382, "closed": 332}}'


def _row(name: str) -> dict | None:
    rows = [r for r in rawstore.get_all("job_runs") if r.get("module") == name]
    return rows[-1] if rows else None


def test_a_scheduled_step_records_the_last_json_object_it_printed(rawstore_tmp_db, tmp_path, monkeypatch):
    name = automations.REGISTRY[0].name
    script = tmp_path / "step.py"
    script.write_text(f"print('working')\nprint({OK_JSON!r})\n", encoding="utf-8")
    _as_task(monkeypatch, tmp_path, name, script)

    assert automations.run_task(name) == 0

    assert _row(name)["counts"] == json.loads(OK_JSON)


def test_a_step_that_prints_no_json_records_no_counts_rather_than_an_invention(
        rawstore_tmp_db, tmp_path, monkeypatch):
    name = automations.REGISTRY[0].name
    script = tmp_path / "step.py"
    script.write_text("print('done, nothing to report')\n", encoding="utf-8")
    _as_task(monkeypatch, tmp_path, name, script)

    assert automations.run_task(name) == 0

    assert _row(name)["counts"] is None


def test_only_this_runs_output_is_read_not_an_earlier_runs_json_in_the_same_log(
        rawstore_tmp_db, tmp_path, monkeypatch):
    """The log is appended to, so a previous run's result object must not be recorded as this run's."""
    name = automations.REGISTRY[0].name
    first, second = tmp_path / "a.py", tmp_path / "b.py"
    first.write_text(f"print({OK_JSON!r})\n", encoding="utf-8")
    second.write_text("print('no numbers this time')\n", encoding="utf-8")

    _as_task(monkeypatch, tmp_path, name, first)
    assert automations.run_task(name) == 0
    _as_task(monkeypatch, tmp_path, name, second)
    assert automations.run_task(name) == 0

    assert _row(name)["counts"] is None


def test_a_job_message_carries_counts_so_a_runner_that_never_returns_can_record_them():
    """Identity and daily bars end with ``JobMessage``; without this they could record nothing."""
    counts = {"tickers": 7, "written": 7}

    assert jobs.JobMessage("done", counts=counts).counts == counts
    assert jobs.JobMessage("done").counts is None


def _as_task(monkeypatch, tmp_path, name: str, script) -> None:
    """Point the registry entry at ``script``, with its log under ``tmp_path``, and make it a table writer so it
    records a row at all (an entry with no ``tables_written`` records nothing, by design). The entry is a frozen
    dataclass, so this replaces it rather than assigning to it, and ``config.OUTPUT_DIR`` moves the log."""
    from RevenueChaser.core import config
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    replaced = dataclasses.replace(automations.entry(name), args=("-I", str(script)),
                                   tables_written=("job_runs",), after=None)
    monkeypatch.setattr(automations, "entry", lambda n: replaced)


def test_only_a_results_own_integer_fields_become_counts():
    """A step's result mixes counts with diagnostics it also returns; the column takes the numbers only."""
    from RevenueChaser.app import runners

    res = {"scope": 7, "rows_inserted": 93714, "latest_session": "2026-11-04", "ratio": 0.5,
           "locked": False, "overlap": {"rows": 3}}

    assert runners.numeric_counts(res) == {"scope": 7, "rows_inserted": 93714}
    assert runners.numeric_counts({"latest_session": "2026-11-04"}) is None
    assert runners.numeric_counts(None) is None
