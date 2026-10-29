"""Test helper for PR 10 (G4): run the queued ``ledger/freeze`` requests through the builder, as its hourly pass
would. A board's freeze only queues its picks; a test that wants them in ``ledger.duckdb`` drains the queue."""
from __future__ import annotations

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build import base


def drain(dataset: str = "ledger/freeze") -> list[dict]:
    """Run every queued ``dataset`` request; returns each request's check row."""
    rows = []
    while (req := ops.next_request(dataset)) is not None:
        rows.append(base.run_request(req))
    return rows


def stored(source: str | None = None) -> list[dict]:
    """Drain, then the ledger rows (of ``source`` when given), as the boards' readers see them."""
    from RevenueChaser.rcdata import read
    drain()
    return read.forecast_ledger(source=source)


def count() -> int:
    """Drain, then the number of ledger rows."""
    return len(stored())
