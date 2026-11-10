"""#400: the profile's company name, sector and industry come from the spine (avo, 10 Nov 2026, before the 400 run).

``equity_universe`` cuts a name at about 31 characters (4,501 of 7,664 names differed from the spine's on homesrv),
and the name is part of the profile's input hash, so a later switch would redo those profiles. The old table still
answers a ticker the spine does not hold.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_relatedness import engine
from RevenueChaser.rcdata import read
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - the spine build's mocks
from RevenueChaser.rcdata.tests.test_spine_security import built  # noqa: F401 - the built spine


@pytest.mark.usefixtures("market_roots", "built")
def test_the_spine_name_sector_and_industry_win_over_the_old_universe():
    spine = read.identity(["MSFT"])[0]
    rawstore.replace_all("equity_universe", [
        {"ticker": "MSFT", "name": "MICROSOFT CORPORATION OF REDMON", "sector": "Old", "industry": "Old"},
        {"ticker": "ZZZQ", "name": "Only In The Old Table", "sector": "Energy", "industry": "Oil"}])
    assert engine._company_names(["MSFT", "ZZZQ"]) == {"MSFT": spine["name"], "ZZZQ": "Only In The Old Table"}
    ctx = engine._context("MSFT", None)
    assert (ctx.get("sector"), ctx.get("industry")) == (spine["sector"], spine["industry"]) and spine["sector"]
    assert engine._context("ZZZQ", None)["sector"] == "Energy"
