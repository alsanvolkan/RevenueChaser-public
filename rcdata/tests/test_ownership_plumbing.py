"""The ownership store (build plan PR 16): its serving kind, its providers' day caps, and its datasets' registry
order (the sources first, ``ownership/serve`` last, every one started by hand)."""
from __future__ import annotations

from RevenueChaser.rcdata import caps, serving


def test_ownership_is_a_serving_kind_of_version_1():
    assert serving.SERVING_SCHEMA_VERSION["ownership"] == 1
    assert "ownership" in serving.KINDS
    assert serving.live_path("ownership").name == "ownership.duckdb"


def test_the_ownership_providers_have_a_day_cap():
    for provider in ("sec", "finra", "nasdaqtrader", "nyse", "ibkr"):
        assert caps.cap_for(provider), provider
