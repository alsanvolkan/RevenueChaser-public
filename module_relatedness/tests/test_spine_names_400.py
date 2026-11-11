"""#400, #642 PR 3: the profile's company name, sector, industry and SIC come from the spine only.

``equity_universe`` cut a name at about 31 characters (4,501 of 7,664 names differed from the spine's on homesrv),
and the name is part of the profile's input hash. The old table was an underlay for a ticker the spine did not hold
until #642 PR 3; now such a ticker is sent as itself, and a profile run with no spine to read stops (D3) instead of
re-keying every profile to bare tickers.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import engine
from RevenueChaser.rcdata import read
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - the spine build's mocks
from RevenueChaser.rcdata.tests.test_spine_security import built  # noqa: F401 - the built spine


@pytest.mark.usefixtures("market_roots", "built")
def test_the_spine_alone_names_the_company_and_gives_sector_and_industry():
    spine = read.identity(["MSFT"])[0]
    rawstore.replace_all("equity_universe", [
        {"ticker": "MSFT", "name": "MICROSOFT CORPORATION OF REDMON", "sector": "Old", "industry": "Old"},
        {"ticker": "ZZZQ", "name": "Only In The Old Table", "sector": "Energy", "industry": "Oil"}])
    assert engine._company_names(["MSFT", "ZZZQ"]) == {"MSFT": spine["name"]}
    ctx = engine._context("MSFT", None)
    assert (ctx.get("sector"), ctx.get("industry")) == (spine["sector"], spine["industry"]) and spine["sector"]
    assert "sector" not in engine._context("ZZZQ", None)


def test_a_profile_run_with_no_spine_file_stops_before_any_call(monkeypatch):
    from RevenueChaser.rcdata import serving
    serving.live_path("spine").unlink()
    monkeypatch.setattr(engine.profiles_mod, "profile_for", lambda *a, **k: pytest.fail("no call without a spine"))
    with pytest.raises(engine.RunStopped) as stop:
        engine.build_profiles(["AAA"])
    assert stop.value.reason == "spine" and "no spine.duckdb yet" in str(stop.value)


def test_a_failing_spine_read_stops_the_run(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("locked")
    monkeypatch.setattr(read, "identity", boom)
    with pytest.raises(engine.RunStopped) as stop:
        engine.build_profiles(["AAA"])
    assert stop.value.reason == "spine" and "locked" in str(stop.value)


def test_an_empty_ticker_list_loads_no_profile(rawstore_tmp_db):
    """``load_profiles([])`` read every stored profile (``if tickers`` was false for an empty list, #642 PR 3)."""
    from RevenueChaser.module_relatedness import store
    store.save_profile("AAA", "", {"products": ["x"]}, "k", "m")
    assert store.load_profiles([]) == {}
    assert set(store.load_profiles(None)) == {"AAA"}
