"""One normalizer, two payload shapes: ``normalize_vix`` / ``normalize_cftc`` cannot tell the store's payload from
the shape the old live fetchers served.

SPX Gamma's snapshot reads the market store through ``store_reads`` (PR 3; the reader's own cases are in
``test_store_reads.py`` and ``test_snapshot_no_live_fallback.py``) and asks no live source. The old
``stored_inputs.vol_index_rows`` / ``cot_row`` readers of the nightly ``raw.duckdb`` tables are gone with that path,
so the payloads here are written out as the readers hand them over, which is what the normalizers are promised.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.module_spx_gamma.normalize import normalize_cftc, normalize_vix

DAY = "2026-08-14"
DAY_DATE = date.fromisoformat(DAY)
CLOSES = {"VIX": 15.5, "VIX9D": 14.25, "VIX3M": 17.0, "VIX6M": 18.0, "VIX1Y": 19.0, "VVIX": 90.5}


def test_normalize_vix_cannot_tell_the_store_payload_from_the_live_one():
    stored = {"tickers": {f"^{s}": c for s, c in CLOSES.items()}, "term_mode": "contango", "stored": True}
    live = {"tickers": {f"^{s}": c for s, c in CLOSES.items()}, "term_mode": "contango"}
    expected = {"vix": 15.5, "vix9d": 14.25, "vix3m": 17.0, "vix6m": 18.0, "vix1y": 19.0, "vvix": 90.5,
                "term_mode": "contango"}

    assert normalize_vix(stored) == expected
    assert normalize_vix(live) == expected


def test_normalize_cftc_cannot_tell_the_store_payload_from_the_live_one():
    groups = {"asset_manager": {"long": 500_000.0, "short": 120_000.0, "net": 380_000.0, "change": 1_500.0},
              "leveraged_funds": {"long": 300_000.0, "short": 260_000.0, "net": 40_000.0, "change": -2_200.0},
              "dealer": {"long": 90_000.0, "short": 420_000.0, "net": -330_000.0, "change": 300.0}}
    stored = {"report_date": DAY, "open_interest": 2_000_000.0, "stored": True, **groups}
    live = {"report_date": DAY, "open_interest": 2_000_000.0, **groups}
    expected = {"report_date": DAY, "open_interest": 2_000_000.0, "rows": [
        {"group": "Asset Manager", "long": 500_000.0, "short": 120_000.0, "net": 380_000.0, "change": 1_500.0},
        {"group": "Leveraged Funds", "long": 300_000.0, "short": 260_000.0, "net": 40_000.0, "change": -2_200.0},
        {"group": "Dealer", "long": 90_000.0, "short": 420_000.0, "net": -330_000.0, "change": 300.0},
    ]}

    assert normalize_cftc(stored) == expected
    assert normalize_cftc(live) == expected
