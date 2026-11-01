"""CLI entry point: python -m RevenueChaser.module_spx_gamma --asof YYYY-MM-DD [--mode volume] [--ticker SPX].

``--nightly`` (#291, the RC-SPX-Gamma-Fetch task) fetches the latest complete New York session instead, and only once
the market store holds that session's SPX close: a complete raw day is never fetched again, so a day fetched before
the close is stored would keep a stale spot. It prints one JSON line (the session, the status, the UW call count) and
exits ``EXIT_NOT_READY`` without a UW call when the close is not stored yet; ``run``'s backfill picks that day up on a
later run. The run's own backfill fills any other missing session of the last two weeks, all expiries included.
"""
from __future__ import annotations

import argparse
import json

from RevenueChaser.core.calendar import latest_complete_session
from RevenueChaser.core.providers.unusual_whales import UWClient

from .config import TICKER
from .run import run
from .snapshot import stored_spx_candles

EXIT_NOT_READY = 3


def _nightly(ticker: str) -> int:
    asof = latest_complete_session().isoformat()
    if not any(r.get("date") == asof and r.get("close") is not None for r in stored_spx_candles(asof, ticker=ticker)):
        print(json.dumps({"asof": asof, "status": "not_ready",
                          "reason": f"no stored {ticker} close for {asof}; nothing was requested"}))
        return EXIT_NOT_READY
    uw = UWClient()
    run(asof, uw=uw, ticker=ticker)
    print(json.dumps({"asof": asof, "status": "done", "uw": uw.health()}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SPX Critical Thresholds (Unusual Whales)")
    when = parser.add_mutually_exclusive_group(required=True)
    when.add_argument("--asof", help="report as-of date (YYYY-MM-DD)")
    when.add_argument("--nightly", action="store_true", help="the latest complete New York session (#291)")
    parser.add_argument("--mode", default="open_interest",
                        choices=["open_interest", "directionalized_volume"],
                        help="spot-exposure mode (default open_interest)")
    parser.add_argument("--ticker", default=TICKER, help="ticker symbol (default SPX)")
    args = parser.parse_args(argv)

    if args.nightly:
        return _nightly(args.ticker)

    result = run(args.asof, mode=args.mode, ticker=args.ticker)
    print(f"report.md:    {result['paths']['report_md']}")
    print(f"report.html:  {result['paths']['report_html']}")
    print(f"position_map: {result['paths']['chart_html']}")
    q = result["snapshot"]["quality"]
    print(f"quality:      {q}")
    print(f"charm:        {result['snapshot']['charm_qualification']['label']} "
          f"@{result['snapshot']['charm_qualification']['strike']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
