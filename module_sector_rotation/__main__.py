"""Rotation Map — standalone daily refresh. Run separately or from main menu."""
import logging
import webbrowser
from pathlib import Path

from RevenueChaser.core.providers.unusual_whales import UWClient
from .engine import RotationEngine, fetch_all_holdings, macro_panel, sector_etf_trees
from .report import export_rotation_dashboard

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RotationMap")


def run_rotation_map():
    print("\n  ROTATION MAP — Sector & Theme RRG")
    print("  Fetching ETF OHLC data vs SPY...\n")

    uw = UWClient()
    if not uw.active:
        print("  UW API key not set. Check .env file.")
        return None

    engine = RotationEngine(uw)
    snapshots = engine.process_all()

    if not snapshots:
        print("  No rotation data fetched. Check API connectivity.")
        return None

    print(f"  Processed {len(snapshots)} ETFs.")

    # Embed per-ticker holdings so the exported map is self-contained (no server needed).
    print("  Fetching ETF holdings RS (theme ETFs)...")
    holdings = fetch_all_holdings(uw, snapshots)
    print(f"  Holdings ready for {len(holdings)} ETFs.")

    # Sector rows expand to the sector -> industry tree (cap-weighted, from the
    # shared universe) instead of an ETF top-10 snapshot.
    sector_holdings = sector_etf_trees()
    html_path = export_rotation_dashboard(snapshots, holdings=holdings,
                                          sector_holdings=sector_holdings,
                                          macro_panel=macro_panel(snapshots[0].as_of_date))
    print(f"  Dashboard: {html_path}")

    webbrowser.open(f"file:///{html_path.as_posix()}")

    # Summary
    for quad in ["LEADING", "IMPROVING", "WEAKENING", "LAGGING"]:
        names = [s.ticker for s in snapshots if s.rrg_quadrant == quad]
        if names:
            print(f"  {quad}: {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")

    print("  Done.\n")
    return html_path


if __name__ == "__main__":
    run_rotation_map()
