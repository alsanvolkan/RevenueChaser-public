"""The ownership store (build plan PR 16): its serving kind, its providers' day caps, and its datasets' registry
order (the sources first, ``ownership/serve`` last, every one started by hand)."""
from __future__ import annotations

from RevenueChaser.rcdata import caps, serving


def test_ownership_is_a_serving_kind_of_version_4():
    """4: ``sec13f_type_holdings`` (#760); 3: ``uw_institution`` and ``uw_institution_succession`` (#775); 2: the
    continuing-filer flows (#732)."""
    assert serving.SERVING_SCHEMA_VERSION["ownership"] == 4
    assert "ownership" in serving.KINDS
    assert serving.live_path("ownership").name == "ownership.duckdb"


def test_the_ownership_providers_have_no_day_cap():
    """None of them publishes a day quota (master plan Order 5, rule 3): SEC and FINRA have a window, the rest none."""
    for provider in ("sec", "finra", "nasdaqtrader", "nyse", "ibkr"):
        assert caps.cap_for(provider) is None, provider
    assert caps.WINDOWS["sec"] == (10, 1.0) and caps.WINDOWS["finra"] == (1_200, 60.0)


def test_the_ownership_datasets_build_in_order_and_start_by_hand():
    from RevenueChaser.rcdata.build import DATASETS
    names = [n for n, d in DATASETS.items() if d.kind == "ownership"]
    assert names == ["finra/short_interest", "regsho/threshold", "ibkr/shortable", "sec/ftd", "sec/13f",
                     "sec/13f_sets_listed", "sec/form4", "sec/13dg", "uw/institutions", "ownership/serve"]
    assert all(DATASETS[n].starts_by_hand for n in names)
