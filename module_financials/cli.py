"""CLI entry point: python -m RevenueChaser.module_financials --ticker AAPL --modules dcf,3stmt,comps."""
from __future__ import annotations

import argparse

from .run import run

MODULES = ("dcf", "3stmt", "comps", "credit", "sensitivity")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fin Research - ticker-based financial analysis")
    parser.add_argument("--ticker", required=True, help="ticker to analyze (e.g. AAPL)")
    parser.add_argument("--modules", default="dcf",
                        help=f"comma-separated modules: {','.join(MODULES)} (default: dcf)")
    parser.add_argument("--output", default="html", choices=["html", "none"],
                        help="output format (default: html)")
    parser.add_argument("--peers", default=None,
                        help="comma-separated peer list for comps (default: by sector)")
    parser.add_argument("--no-cache", action="store_true", help="skip cache, fetch fresh data")
    args = parser.parse_args()

    modules = [m for m in args.modules.split(",") if m.strip()]
    peers = [p.strip().upper() for p in args.peers.split(",") if p.strip()] if args.peers else None

    result = run(args.ticker, modules=modules, output=args.output,
                 use_cache=not args.no_cache, peers=peers)

    if "html" in result["paths"]:
        print(f"report.html: {result['paths']['html']}")
    qr = result["results"].get("quick_remarks")
    if qr:
        print(f"quick remarks: {qr.get('score')}/{qr.get('max')} (source: {qr.get('source')})")
    for name, res in result["results"]["modules"].items():
        if isinstance(res, dict):
            if "error" in res:
                print(f"{name}: ERROR - {res['error']}")
            elif "valuation" in res:
                v = res["valuation"]
                print(f"{name}: per share Gordon={v.get('per_share_gordon')} Exit={v.get('per_share_exit')}")


if __name__ == "__main__":
    main()
