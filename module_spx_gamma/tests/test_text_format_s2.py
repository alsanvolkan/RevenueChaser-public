"""M-SPX-3 (#376): SPX text and format (DF24, DF28, DF29, DF30, SPX-7 option B)."""
from __future__ import annotations

import json

import pytest

from RevenueChaser.module_spx_gamma import render, run as spx_run

from .conftest import make_snapshot
from .test_s2_fetch import DAY, _fetch, _no_network


@pytest.fixture(autouse=True)
def _no_local_state(monkeypatch):
    monkeypatch.setattr(spx_run, "_load_history", lambda *a, **k: {})
    monkeypatch.setattr(render, "scorecard", lambda *a, **k: {
        "average": None, "n": 0, "verdict": "No scored weeks yet", "calibrated": None, "records": []})


def _html(**over):
    s = make_snapshot()
    s.update(over)
    return render.render_html(s)


# ── DF24: RoR and IV are decimals, not integers ────────────────────────────
def test_iron_butterfly_iv_and_ror_keep_their_decimals():
    s = make_snapshot()
    s["iron_butterfly"]["width_source"]["iv_used"] = 0.18
    s["iron_butterfly"]["return_on_risk"] = 0.35
    html = render.render_html(s)
    md = render.render_markdown(s)
    assert "IV 0.18" in html and "IV 0)" not in html
    assert "1 : 0.35" in html
    assert "IV 0.18" in md and "1 : 0.35" in md


# ── DF28: the VIX row shows 9D and 6M ──────────────────────────────────────
def test_vix_row_shows_9d_and_6m():
    html = _html(vix={"vix": 15.5, "vix9d": 14.25, "vix3m": 17.0, "vix6m": 18.5, "vix1y": 19.0,
                      "vvix": 90.0, "term_mode": "contango"})
    assert "9D 14.25" in html and "6M 18.5" in html


# ── DF29: the vol term structure is rendered ───────────────────────────────
def test_vol_term_structure_table_is_rendered():
    html = _html(vol_term=[{"expiry": "2026-09-18", "dte": 35, "volatility": 0.155, "implied_move_perc": 0.045}])
    assert "Vol Term Structure" in html
    assert "2026-09-18" in html and "15.5%" in html and "+4.50%" in html


# ── DF30: None prints N/A, not 0 ───────────────────────────────────────────
def test_missing_iv_rank_prints_na():
    html = _html(iv_rank={"iv_rank_1y": None, "volatility": 0.12})
    row = html.split("IV Rank (1Y)")[1].split("</tr>")[0]
    assert "N/A" in row and "0.0%" not in row


def test_missing_vrp_and_skew_print_na():
    html = _html(variance_risk_premium={"risk_premium": None, "rank": None},
                 skew={"put_bias_1m": None, "put_bias_3m": None, "note": "x"})
    vrp_row = html.split("Variance Risk Premium</td>")[1].split("</tr>")[0]
    skew_row = html.split("Skew (Put Bias)</td>")[1].split("</tr>")[0]
    assert "N/A" in vrp_row and "0.0%" not in vrp_row and "0%" not in vrp_row.split("rank")[0]
    assert "N/A" in skew_row and "0.0%" not in skew_row


def test_missing_realized_vol_prints_na_instead_of_failing():
    html = _html(realized_vol={"implied_volatility": None, "realized_volatility": 0.11})
    row = html.split("Realized vs Implied")[1].split("</tr>")[0]
    assert "IV N/A" in row and "RV 11.0%" in row


# ── SPX-7 B: live files inside a backfilled day are labelled historical ────
def test_vix_in_a_backfilled_day_is_the_days_own_stored_value():
    """PR 3: the VIX family is read from the market store for the day shown, so it is never a later session's
    value; an older raw day whose manifest still lists vix_family.json as live does not hide it."""
    html = _html(vix={"vix": 15.5, "vix3m": 17.0, "term_mode": "contango"},
                 historical_live_files=["vix_family.json"])
    row = html.split("VIX family</td>")[1].split("</tr>")[0]
    assert "n/a (historical)" not in row and "15.5" in row


def test_live_skew_in_a_backfilled_day_shows_the_historical_label():
    html = _html(skew={"put_bias_1m": 0.031, "put_bias_3m": 0.042, "note": "x"},
                 historical_live_files=["interpolated_iv.json"])
    row = html.split("Skew (Put Bias)</td>")[1].split("</tr>")[0]
    assert "n/a (historical)" in row and "3.1%" not in row


