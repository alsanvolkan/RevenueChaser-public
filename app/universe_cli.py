"""CLI entry point of the universe refresh (#244): ``python -m RevenueChaser.app.universe_cli``.

Runs ``runners.run_universe_refresh`` in this process, for when the dashboard is down (#129's scheduled path).
It takes the same ``universe`` lock as the dashboard job, so the two never run at once.

Exit codes:
- ``0``: the run finished (a board that scored nothing prints its reason and still exits 0);
- ``UNIVERSE_BUSY_EXIT`` (3): another universe run, or Phase B, holds the lock, or a single-board Fetch runs (#356);
  nothing was fetched or written;
- ``1``: the run failed.
"""
from __future__ import annotations

import logging
import sys

UNIVERSE_BUSY_EXIT = 3


def main(argv: list[str] | None = None) -> int:
    from RevenueChaser.app import runners
    from RevenueChaser.core import ledger

    ledger.take_notes()                   # notes are per thread; start clean
    try:
        runners.run_universe_refresh(None)
    except runners.UniverseBusy as exc:
        print(exc.message, file=sys.stderr)
        return UNIVERSE_BUSY_EXIT
    except runners.JobMessage as exc:
        print(exc.message)
    except Exception:  # noqa: BLE001 - the exit code is the outcome; the trace goes to the log
        logging.getLogger(__name__).exception("universe refresh failed")
        return 1
    finally:
        for note in ledger.take_notes():
            print(note)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main(sys.argv[1:]))
