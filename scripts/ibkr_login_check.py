"""``RC-IBKR-Login-Check`` (#162): one read-only look at the IBKR Gateway's login state.

One probe, one reading folded into the record, one line printed. It writes **no table and no store
file** — only its own JSON record under ``.cache/`` — so it can never queue behind or hold the
dashboard's single DuckDB writer, and it needs no schema change. See ``core/ibkr_login.py`` for what
the probe does and, just as importantly, what it never asks for.

**It exits 0 whatever it finds.** A session that expired on its weekly cycle is not a failed job; a
non-zero exit would turn the scheduler row red for something nobody broke, and would be read a week
later as a breakage. The state is the output, printed and recorded; the exit code says only that the
check itself ran.

    python -m RevenueChaser.scripts.ibkr_login_check

Switched off until ``RC_IBKR_LOGIN_CHECK`` is set: with it unset the job probes nothing, writes
nothing and says so.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from RevenueChaser.core import ibkr_login

_WORDS = {ibkr_login.UP: "logged in", ibkr_login.DOWN: "login needed",
          ibkr_login.SILENT: "port open, the API is not answering"}


def main(argv: list[str] | None = None, *, probe=None, path: Path | None = None,
         now: datetime | None = None) -> int:
    """``probe``, ``path`` and ``now`` are the seams the tests use; the job itself passes none."""
    parser = argparse.ArgumentParser(prog="ibkr_login_check", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timeout", type=float, default=ibkr_login.TIMEOUT_SECONDS,
                        help="seconds to wait for the connection and the handshake reply")
    args = parser.parse_args(argv)

    host, port = ibkr_login.target()
    if not ibkr_login.enabled():
        print(f"IBKR login check: off (RC_IBKR_LOGIN_CHECK is not set); {host}:{port} was not contacted "
              "and nothing was recorded", file=sys.stderr)
        return 0

    probe = probe or ibkr_login.probe
    try:
        reading = probe(host, port, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - the probe is written not to raise; if it does, say so
        print(f"IBKR login check: the probe of {host}:{port} failed with "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 0

    state = ibkr_login.record(reading, path=path, now=now)
    held = ibkr_login.for_how_long(state["since"], now or datetime.now(timezone.utc))
    print(f"IBKR login check: {_WORDS.get(reading.state, reading.state)} — {reading.detail}"
          f"{f'; this state has held for {held}' if held else ''}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
