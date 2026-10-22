"""RevenueChaser — UW-first earnings decision engine."""
import logging, json, sys
from datetime import date, timedelta
from collections import defaultdict
from pathlib import Path

from .core.config import UW_API_KEY
from RevenueChaser.core import rawstore
from RevenueChaser.core.providers.unusual_whales import UWClient
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline, nrc_from_card
from RevenueChaser.module_earnings_radar.terminal import render_dashboard
from RevenueChaser.module_earnings_radar.report import export_html, open_in_browser
from RevenueChaser.module_earnings_radar.rules.nrc import export_nrc_html
from RevenueChaser.module_earnings_radar.watchlist_augment import augment_calendar, _augment_watchlist  # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RevenueChaser")


def build_weeks(n: int = 12) -> list[tuple[date, date, int]]:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return [(monday + timedelta(weeks=i), monday + timedelta(weeks=i, days=6),
             (monday + timedelta(weeks=i)).isocalendar()[1]) for i in range(n)]


def run_unit_tests() -> int:
    """Run the co-located pytest suite and return the exit code."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    package_dir = Path(__file__).resolve().parent  # RevenueChaser/ (co-located tests)

    print(f"\n  Running unit tests: {package_dir}")
    print("  " + "=" * 60)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(package_dir), "-q"],
        cwd=str(root),
    )
    if result.returncode == 0:
        print("\n  All unit tests passed.\n")
    else:
        print(f"\n  Unit tests FAILED (exit code {result.returncode}).\n")
    return result.returncode


def main():
    if "--test" in sys.argv:
        sys.exit(run_unit_tests())

    uw = UWClient()
    if not uw.active:
        print("UW API key not set.")
        return

    print("\n  REVENUECHASER v1 — UW Powered")
    print("  [1] Earnings Radar")
    print("  [2] Rotation Map (Sector/Theme RRG)")
    print("  [3] Run Unit Tests")
    mode = input("\n  Select mode [1/2/3]: ").strip()

    if mode == "3":
        run_unit_tests()
        return

    if mode == "2":
        from RevenueChaser.module_sector_rotation.__main__ import run_rotation_map
        run_rotation_map()
        return

    weeks = build_weeks(12)

    print(f"\n  {'=' * 50}")
    print(f"  EARNINGS CALENDAR — Next 12 Weeks")
    print(f"  {'=' * 50}")
    for i, (m, s, wn) in enumerate(weeks):
        print(f"  [{i + 1}] Week {wn}  {m.strftime('%b %d')} – {s.strftime('%b %d')}")
    print(f"  [C] Custom range")
    print()

    choice = input(f"  Pick week [1-{len(weeks)}/C]: ").strip().upper()
    if choice == "C":
        s = input("  Start (YYYY-MM-DD): ").strip()
        e = input("  End   (YYYY-MM-DD): ").strip()
        start, end = date.fromisoformat(s), date.fromisoformat(e)
    elif choice.isdigit() and 1 <= int(choice) <= len(weeks):
        start, end, _ = weeks[int(choice) - 1]
    else:
        start, end, _ = weeks[0]

    print(f"\n  Fetching UW earnings {start} – {end}...")
    events = []
    raw_cal = {}
    d = start
    while d <= end:
        if d.weekday() < 5:
            for session in ("premarket", "afterhours"):
                for item in uw.calendar_day_raw(d, session):
                    t = item.get("symbol") or item.get("ticker", "")
                    if t:
                        time_label = "BMO" if session == "premarket" else "AMC"
                        ev = {
                            "ticker": t,
                            "earnings_date": d,
                            "earnings_time": time_label,
                            "sector": item.get("sector", ""),
                            "full_name": item.get("full_name", ""),
                            "marketcap": item.get("marketcap", ""),
                            "expected_move_perc": item.get("expected_move_perc"),
                            "street_mean_est": item.get("street_mean_est"),
                            "pre_earnings_close": item.get("pre_earnings_close"),
                            "post_earnings_close": item.get("post_earnings_close"),
                            "report_time": item.get("report_time", ""),
                            "source": "UW",
                        }
                        events.append(ev)
                        if t not in raw_cal or session == "premarket":
                            raw_cal[t] = ev
        d += timedelta(days=1)

    # Augment with watchlist tickers reporting in-range but missing from the
    # session calendar (e.g. "unknown" earnings time / estimated dates).
    augment_calendar(uw, events, raw_cal, rawstore.load_watchlist(), start, end)

    print(f"  Found {len(events)} events.")
    if not events: return

    by_day = defaultdict(list)
    for e in events: by_day[e["earnings_date"]].append(e)
    days = sorted(by_day.keys())

    print(f"\n  {'=' * 60}")
    for i, d in enumerate(days):
        ts = ", ".join(e["ticker"] for e in by_day[d][:15])
        more = f" +{len(by_day[d]) - 15}" if len(by_day[d]) > 15 else ""
        print(f"  [{i + 1}] {d.strftime('%a %b %d')} ({len(by_day[d])}) — {ts}{more}")
    print(f"  [A] All ({len(events)})")
    print()

    choice = input("  Pick day [1-{}/A]: ".format(len(days))).strip().upper()
    selected = events if choice == "A" else (by_day[days[int(choice) - 1]] if choice.isdigit() and 1 <= int(choice) <= len(days) else events)
    tickers = list(dict.fromkeys(e["ticker"] for e in selected))
    cal_meta = {e["ticker"]: e for e in selected}

    print(f"\n  {len(tickers)} tickers. Loading rotation data...")
    etf_quadrant_map = {}
    try:
        from RevenueChaser.module_sector_rotation.engine import rotation_quadrant_map
        etf_quadrant_map = rotation_quadrant_map(uw, compute=True)
        if etf_quadrant_map:
            print(f"  Rotation ready: {len(etf_quadrant_map)} ETFs classified (pipeline maps tickers by cluster).")
        else:
            print("  Rotation data unavailable — continuing without it.")
    except Exception as e:
        logger.warning(f"Rotation engine failed: {e}")

    print(f"  Running analysis...")
    pipeline = EarningsPipeline(tickers, uw, raw_cal, etf_quadrant_map=etf_quadrant_map)
    cards = pipeline.run(use_preselect=True)
    print(f"  Scored {len(cards)} events.")
    fs = pipeline._filter_stats
    if fs and fs.get("total", 0) > 0:
        print(f"  Preselect: {fs['total']} total → {fs.get('passed',0)+fs.get('bypassed',0)} scored "
              f"({fs.get('bypassed',0)} bypassed) · {fs.get('filtered',0)} filtered")
        for r, c in sorted(fs.get("by_reason", {}).items(), key=lambda x: -x[1]):
            print(f"    {r}: {c}")
    print()
    render_dashboard(cards)

    # Compute NRC from real card data (differentiated per ticker)
    wl = rawstore.load_watchlist()
    nrc_results = {card.ticker: nrc_from_card(card, wl) for card in cards}

    nrc_path = export_nrc_html(nrc_results, cards)
    html_path = export_html(cards, events, nrc_results, filter_stats=pipeline._filter_stats)
    print(f"  NRC: {nrc_path}  |  HTML: {html_path}")
    open_in_browser(html_path)

    # Show section counts
    for tier in ["Tier A — Revenue Catalyst", "Tier B — Watchlist", "Tier C — Emerging", "Ignore"]:
        count = sum(1 for v in nrc_results.values() if v["tier"] == tier)
        if count: print(f"    {tier}: {count} tickers")
    print("  Done.\n")


if __name__ == "__main__":
    main()
