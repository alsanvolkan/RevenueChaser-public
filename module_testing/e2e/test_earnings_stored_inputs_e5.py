"""E5 (#372), end to end: an Earnings Fetch with the nightly inputs stored makes no calendar, short-screener or
year-of-bars request for them (review S2, the spec's e2e).

The dashboard subprocess reads the faked clock through ``RC_E2E_NOW``: Wed 2026-08-12 17:00 New York, so 08-12 is
the latest complete session. The Fetch is for Fri 08-14, past today and the next trading day (which always read
the calendar live, S4). The store holds what RC-Earnings-Inputs and RC-Daily-Bars leave after that night: the
calendar pairs, the short-interest rows and the D01 bars ending 08-12.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.module_testing.e2e.harness.screener_seed import bars_to, point_at, seed_d01

NOW = "2026-08-12T21:00:00+00:00"           # Wed 17:00 EDT
SWEPT = date(2026, 8, 12)
DAY = date(2026, 8, 14)                     # Fri
NAMES = {"premarket": "NVDA", "afterhours": "AMD"}


@pytest.fixture
def e2e_env(e2e_env):
    return {**e2e_env, "RC_E2E_NOW": NOW}


def _item(symbol, session):
    return {"symbol": symbol, "sector": "Technology", "full_name": f"{symbol} Corp", "marketcap": "3000000000000",
            "expected_move_perc": "0.06", "street_mean_est": "1.2", "report_time": session}


@pytest.fixture
def seeded(dashboard, monkeypatch):
    import RevenueChaser.core.rawstore as rawstore
    point_at(dashboard, monkeypatch)
    rawstore.ensure_all_tables()
    rawstore.write_rows("earnings_calendar", [
        {"session_date": SWEPT, "earnings_date": DAY, "ticker": t, "report_session": s, "item": _item(t, s),
         "source": "uw"} for s, t in NAMES.items()])
    rawstore.write_rows("short_interest", [
        {"ticker": t, "session_date": SWEPT, "source": "uw_short_screener", "si_float": 0.05,
         "total_float": 1.0e9, "short_interest": 5.0e7, "days_to_cover": 2.0} for t in NAMES.values()])
    for t in NAMES.values():
        seed_d01(t, bars_to(SWEPT))
    return dashboard


def test_a_fetch_with_the_inputs_stored_asks_uw_for_none_of_them(seeded, provider_routes):
    result = seeded.run_job("/run/earnings", start=DAY.isoformat(), end=DAY.isoformat())
    assert result.ok, f"Earnings fetch failed: {result}\n{seeded.logs()}"
    seeded.wait_until_idle()

    assert provider_routes.requests("/api/earnings/premarket") == []
    assert provider_routes.requests("/api/earnings/afterhours") == []
    assert provider_routes.requests("/short_screener") == []
    for t in NAMES.values():
        assert provider_routes.requests(f"/api/stock/{t}/ohlc") == [], t
        assert provider_routes.requests(f"/api/stock/{t}/"), f"{t} was scored with its per-ticker UW reads"

    conn = duckdb.connect(str(seeded.store_dir / "history.duckdb"), read_only=True)
    try:
        tickers = sorted(r[0] for r in conn.execute("SELECT ticker FROM earnings_cards_history").fetchall())
        (context,) = conn.execute("SELECT context FROM result_runs").fetchone()
    finally:
        conn.close()
    assert tickers == ["AMD", "NVDA"]
    import json
    inputs = json.loads(context)["inputs"]
    assert inputs["calendar"] == {"stored": 2, "live": 0, "session": SWEPT.isoformat()}
    assert inputs["ohlc"] == {"stored": 2, "live": 0}
    assert inputs["short_interest"]["stored"] == 2 and inputs["short_interest"]["live"] == 0