def test_iron_butterfly_from_a_later_chain_shows_the_historical_label():
    s = make_snapshot()
    s["historical_live_files"] = ["option_chain.json"]
    html = render.render_html(s)
    panel = html.split("Iron Butterfly — Pin Harvest</h2>")[1].split("At a Glance")[0]
    assert "n/a (historical)" in panel
    assert "Center (Pin)" not in panel and "ib-legs" not in panel.split("</style>")[-1]


def test_the_current_day_keeps_its_live_values():
    html = _html(vix={"vix": 15.5, "vix3m": 17.0, "term_mode": "contango"}, historical_live_files=[])
    assert "n/a (historical)" not in html


def test_refresh_of_a_backfilled_day_marks_its_live_files(tmp_path, monkeypatch):
    # the stored day was fetched today (api_snapshot_time = now), long after DAY's session
    _fetch(tmp_path / "raw")
    _no_network(monkeypatch)
    spx_run.refresh(DAY, data_root=tmp_path)
    snap = json.loads((tmp_path / "parsed" / DAY / "snapshot.json").read_text(encoding="utf-8"))
    assert "option_chain.json" in snap["historical_live_files"]
    assert "vix_family.json" not in snap["historical_live_files"], "not a file of the day since PR 3"
    assert "gex_levels.json" not in snap["historical_live_files"]
    # the panel the day's live chain feeds is the one marked (the VIX panel was, before PR 3 read VIX from the store)
    assert render._historical_panels(snap)["iron_butterfly"] is True


def test_a_day_fetched_in_its_own_session_is_not_historical(tmp_path):
    day = tmp_path / "raw" / DAY
    day.mkdir(parents=True)
    (day / "manifest.json").write_text(json.dumps({
        "effective_date": DAY, "requested_date": DAY, "api_snapshot_time": f"{DAY}T19:30:00+00:00",
        "files": {"vix_family.json": {"as_of": False}, "gex_levels.json": {"as_of": True}}}), encoding="utf-8")
    assert spx_run._historical_live_files(day) == []


def test_a_saturday_fetch_of_friday_is_not_historical(tmp_path):
    day = tmp_path / "raw" / DAY            # 2026-08-14 is a Friday
    day.mkdir(parents=True)
    (day / "manifest.json").write_text(json.dumps({
        "effective_date": DAY, "requested_date": "2026-08-15", "api_snapshot_time": "2026-08-15T16:00:00+00:00",
        "files": {"vix_family.json": {"as_of": False}}}), encoding="utf-8")
    assert spx_run._historical_live_files(day) == []
    (day / "manifest.json").write_text(json.dumps({
        "effective_date": DAY, "requested_date": DAY, "api_snapshot_time": "2026-08-17T16:00:00+00:00",
        "files": {"vix_family.json": {"as_of": False}}}), encoding="utf-8")
    assert spx_run._historical_live_files(day) == ["vix_family.json"]


def test_markdown_survives_missing_vol_numbers():
    s = make_snapshot()
    s.update(realized_vol={"implied_volatility": None, "realized_volatility": 0.11},
             skew={"put_bias_1m": None, "put_bias_3m": 0.02, "note": "x"},
             variance_risk_premium={"risk_premium": None, "rank": None})
    md = render.render_markdown(s)
    assert "IV **N/A**" in md and "RV **11.0%**" in md and "3M **2.0%**" in md


def _manifest_day(tmp_path, effective, fetched_utc):
    day = tmp_path / "raw" / effective
    day.mkdir(parents=True, exist_ok=True)
    (day / "manifest.json").write_text(json.dumps({
        "effective_date": effective, "requested_date": effective, "api_snapshot_time": fetched_utc,
        "files": {"vix_family.json": {"as_of": False}, "gex_levels.json": {"as_of": True}}}), encoding="utf-8")
    return day


@pytest.mark.parametrize("fetched_utc, historical", [
    ("2026-08-14T12:00:00Z", False),   # Fri 08:00 NY: pre-open fallback to Thu keeps Thu's live values
    ("2026-08-14T13:29:59Z", False),   # one second before the 09:30 NY open
    ("2026-08-14T13:30:00Z", True),    # Fri open: the live files are Friday's now
    ("2026-08-14T16:00:00Z", True),    # Fri 12:00 NY
])
def test_a_later_session_starts_at_the_ny_open(tmp_path, fetched_utc, historical):
    day = _manifest_day(tmp_path, "2026-08-13", fetched_utc)    # Thu effective day
    assert spx_run._historical_live_files(day) == (["vix_family.json"] if historical else [])


def test_a_monday_pre_open_fetch_of_friday_keeps_its_live_values(tmp_path):
    day = _manifest_day(tmp_path, DAY, "2026-08-17T11:00:00Z")   # Mon 07:00 NY, Friday effective
    assert spx_run._historical_live_files(day) == []
