"""E0f, D29 (#305, the CFTC part): the weekly E-mini S&P 500 Traders-in-Financial-Futures row.

No request leaves the process: the Socrata read is an injected ``fetch`` (or a monkeypatched ``fetch_rows``).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from RevenueChaser.core import nightly_inputs as N
from RevenueChaser.core import rawstore
from RevenueChaser.module_earnings_radar import inputs
from RevenueChaser.module_earnings_radar.inputs import cftc_cot as d29
from RevenueChaser.module_spx_gamma.cftc import _map_row

NY = ZoneInfo("America/New_York")
NIGHT = datetime(2026, 10, 13, 19, 30, tzinfo=NY).timestamp()        # session 2026-10-13, complete

ROW = {
    "market_and_exchange_names": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
    "report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000",
    "open_interest_all": "2000000",
    "asset_mgr_positions_long": "500000", "asset_mgr_positions_short": "120000",
    "change_in_asset_mgr_long": "1500",
    "lev_money_positions_long": "300000", "lev_money_positions_short": "260000",
    "change_in_lev_money_long": "-2200",
    "dealer_positions_long_all": "90000", "dealer_positions_short_all": "420000",
    "change_in_dealer_long_all": "300",
}


def _run(sweeps, tmp_path, *, run_at=NIGHT, caps=None, run_id="r1"):
    caps = {"uw": 0, "fred": 0, "yfinance": 0, "cboe": 0, "cftc": 5, **(caps or {})}
    return N.run_step("earnings", sweeps, caps=caps, run_id=run_id, run_at=run_at, log=lambda s: None,
                      ledger_dir=tmp_path)


def _rows():
    return rawstore.query("SELECT * FROM cftc_cot ORDER BY report_date")


class _Ctx:
    """The bare ``StepRun`` a direct ``run(ctx, ...)`` call needs."""

    def __init__(self):
        self.run_id, self.run_at, self.session_date = "r1", NIGHT, date(2026, 10, 13)

    def budget(self, provider):
        return N.CallBudget(provider, 5)

    def stamps(self, fetched_at=None):
        return {"run_id": self.run_id, "run_at": self.run_at, "fetched_at": fetched_at or 0.0}


# --- the row --------------------------------------------------------------------------------------------------------

def test_d29_maps_the_socrata_row_onto_the_table_columns(rawstore_tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d29, "fetch_rows", lambda: ([ROW, dict(ROW, report_date_as_yyyy_mm_dd="2026-09-29")], None))
    res = _run((d29.SWEEP,), tmp_path)
    s = res["sweeps"]["D29"]
    assert s["status"] == "ok" and s["written"] == 2

    row = _rows()[0]
    mapped = _map_row(ROW)
    assert mapped is not None
    assert (row["report_type"], row["market"]) == ("TFF", "E-MINI S&P 500")
    assert (row["report_date"], row["release_date"], row["session_date"]) == (
        date(2026, 9, 29), date(2026, 10, 2), date(2026, 9, 29))
    assert row["open_interest"] == mapped["open_interest"] == 2000000.0
    assert row["asset_mgr_long"] == mapped["asset_manager"]["long"] == 500000.0
    assert row["asset_mgr_short"] == mapped["asset_manager"]["short"] == 120000.0
    assert row["asset_mgr_change_long"] == mapped["asset_manager"]["change"] == 1500.0
    assert row["lev_money_long"] == mapped["leveraged_funds"]["long"] == 300000.0
    assert row["lev_money_short"] == mapped["leveraged_funds"]["short"] == 260000.0
    assert row["lev_money_change_long"] == mapped["leveraged_funds"]["change"] == -2200.0
    assert row["dealer_long"] == mapped["dealer"]["long"] == 90000.0
    assert row["dealer_short"] == mapped["dealer"]["short"] == 420000.0
    assert row["dealer_change_long"] == mapped["dealer"]["change"] == 300.0
    assert (row["source"], row["run_id"], row["run_at"]) == ("cftc", "r1", NIGHT)


def test_d29_keeps_the_source_row_as_json_that_round_trips(rawstore_tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d29, "fetch_rows", lambda: ([ROW], None))
    _run((d29.SWEEP,), tmp_path)
    kept = json.loads(_rows()[0]["raw_row"])
    assert kept["asset_mgr_positions_long"] == "500000"                 # as served, not coerced
    assert kept["market_and_exchange_names"] == ROW["market_and_exchange_names"]
    assert kept["report_date_as_yyyy_mm_dd"] == "2026-10-06T00:00:00.000"


def test_d29_release_date_is_three_days_after_the_report_date(rawstore_tmp_db):
    ctx = _Ctx()
    row = d29.row_from(ROW, ctx, 1.0)
    assert row["report_date"] == date(2026, 10, 6) and row["release_date"] == date(2026, 10, 9)
    stated = d29.row_from(dict(ROW, release_date="2026-10-08"), ctx, 1.0)
    assert stated["release_date"] == date(2026, 10, 8)                   # the source wins when it states one


def test_d29_skips_a_row_the_parser_does_not_recognise():
    ctx = _Ctx()
    assert d29.row_from({"report_date_as_yyyy_mm_dd": "2026-10-06"}, ctx, 1.0) is None
    assert d29.row_from(dict(ROW, report_date_as_yyyy_mm_dd=None), ctx, 1.0) is None


# --- idempotence, cap, failure ---------------------------------------------------------------------------------------

def test_d29_a_second_run_adds_no_duplicate_row(rawstore_tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d29, "fetch_rows", lambda: ([ROW], None))
    first = _run((d29.SWEEP,), tmp_path)
    second = _run((d29.SWEEP,), tmp_path, run_id="r2")
    assert first["sweeps"]["D29"]["written"] == 1 and second["sweeps"]["D29"]["written"] == 0
    assert len(_rows()) == 1


def test_d29_sends_nothing_without_its_budget(rawstore_tmp_db, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(d29, "fetch_rows", lambda: sent.append(1) or ([ROW], None))
    res = _run((d29.SWEEP,), tmp_path, caps={"cftc": 0})
    assert sent == [] and res["sweeps"]["D29"]["note"] == "call_cap"
    assert res["calls"]["cftc"] == {"limit": 0, "before": 0, "used": 0, "refused": 1}
    assert _rows() == []


def test_d29_a_failed_request_is_a_failed_sweep_that_says_why(rawstore_tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(d29, "fetch_rows", lambda: (None, "HTTP 500"))
    res = _run((d29.SWEEP,), tmp_path)
    s = res["sweeps"]["D29"]
    assert s["status"] == "failed" and "HTTP 500" in s["error"] and _rows() == []


def test_d29_is_registered_in_the_step():
    assert "D29" in [s.name for s in inputs.sweeps()]
    assert d29.SWEEP.tables == ("cftc_cot",)
    assert rawstore.MANIFEST["cftc_cot"]["key"] == ["report_type", "market", "report_date"]
