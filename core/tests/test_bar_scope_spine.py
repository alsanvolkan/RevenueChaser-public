"""The nightly Daily Bars scope (``core.prices.bar_scope``) on the spine (#262, PR 7): while the boards read the
identity spine, the universe part of the scope is the spine's listed and over-the-counter names in tier order (Tier A,
the names the boards screen, first), so the bars they read exist and the nightly's UW cap is spent on them first; on
the old store it stays ``equity_universe``."""
from __future__ import annotations

import pytest

from RevenueChaser.core import prices, rawstore, universe
from RevenueChaser.core.tests.test_prices import NOW, FakeDownload, FakeUW, bar_row, rows_for, seed_spy, yf_row
from RevenueChaser.core.tests.test_universe_spine_flag import old_store, spine  # noqa: F401 - the fixtures
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built  # noqa: F401 - the 10 names

TIER_A = {"MSFT", "HEI", "HEI-A", "ETN", "TM", "CNA", "FDX", "NVR", "VYLR"}
ETFS = [t for t, info in prices.ROTATION_ETFS.items() if (info or {}).get("group") not in ("yield", "commodity")]


@pytest.mark.usefixtures("spine")
def test_on_the_spine_the_scope_is_the_benchmarks_tier_a_the_etfs_then_tier_b_and_c():
    scope = prices.bar_scope()
    assert scope[:3] == ["SPY", "DIA", "QQQ"]
    assert TIER_A <= set(scope) and len(scope) == len(set(scope))
    last_a = max(scope.index(t) for t in TIER_A)
    etfs = [scope.index(t) for t in ETFS]
    assert last_a < min(etfs) and max(etfs) < scope.index("WFC-PC") < scope.index("OTCX")   # B a preferred, C OTC
    assert not {"OLDONLY", "NOEX"} & set(scope)                     # old-only, no exchange (Tier D)


@pytest.mark.usefixtures("spine")
def test_the_nightly_uw_cap_reaches_tier_a_first_and_the_rest_still_gets_yahoo_bars(rawstore_tmp_db):
    scope = prices.bar_scope()
    seed_spy()
    rawstore.write_rows("price_bars_daily", [bar_row(t, "2026-10-07", close=10.0) for t in scope if t != "SPY"])
    uw = FakeUW({t: [{"date": "2026-10-08", "close": 11.0, "volume": 1}] for t in scope})
    dl = FakeDownload({t: {"2026-10-08": yf_row(11.0)} for t in scope})
    cap = scope.index(ETFS[0])                                      # the benchmarks and Tier A
    res = prices.ensure_daily_bars(uw=uw, yf_download=dl, now=NOW, run_id="r1", max_uw_calls=cap)
    assert [t for t, _ in uw.requests] == scope[:cap] and TIER_A <= set(scope[:cap])
    assert res["uw_capped"] == len(scope) - cap
    assert rows_for("OTCX", "2026-10-08")[0]["source"] == "yfinance"    # past the cap: Yahoo's bar, not none


def test_on_the_old_store_the_scope_is_its_universe(monkeypatch, built, old_store):
    monkeypatch.setenv(universe.SOURCE_ENV, universe.STORE)
    scope = prices.bar_scope()
    assert "OLDONLY" in scope and "WFC-PC" in scope and "VYLR" not in scope
