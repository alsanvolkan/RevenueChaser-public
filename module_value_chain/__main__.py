"""Value Chain CLI.

    python -m RevenueChaser.module_value_chain --sync-filings [--limit N] [--force] [--floor M | --tickers A,B]
    python -m RevenueChaser.module_value_chain --fetch [--limit N] [--floor M | --tickers A,B]
    python -m RevenueChaser.module_value_chain --reslice-dry-run \\
        --edgar PATH --docs DIR [--out DIR]
    python -m RevenueChaser.module_value_chain --reslice-apply [--out DIR]

``--floor`` (USD millions) and ``--tickers`` choose the names (#108 S0). Without either, both commands cover the
$300M classification universe as before, and the sync's submissions sweep covers every universe CIK (#299); with
either, the sweep covers the chosen names' CIKs only. WARNING: either command with a selection makes SEC requests
for those names (``--floor 50`` adds about 900 filers) and rebuilds the Value Chain edges; run it only with the
user's typed words, never under the standing permission.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config, engine, reslice, sync


def _print_summary(report: dict) -> None:
    """Print the counts and the edge totals only (a whole report could break a cp1252 pipe)."""
    edges = report.get("edges") or {}
    totals = {k: v for k, v in edges.items() if isinstance(v, int)}
    print(json.dumps({"counts": report.get("counts"), "total": report.get("total"),
                      "edges": totals}, ensure_ascii=False))


def _floor(value: str) -> float:
    """A market-cap floor in USD millions; above 0 (the universe at 0 would also keep the names with no cap)."""
    floor = float(value)
    if not floor > 0:
        raise argparse.ArgumentTypeError("--floor is USD millions and must be above 0")
    return floor


def _selection(args) -> list[str] | None:
    """The names ``--floor`` or ``--tickers`` choose; None keeps today's default (the $300M universe)."""
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.floor is not None:
        from RevenueChaser.core.universe import classification_universe
        return classification_universe(args.floor * 1_000_000)
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Value Chain corpus + new-filing sync")
    p.add_argument("--sync-filings", action="store_true",
                   help="detect newer 10-K/20-F and refresh the raw doc + corpus")
    p.add_argument("--fetch", action="store_true",
                   help="fetch the corpus for the (classification) universe")
    p.add_argument("--limit", type=int, default=None, help="cap tickers")
    p.add_argument("--force", action="store_true",
                   help="re-check recorded misses and re-slice stored filings from their stored "
                        "documents (no download)")
    reslice_pick = p.add_mutually_exclusive_group()
    reslice_pick.add_argument("--reslice-dry-run", action="store_true",
                              help="re-slice the stored corpus offline and report the change (writes nothing)")
    reslice_pick.add_argument("--reslice-apply", action="store_true",
                              help="re-slice stored filings from their stored documents and write them through "
                                   "the slice guard, then rebuild edges (no network; holds the edgar_sync lock)")
    p.add_argument("--edgar", default=None, help="path to edgar.duckdb (--reslice-dry-run)")
    p.add_argument("--docs", default=None, help="path to the edgar_docs/ tree (--reslice-dry-run)")
    p.add_argument("--out", default=str(config.OUTPUT_DIR / "research" / "185"),
                   help="report directory (--reslice-dry-run / --reslice-apply)")
    pick = p.add_mutually_exclusive_group()
    warn = " Makes SEC requests for these names and rebuilds the Value Chain edges."
    pick.add_argument("--floor", type=_floor, default=None, metavar="M",
                      help="operating names with a market cap >= M USD millions (default: the $300M universe)."
                           + warn)
    pick.add_argument("--tickers", default=None, metavar="A,B",
                      help="exactly these tickers, comma-separated." + warn)
    args = p.parse_args(argv)
    # The re-slice commands run before the selection, which (with --floor) would read the live store
    # through rawstore; the apply also refuses the flags that drive a fetch or a selection.
    if args.reslice_apply:
        if args.fetch or args.sync_filings or args.floor is not None or args.tickers:
            p.error("--reslice-apply cannot be combined with --fetch, --sync-filings, --floor or --tickers")
        result = reslice.apply(out_dir=Path(args.out))
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("locked") else 0
    # The dry run reads the store copy it is given and nothing else.
    if args.reslice_dry_run:
        if not (args.edgar and args.docs):
            p.error("--reslice-dry-run needs --edgar and --docs")
        report = reslice.dry_run(Path(args.edgar), Path(args.docs), Path(args.out))
        _print_summary(report)
        return 0
    chosen = _selection(args)
    if chosen == []:      # an empty list would read as "no selection" downstream: the whole universe
        p.error("--floor / --tickers selected no names")

    if args.sync_filings:
        result = sync.sync_filings(tickers=chosen, limit=args.limit, force=args.force)
        print(json.dumps(result, ensure_ascii=False))
        # A scheduled run that was locked out, lost tickers or lost submissions (#299) must not read as green.
        subs = result.get("submissions") or {}
        if result.get("locked") or result.get("failed") or subs.get("error") or subs.get("failed"):
            return 1
    elif args.fetch:
        print(json.dumps(engine.fetch_corpus(tickers=chosen, limit=args.limit), ensure_ascii=False))
    else:
        print("use --sync-filings or --fetch")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
