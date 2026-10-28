"""D28/D29 (#34): the stored vol-index and COT payloads reach ``normalize_vix`` / ``normalize_cftc`` in the shape the
old live fetchers served — one expected dict, two sources.

SPX Gamma's snapshot no longer reads ``stored_inputs`` nor asks a live source (PR 3: it reads the market store through
``store_reads``, tested in ``test_store_reads.py`` and ``test_snapshot_no_live_fallback.py``), so the tests of the
snapshot's store-then-live path are gone with that path. ``stored_inputs`` stays Earnings' reader, and these shape
tests stay with the normalizers SPX Gamma still shares.
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import rawstore
from RevenueChaser.module_earnings_radar import stored_inputs
from RevenueChaser.module_spx_gamma.normalize import normalize_cftc, normalize_vix


DAY = "2026-08-14"
DAY_DATE = date.fromisoformat(DAY)
CLOSES = {"VIX": 15.5, "VIX9D": 14.25, "VIX3M": 17.0, "VIX6M": 18.0, "VIX1Y": 19.0, "VVIX": 90.5}
COT = {"open_interest": 2_000_000.0,
       "asset_mgr_long": 500_000.0, "asset_mgr_short": 120_000.0, "asset_mgr_change_long": 1_500.0,
       "lev_money_long": 300_000.0, "lev_money_short": 260_000.0, "lev_money_change_long": -2_200.0,
       "dealer_long": 90_000.0, "dealer_short": 420_000.0, "dealer_change_long": 300.0}


def _seed_vol(session: date = DAY_DATE) -> None:
    rawstore.write_rows("vol_index_daily", [
        {"symbol": symbol, "session_date": session, "source": "cboe", "close": close,
         "open": close, "high": close, "low": close} for symbol, close in CLOSES.items()])


def _seed_cot(report: date = DAY_DATE, session: date | None = None) -> None:
    rawstore.write_rows("cftc_cot", [{
        "report_type": "TFF", "market": "E-MINI S&P 500", "report_date": report,
        "session_date": session or report, "source": "cftc", **COT}])


# ── 6. one normalizer, two sources, one dict ─────────────────────────────────────────────────────────────────────

def test_normalize_vix_cannot_tell_the_stored_payload_from_the_live_one(rawstore_tmp_db):
    _seed_vol()
    stored, session = stored_inputs.vol_index_rows(DAY_DATE)
    live = {"tickers": {f"^{s}": c for s, c in CLOSES.items()}, "term_mode": "contango"}
    expected = {"vix": 15.5, "vix9d": 14.25, "vix3m": 17.0, "vix6m": 18.0, "vix1y": 19.0, "vvix": 90.5,
                "term_mode": "contango"}

    assert session == DAY_DATE
    assert normalize_vix(stored) == expected
    assert normalize_vix(live) == expected


def test_normalize_cftc_cannot_tell_the_stored_payload_from_the_live_one(rawstore_tmp_db):
    _seed_cot()
    stored, session = stored_inputs.cot_row(DAY_DATE)
    live = {"report_date": DAY, "open_interest": 2_000_000.0,
            "asset_manager": {"long": 500_000.0, "short": 120_000.0, "net": 380_000.0, "change": 1_500.0},
            "leveraged_funds": {"long": 300_000.0, "short": 260_000.0, "net": 40_000.0, "change": -2_200.0},
            "dealer": {"long": 90_000.0, "short": 420_000.0, "net": -330_000.0, "change": 300.0}}
    expected = {"report_date": DAY, "open_interest": 2_000_000.0, "rows": [
        {"group": "Asset Manager", "long": 500_000.0, "short": 120_000.0, "net": 380_000.0, "change": 1_500.0},
        {"group": "Leveraged Funds", "long": 300_000.0, "short": 260_000.0, "net": 40_000.0, "change": -2_200.0},
        {"group": "Dealer", "long": 90_000.0, "short": 420_000.0, "net": -330_000.0, "change": 300.0},
    ]}

    assert session == DAY_DATE
    assert normalize_cftc(stored) == expected
    assert normalize_cftc(live) == expected


# ── review #103 check 2: the panel renders report_date raw, so both sources must spell it the same ───────────────

def test_the_cot_report_date_reads_the_same_whichever_source_served_it():
    """Socrata serves ``2026-10-06T00:00:00.000`` and the stored row ``2026-10-06``; the panel prints this string
    as it is (``section_position_map.html.j2``), so the normalizer settles on the plain day."""
    live = normalize_cftc({"report_date": "2026-10-06T00:00:00.000", "open_interest": 1.0})
    stored = normalize_cftc({"report_date": "2026-10-06", "open_interest": 1.0})
    assert live["report_date"] == stored["report_date"] == "2026-10-06"
    assert normalize_cftc({"report_date": None, "open_interest": 1.0})["report_date"] is None
