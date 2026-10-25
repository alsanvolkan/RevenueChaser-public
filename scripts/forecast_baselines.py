"""Forecast baselines CLI (#25 HR-1): the ``RC-Forecast-Baselines`` nightly and the attended first fill.

``--nightly`` recomputes only the baseline rows still open (chained on RC-Forecast-Outcomes, so the two raw.duckdb
writers of the night do not overlap); ``--full`` recomputes every date (the first fill, or after a rule change).
``--dry-run`` computes and counts without writing. Reads ``forecast_ledger``, the equity-universe change history and
the stored bars, writes ``forecast_baselines`` only (``core/baselines.py``), no network call.

One writer at a time (``job_lock`` name ``"forecast_baselines"``, #26): a run that holds the lock makes this exit
``BUSY_EXIT`` with one line on stderr and no table write; ``--report`` still gets a ``{"status": "busy"}`` record, so
yesterday's success JSON cannot be read as tonight's (S-3). ``--dry-run`` writes nothing, so it takes no lock and
still answers while the task runs.
"""
from __future__ import annotations

import sys

from RevenueChaser.core import baselines
from RevenueChaser.scripts import forecast_cli
from RevenueChaser.scripts.forecast_cli import BUSY_EXIT  # noqa: F401 - the task's documented busy exit code

# The job_lock name, as a constant so the lock-name guard (core/tests/test_job_lock_names_pl6a.py) sees it.
LOCK = "forecast_baselines"
CLI = forecast_cli.WriterCli(
    prog="forecast_baselines", what="forecast baselines", lock=LOCK,
    run=lambda **kw: baselines.run(**kw),
    description=__doc__, nightly_help="recompute the rows that are not closed yet", full_help="recompute every date")


def main(argv: list[str] | None = None) -> int:
    return forecast_cli.main(CLI, argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
