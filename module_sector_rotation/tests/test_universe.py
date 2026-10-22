"""Rotation universe shape (#374 ROT-4 C1)."""
from RevenueChaser.module_sector_rotation.universe import ROTATION_ETFS, TICKER_ETF_MAP


def test_every_ticker_etf_map_target_is_a_rotation_etf():
    targets = {t for targets in TICKER_ETF_MAP.values() for t, _ in targets}
    missing = targets - set(ROTATION_ETFS)
    assert not missing, f"TICKER_ETF_MAP targets missing from ROTATION_ETFS: {sorted(missing)}"
