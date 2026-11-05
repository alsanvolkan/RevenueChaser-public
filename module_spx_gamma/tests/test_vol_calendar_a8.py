"""A8 (Fable report review, 5 Nov 2026): the week's volatility calendar from UW's economic calendar.

The Fetch asks /market/economic-calendar once (live). The card lists the events from the board's day through the
target Friday, high-impact ones (CPI, the employment report, PPI, FOMC, PCE, GDP, retail sales) marked, times in
Europe/Amsterdam, duplicates dropped (UW repeats rows).
"""
from __future__ import annotations

import json

from RevenueChaser.module_spx_gamma import normalize as nm, snapshot as snap, vol_calendar
from RevenueChaser.module_spx_gamma.raw_day import load_raw_day

PAYLOAD = {"data": [
    {"type": "report", "time": "2026-11-06T13:30:00Z", "event": "Employment Report", "prev": "29000", "forecast": "55000"},
    {"type": "report", "time": "2026-11-06T13:30:00Z", "event": "Employment Report", "prev": "29000", "forecast": "55000"},
    {"type": "report", "time": "2026-11-05T13:30:00Z", "event": "Weekly Jobless Claims", "prev": "194000"},
    {"type": "fed-speaker", "time": "2026-11-06T14:10:00Z", "event": "Fed President Musalem speaks"},
    {"type": "report", "time": "2026-11-10T13:30:00Z", "event": "CPI, Y/Y%", "prev": "3.7%"},
    {"type": "report", "time": "2026-11-03T13:30:00Z", "event": "Old event"},
]}


def test_week_events_through_the_target_friday():
    rows = vol_calendar.week_events(PAYLOAD, "2026-11-04")       # target Friday 2026-11-06
    assert [r["event"] for r in rows] == ["Weekly Jobless Claims", "Employment Report",
                                          "Fed President Musalem speaks"]
    emp = rows[1]
    assert emp["high_impact"] is True and emp["local"] == "Fri 6 Nov 14:30"     # CET
    assert rows[0]["high_impact"] is False


def test_cpi_is_high_impact():
    rows = vol_calendar.week_events(PAYLOAD, "2026-11-09")        # target 2026-11-13
    assert rows[0]["event"] == "CPI, Y/Y%" and rows[0]["high_impact"] is True


def test_snapshot_and_raw_day(tmp_path):
    assert nm.normalize_snapshot({"economic_calendar": PAYLOAD}, "2026-11-04")["vol_calendar"][0]["event"] == \
        "Weekly Jobless Claims"
    assert "economic_calendar.json" in snap.LIVE_FILES
    (tmp_path / "manifest.json").write_text(json.dumps({"effective_date": "2026-11-04"}), encoding="utf-8")
    (tmp_path / "economic_calendar.json").write_text(json.dumps(PAYLOAD), encoding="utf-8")
    assert load_raw_day(tmp_path)["economic_calendar"] == PAYLOAD


def test_the_fetch_asks_the_calendar_once():
    assert snap.BASE_ENDPOINTS["economic_calendar"] == ("/market/economic-calendar", {})


def test_a_refresh_keeps_the_pr415_files(tmp_path):
    """greek_flow.json and vix_greeks.json (PR 415) were written by the Fetch but not read back, so a Refresh
    dropped the vega flow and the VIX option book."""
    (tmp_path / "manifest.json").write_text(json.dumps({"effective_date": "2026-11-04"}), encoding="utf-8")
    for name in ("greek_flow", "vix_greeks"):
        (tmp_path / f"{name}.json").write_text(json.dumps({"data": [1]}), encoding="utf-8")
    raw = load_raw_day(tmp_path)
    assert raw["greek_flow"] == {"data": [1]} and raw["vix_greeks"] == {"data": [1]}
