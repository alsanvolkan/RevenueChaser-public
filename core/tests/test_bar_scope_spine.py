"""The nightly Daily Bars scope (``core.prices.bar_scope``) on the spine (#262, PR 7): while the boards read the
identity spine, the universe part of the scope is the spine's Tier A names, the names the boards can screen, so the
bars they read exist; on the old store it stays ``equity_universe``."""
from __future__ import annotations

import pytest

from RevenueChaser.core import prices, universe
from RevenueChaser.core.tests.test_universe_spine_flag import old_store, spine  # noqa: F401 - the fixtures
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built  # noqa: F401 - the 10 names

TIER_A = {"MSFT", "HEI", "HEI-A", "ETN", "TM", "CNA", "FDX", "NVR", "VYLR"}


@pytest.mark.usefixtures("spine")
def test_on_the_spine_the_scope_is_the_benchmarks_the_tier_a_names_and_the_etfs():
    scope = prices.bar_scope()
    assert scope[:3] == ["SPY", "DIA", "QQQ"]
    assert TIER_A <= set(scope)
    assert not {"OLDONLY", "WFC-PC", "OTCX", "NOEX"} & set(scope)     # old-only, Tier B, OTC, no exchange
    assert "XLK" in scope and len(scope) == len(set(scope))


def test_on_the_old_store_the_scope_is_its_universe(monkeypatch, built, old_store):
    monkeypatch.setenv(universe.SOURCE_ENV, universe.STORE)
    scope = prices.bar_scope()
    assert "OLDONLY" in scope and "WFC-PC" in scope and "VYLR" not in scope
