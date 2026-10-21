"""Daily price bars CLI (#156): the ``RC-Daily-Bars`` nightly and the attended max fills.

``--nightly`` writes each scope ticker's bars up to the latest complete New York session
(UW ``ohlc/1d`` with a yfinance fallback). ``--fill --confirm`` is the one-off attended
``period="max"`` history fill. Two entry points, one lock, one store writer
(``core/prices.py``); the scheduled task runs ``--nightly --sec-files --index-levels``.

``--sec-files`` (#301) adds a best-effort first step to a whole-scope ``--nightly``: the SEC
exchange and fund files (``core/sec_files.py``, at most 2 SEC requests a weekday night). Its
result rides in the JSON line under ``sec_files``; it never changes the exit code, because
the steps chained on RC-Daily-Bars wait on that exit 0.

``--index-levels`` (D03, #293, M-SPX-0 #376) adds a best-effort last step to a whole-scope
``--nightly``, after the bars (also after a failed UW key check, since it needs none): the
``^GSPC`` daily levels into ``index_levels_daily`` (``core/index_levels.py``, at most 1
yfinance request a night, none when current or before the fill). Its result rides under
``index_levels``; it never changes the exit code either. ``--index-fill --confirm`` is the
attended one-time ``period="max"`` fill of those levels: one yfinance request, no UW client,
no bars; it exits 1 when it fails.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from RevenueChaser.core import index_levels, ledger, prices


# The dashboard route's symbol rule (app/dashboard.py ``_TICKER_RE``); a leading ``^`` index level
# passes because ``prices._norm`` drops it. CL=F / EURUSD=X style symbols stop here (#156 N13).
_SYMBOL_RE = re.compile(r"\^?[A-Z0-9][A-Z0-9.\-]{0,9}")


def _split_tickers(text: str | None) -> list[str]:
    return [p.strip().upper() for p in (text or "").split(",") if p.strip()]


def _tickers(args) -> list[str] | None:
    """The asked tickers, or None for the whole scope. An unreadable --tickers-file raises
    OSError: falling back to the whole scope would turn a 10-name fill into a universe fill."""
    out = _split_tickers(args.tickers)
    if args.tickers_file:
        text = Path(args.tickers_file).read_text(encoding="utf-8")
        out += _split_tickers(text.replace("\n", ","))
    bad = [t for t in out if not _SYMBOL_RE.fullmatch(t)]
    if bad:
        raise ValueError(f"not an equity symbol: {', '.join(bad)}")
    deduped = list(dict.fromkeys(out))
    return deduped or None


def _sec_files_step(run_id: str) -> dict:
    """The #301 step. Never raises: a failure is its ``failed`` result, and the bars still run."""
    try:
        from RevenueChaser.core import sec_files

        res = sec_files.nightly(run_id=run_id)
        line = sec_files.summary_line(res)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        res = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
        line = f"SEC files: failed ({res['error']})"
    print(line, file=sys.stderr, flush=True)
    return res


def _index_levels_step(run_id: str) -> dict:
    """The D03 step (#293). Never raises: a failure is its ``failed`` result; the exit code stays the bars'."""
    try:
        res = index_levels.nightly(run_id=run_id)
        line = index_levels.summary_line(res)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        res = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
        line = f"Index levels: failed ({res['error']})"
    print(line, file=sys.stderr, flush=True)
    return res


def _index_fill(args, tickers) -> int:
    """``--index-fill --confirm``: the one-time ``^GSPC`` fill, alone (no bars, no SEC files, no UW client)."""
    other = [flag for flag, on in (("--tickers/--tickers-file", tickers is not None),
                                   ("--sec-files", args.sec_files), ("--index-levels", args.index_levels),
                                   ("--no-fetch", args.no_fetch), ("--max-uw-calls", args.max_uw_calls is not None))
             if on]
    if other:
        print(f"refusing: --index-fill runs alone (got {', '.join(other)})", file=sys.stderr)
        return 2
    if not args.confirm:
        print("refusing: --index-fill writes the store; pass --confirm", file=sys.stderr)
        return 2
    res = index_levels.fill(run_id=ledger.new_run_id())
    if args.report:
        Path(args.report).write_text(json.dumps(res, default=str), encoding="utf-8")
    print(json.dumps(res, default=str))
    print(index_levels.summary_line(res), file=sys.stderr)
    return 0 if res.get("status") in ("ok", "skipped") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="daily_bars", description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--nightly", action="store_true", help="write the latest complete session")
    mode.add_argument("--fill", action="store_true", help="attended period=max history fill")
    mode.add_argument("--index-fill", action="store_true",
                      help="attended one-time period=max fill of the ^GSPC daily levels (#293); needs --confirm")
    ap.add_argument("--tickers", default=None, help="comma list of tickers")
    ap.add_argument("--tickers-file", default=None, help="a file of tickers (newline or comma)")
    ap.add_argument("--no-fetch", action="store_true", help="nightly: count only, no calls, no writes")
    ap.add_argument("--max-uw-calls", type=int, default=None, help="nightly UW call cap")
    ap.add_argument("--sec-files", action="store_true",
                    help="nightly, whole scope: first store the SEC exchange and fund files (#301, best effort)")
    ap.add_argument("--index-levels", action="store_true",
                    help="nightly, whole scope: last, store the ^GSPC daily levels (#293, best effort, "
                         "at most 1 yfinance request a night)")
    ap.add_argument("--confirm", action="store_true", help="--fill and --index-fill need this to touch the store")
    ap.add_argument("--detail", action="store_true", help="--fill: per-ticker rows and first date")
    ap.add_argument("--report", default=None, help="write the result JSON to this path")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    try:
        tickers = _tickers(args)
    except OSError as exc:
        print(f"could not read --tickers-file: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.index_fill:
        return _index_fill(args, tickers)

    if args.fill and args.sec_files:
        print("refusing: --sec-files is a --nightly step", file=sys.stderr)
        return 2
    if args.fill and args.index_levels:
        print("refusing: --index-levels is a --nightly step", file=sys.stderr)
        return 2

    sec_res = idx_res = None
    if args.fill:
        if not args.confirm:
            print("refusing: --fill writes the store; pass --confirm", file=sys.stderr)
            return 2
        res = prices.fill_history(tickers, run_id=ledger.new_run_id(), detail=bool(args.detail))
    else:
        from RevenueChaser.core.providers.unusual_whales import UWClient

        run_id = ledger.new_run_id()
        whole_night = tickers is None and not args.no_fetch
        if args.sec_files and whole_night:
            sec_res = _sec_files_step(run_id)
        uw = UWClient()
        try:
            ok, message = uw.verify()
            if not ok:
                print(message or "Unusual Whales key is not usable", file=sys.stderr)
                res = None
            else:
                kwargs: dict = {}
                if args.max_uw_calls is not None:
                    kwargs["max_uw_calls"] = args.max_uw_calls
                res = prices.ensure_daily_bars(tickers, uw=uw, run_id=run_id,
                                               no_fetch=bool(args.no_fetch), **kwargs)
        finally:
            getattr(uw, "close", lambda: None)()
        if args.index_levels and whole_night:
            idx_res = _index_levels_step(run_id)
        if res is None:
            return 1
        if sec_res is not None:
            res = {**res, "sec_files": sec_res}
        if idx_res is not None:
            res = {**res, "index_levels": idx_res}

    if args.report:
        Path(args.report).write_text(json.dumps(res, default=str), encoding="utf-8")
    print(json.dumps(res, default=str))
    print(prices.summary_line(res), file=sys.stderr)
    if res.get("locked") or prices.run_ok(res):
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
