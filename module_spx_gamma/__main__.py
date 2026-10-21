"""CLI entry point: python -m RevenueChaser.module_spx_gamma --asof YYYY-MM-DD [--mode volume] [--ticker SPX]."""
from __future__ import annotations

import argparse

from .config import TICKER
from .run import run


def main() -> None:
    parser = argparse.ArgumentParser(description="SPX Critical Thresholds (Unusual Whales)")
    parser.add_argument("--asof", required=True, help="report as-of date (YYYY-MM-DD)")
    parser.add_argument("--mode", default="open_interest",
                        choices=["open_interest", "directionalized_volume"],
                        help="spot-exposure mode (default open_interest)")
    parser.add_argument("--ticker", default=TICKER, help="ticker symbol (default SPX)")
    args = parser.parse_args()

    result = run(args.asof, mode=args.mode, ticker=args.ticker)
    print(f"report.md:    {result['paths']['report_md']}")
    print(f"report.html:  {result['paths']['report_html']}")
    print(f"position_map: {result['paths']['chart_html']}")
    q = result["snapshot"]["quality"]
    print(f"quality:      {q}")
    print(f"charm:        {result['snapshot']['charm_qualification']['label']} "
          f"@{result['snapshot']['charm_qualification']['strike']}")


if __name__ == "__main__":
    main()
