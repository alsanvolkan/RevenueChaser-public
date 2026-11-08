"""CLI entry point: python -m RevenueChaser.module_spx_gamma --asof YYYY-MM-DD [--mode volume] [--ticker SPX].

``--nightly`` (#291, the RC-SPX-Gamma-Fetch task) fetches the latest complete New York session instead, SPX only, and
only once the market store holds that session's SPX close and outside the US session: a complete raw day is never
fetched again, so a day fetched before its close is stored would keep a stale spot, and one fetched mid-session (a
missed run caught up) would keep intraday answers. Either case exits ``EXIT_NOT_READY`` without a UW call. It prints
one JSON line: the session, the day actually stored (``effective``; the mandatory-endpoint fallback can store the
previous one), the status and the UW call count. ``run``'s own backfill still fills a missing session of the last two
weeks a day or more late, as a board Fetch does: its dated calls are that session's, its undated ones (the live
files, ``as_of: false`` in the manifest) the later day's.
"""
from __future__ import annotations

import argparse
import json

from RevenueChaser.core.calendar import in_us_session_window, latest_complete_session
from RevenueChaser.core.providers.unusual_whales import UWClient

from .config import TICKER
from .run import run
from .snapshot import stored_spx_candles

EXIT_NOT_READY = 3


def _nightly(ticker: str) -> int:
    asof = latest_complete_session().isoformat()
    if in_us_session_window():
        print(json.dumps({"asof": asof, "status": "in_session",
                          "reason": "inside the US session; nothing was requested"}))
        return EXIT_NOT_READY
    if not any(r.get("date") == asof and r.get("close") is not None for r in stored_spx_candles(asof, ticker=ticker)):
        print(json.dumps({"asof": asof, "status": "not_ready",
                          "reason": f"no stored {ticker} close for {asof}; nothing was requested"}))
        return EXIT_NOT_READY
    uw = UWClient()
    result = run(asof, uw=uw, ticker=ticker)
    print(json.dumps({"asof": asof, "effective": result["latest"]["asof_date"], "status": "done",
                      "uw": uw.health()}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SPX Critical Thresholds (Unusual Whales)")
    when = parser.add_mutually_exclusive_group(required=True)
    when.add_argument("--asof", help="report as-of date (YYYY-MM-DD)")
    when.add_argument("--nightly", action="store_true", help="the latest complete New York session (#291)")
    when.add_argument("--migrate-data-root", action="store_true",
                      help="S4: copy the tree out of the checkout into the store root (dry run without --apply)")
    parser.add_argument("--apply", action="store_true", help="with --migrate-data-root: copy (never deletes)")
    parser.add_argument("--mode", default="open_interest",
                        choices=["open_interest", "directionalized_volume"],
                        help="spot-exposure mode (default open_interest)")
    parser.add_argument("--ticker", default=TICKER, help="ticker symbol (default SPX)")
    args = parser.parse_args(argv)

    if args.migrate_data_root:
        from .config import LEGACY_DATA_ROOT, STORE_DATA_ROOT
        from .data_root_move import migrate
        out = migrate(LEGACY_DATA_ROOT, STORE_DATA_ROOT, apply=args.apply)
        print(json.dumps(out))
        return 0 if out["status"] in ("dry_run", "copied", "target_exists", "nothing_to_copy") else 1

    if args.nightly:
        if args.ticker != TICKER:
            parser.error("--nightly is SPX only: its close guard reads the stored SPX series")
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
