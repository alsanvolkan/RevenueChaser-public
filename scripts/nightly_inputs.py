"""Nightly inputs CLI (C25a/K11, #383): run one inputs step of ``core/nightly_inputs.py``.

``python -m RevenueChaser.scripts.nightly_inputs <step> --max-uw-calls N --max-fred-calls N --max-yf-calls N
--max-cboe-calls N --max-cftc-calls N``

A registry entry runs it (``core/automations.py`` checks the entry: after RC-Daily-Bars, every cap stated). A cap
left out is 0 for ``uw``, ``fred`` and ``yfinance``: that provider gets no call. ``cboe`` and ``cftc`` default to
their runaway guard, 20 and 5. Prints the result as one JSON line on stdout and a summary on stderr; exit 0 when every
sweep finished, 1 when one failed or the run had no session, 2 for a bad command line, ``BUSY_EXIT`` (3) when another
run of this step holds the lock (``job_lock`` name ``"nightly_inputs_<step>"``, #26) — no table was written then, and
``--report`` gets a ``{"status": "busy"}`` record over the last success (S-3). The lock is per step, because the steps
write different tables: two different steps may run together.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from RevenueChaser.core import job_lock, nightly_inputs

BUSY_EXIT = 3


def _write_report(path: str | None, res: dict) -> None:
    """Write ``res`` to ``--report``: the result of a run, or the ``busy`` record of one that wrote nothing (S-3)."""
    if path:
        Path(path).write_text(json.dumps(res, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nightly_inputs", description=__doc__)
    ap.add_argument("step", help="the inputs step to run")
    ap.add_argument("--max-uw-calls", type=int, default=0, help="Unusual Whales calls allowed tonight")
    ap.add_argument("--max-fred-calls", type=int, default=0, help="FRED calls allowed tonight")
    ap.add_argument("--max-yf-calls", type=int, default=0, help="yfinance requests allowed tonight (read only)")
    ap.add_argument("--max-cboe-calls", type=int, default=20, help="Cboe index-file calls allowed tonight")
    ap.add_argument("--max-cftc-calls", type=int, default=5, help="CFTC requests allowed tonight")
    ap.add_argument("--report", default=None, help="write the result JSON to this path")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    caps = {"uw": args.max_uw_calls, "fred": args.max_fred_calls, "yfinance": args.max_yf_calls,
            "cboe": args.max_cboe_calls, "cftc": args.max_cftc_calls}
    if any(v < 0 for v in caps.values()):
        print("a call cap cannot be negative", file=sys.stderr)
        return 2
    if args.step not in nightly_inputs.STEPS:
        known = ", ".join(sorted(nightly_inputs.STEPS)) or "none"
        print(f"unknown step {args.step!r} (known: {known})", file=sys.stderr)
        return 2

    name = f"nightly_inputs_{args.step}"
    with job_lock.acquire(name, wait=0) as got:     # as late as possible: the command line is valid here
        if not got:                                 # the same step runs already: nothing was written
            pid = dict(job_lock.holders()).get(name, 0)
            who = f" (pid {pid})" if pid else ""
            print(f"nightly inputs {args.step}: another run{who} holds {name}; nothing was written.",
                  file=sys.stderr)
            # Over whatever the last success left there: a reader must not read last night's JSON as tonight's (S-3).
            _write_report(args.report, {"status": "busy", "holder_pid": pid})
            return BUSY_EXIT
        res = nightly_inputs.run_step(args.step, nightly_inputs.load_step(args.step), caps=caps)
    _write_report(args.report, res)
    print(json.dumps(res, default=str))
    print(nightly_inputs.summary_line(res), file=sys.stderr)
    return nightly_inputs.exit_code(res)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
