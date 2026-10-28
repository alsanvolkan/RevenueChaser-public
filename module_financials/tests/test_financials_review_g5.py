"""The Financials Fable review's fixes (build plan PR 12, group G5). No network.

S1: a base-year input the statements do not give is a default, and the board says so instead of showing it as a
measured number (the #173 convention: missing is unknown, never a silent number).
S4: the header's "Last sync" and the comps' "as of" name the same New York session (core/calendar rule B), and a
fetch inside the session says "intraday", not "EOD".
S5: Quick Remarks say when they were scored, and a Refresh shows what the last Fetch scored instead of an empty card.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timezone

import pytest

import RevenueChaser.core.ticker_store as ts
from RevenueChaser.module_asymmetric_growth import engine as agh
from RevenueChaser.module_financials import history as fin_history
from RevenueChaser.module_financials.engines import quick_remarks_engine as qr
from RevenueChaser.module_financials.engines.dcf_engine import run_dcf
from RevenueChaser.module_financials.engines.three_statement_engine import run_three_statement
from RevenueChaser.module_financials.reporting.html_builder import build_report_html
from RevenueChaser.module_financials.reporting.quick_remarks_html import (render_quick_remarks_html,
                                                                       render_quick_remarks_section)

NWC_LINES = ("receivables", "inventory", "payables", "current_assets", "current_liabilities")


def _drop(financials, *keys, every_year=False):
    f = copy.deepcopy(financials)
    for rec in f["annual"] if every_year else f["annual"][-1:]:
        for key in keys:
            rec[key] = None
    return f


def _html(financials, modules):
    return build_report_html({"ticker": "TEST", "financials": financials, "modules": modules, "warnings": []})


# ── S1: the DCF's base year ─────────────────────────────────────────────────
def test_a_measured_base_year_carries_no_default(financials):
    res = run_dcf(financials)
    assert res["base_year"]["defaults"] == [] and res["wacc"]["weights_source"] == "market"
    assert not [w for w in res["warnings"] if "assumed" in w]


def test_a_missing_ebitda_is_a_labelled_default_margin(financials):
    res = run_dcf(_drop(financials, "ebitda", "ebit"))
    assert res["base_year"]["ebitda_margin"] == pytest.approx(0.30)          # the number is unchanged
    assert "ebitda_margin" in res["base_year"]["defaults"]
    assert "EBITDA margin: assumed 30.0% (not reported)" in res["warnings"]


def test_a_margin_the_user_typed_is_not_warned_as_assumed(financials):
    res = run_dcf(_drop(financials, "ebitda", "ebit"), {"ebitda_margin": 0.2})
    assert "ebitda_margin" in res["base_year"]["defaults"]                  # the base year itself still has none
    assert not [w for w in res["warnings"] if w.startswith("EBITDA margin")]


def test_a_missing_capex_is_a_labelled_default(financials):
    res = run_dcf(_drop(financials, "capex"))
    assert res["base_year"]["capex_pct_rev"] == pytest.approx(0.05)
    assert "capex_pct_rev" in res["base_year"]["defaults"]
    assert "Capex: assumed 5.0% of revenue (not reported)" in res["warnings"]


def test_a_capex_the_user_typed_is_no_default(financials):
    res = run_dcf(_drop(financials, "capex"), {"capex_pct_rev": 0.07})
    assert "capex_pct_rev" not in res["base_year"]["defaults"]
    assert not [w for w in res["warnings"] if w.startswith("Capex")]


def test_no_working_capital_lines_is_a_labelled_default_nwc(financials):
    res = run_dcf(_drop(financials, *NWC_LINES, every_year=True))
    assert res["base_year"]["nwc_pct_rev"] == 0.0
    assert "nwc_pct_rev" in res["base_year"]["defaults"]
    assert "NWC: assumed 0.0% of revenue (no working-capital lines reported)" in res["warnings"]


def test_one_year_with_working_capital_lines_is_enough(financials):
    f = _drop(financials, *NWC_LINES, every_year=True)
    f["annual"][0].update(receivables=80.0, inventory=60.0, payables=40.0)
    assert "nwc_pct_rev" not in run_dcf(f)["base_year"]["defaults"]


def test_a_missing_dna_marks_its_row_default_too(financials):
    assert "dep_pct_rev" in run_dcf(_drop(financials, "depreciation"))["base_year"]["defaults"]


@pytest.mark.parametrize("key", ["market_cap", "total_debt"])
def test_no_market_cap_or_debt_is_a_labelled_default_weight(financials, key):
    f = copy.deepcopy(financials)
    if key == "market_cap":
        f["market_cap"] = None
    else:
        f["annual"][-1]["total_debt"] = None
    res = run_dcf(f)
    assert (res["wacc"]["equity_weight"], res["wacc"]["debt_weight"]) == (0.85, 0.15)
    assert res["wacc"]["weights_source"] == "default"
    assert "WACC weights: assumed 85% equity / 15% debt (market cap or total debt missing)" in res["warnings"]


def test_a_debt_weight_the_user_typed_is_an_assumption(financials):
    res = run_dcf(dict(financials, market_cap=None), {"debt_weight": 0.3})
    assert res["wacc"]["weights_source"] == "assumption"
    assert not [w for w in res["warnings"] if w.startswith("WACC weights")]


# ── S1: the three-statement model ──────────────────────────────────────────
def test_the_three_statement_model_labels_each_default(financials):
    res = run_three_statement(_drop(financials, "ebitda", "ebit", "capex", "receivables", "inventory", "payables"))
    assert res["defaults"] == ["ebitda_margin", "capex_pct_rev", "dso", "dio", "dpo"]
    for line in ("EBITDA margin: assumed 30.0% (not reported)", "Capex: assumed 5.0% of revenue (not reported)",
                 "Receivable days: assumed 45 (not reported)", "Inventory days: assumed 60 (not reported)",
                 "Payable days: assumed 45 (not reported)"):
        assert line in res["warnings"]


def test_a_measured_three_statement_model_has_no_default(financials):
    res = run_three_statement(financials)
    assert res["defaults"] == [] and not [w for w in res["warnings"] if "assumed" in w]


def test_days_the_user_typed_are_no_default(financials):
    res = run_three_statement(_drop(financials, "receivables"), {"dso": 30})
    assert "dso" not in res["defaults"]


# ── S1: the page ────────────────────────────────────────────────────────────
def test_the_page_labels_the_default_rows(financials):
    f = _drop(financials, "ebitda", "ebit", "capex", "depreciation")
    f["market_cap"] = None
    for rec in f["annual"]:
        for key in NWC_LINES:
            rec[key] = None
    html = _html(f, {"dcf": run_dcf(f)})
    for row in ("default 30.0%", "default 5.0%", "default 0.0%", "default 85.0%", "default 15.0%"):
        assert row in html
    assert html.count("default 0.0%") == 2                                  # D&A and NWC


def test_a_measured_page_says_default_nowhere(financials):
    assert "default" not in _html(financials, {"dcf": run_dcf(financials)})


# ── S4: one New York day in the header and in the comps' "as of" ───────────────
@pytest.mark.parametrize("stamp, header", [
    ("2026-10-06T20:45:00+00:00", "Tuesday, October 06, 2026 EOD"),        # 16:45 ET, the session is complete
    ("2026-10-07T02:00:00+00:00", "Tuesday, October 06, 2026 EOD"),        # 22:00 ET: UTC already says Wednesday
    ("2026-10-07T12:00:00+00:00", "Tuesday, October 06, 2026 EOD"),        # 08:00 ET Wednesday, before the open
    ("2026-10-10T15:00:00+00:00", "Friday, October 09, 2026 EOD"),         # a Saturday: Friday's close
    ("2026-10-06T15:00:00+00:00", "Tuesday, October 06, 2026 intraday"),   # 11:00 ET, inside the session
])
def test_the_header_and_the_comps_name_the_same_new_york_session(financials, stamp, header):
    financials["_fetched_at_iso"] = stamp
    assert f"Last sync: {header}<" in _html(financials, {})
    assert header.startswith(f"{date.fromisoformat(fin_history._as_of(stamp, None)):%A, %B %d, %Y} ")


def test_an_intraday_fetch_is_not_called_end_of_day(financials):
    financials["_fetched_at_iso"] = "2026-10-06T15:00:00+00:00"
    assert "EOD" not in _html(financials, {})


# ── S5: Quick Remarks are dated; a Refresh shows what the last Fetch scored ─────
SCAN_ASOF = "2026-10-06T21:00:00+00:00"                                    # 17:00 ET Tuesday
TUESDAY_LATE = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc).timestamp()   # 22:00 ET Tuesday


def _record_remarks(remarks, *, run_id, run_at=TUESDAY_LATE, fetched="2026-10-06T21:30:00+00:00"):
    fin_history.record({"financials": {"ticker": "TEST", "fetched_at_iso": fetched}, "modules": {},
                        "quick_remarks": remarks, "warnings": []}, ticker="TEST", run_id=run_id, run_at=run_at,
                       modules=[])


def test_a_scan_card_carries_the_scans_session(monkeypatch):
    monkeypatch.setattr(ts, "get", lambda t: {"name": "Test", "total_points": 6, "signals": []})
    monkeypatch.setattr(ts, "asof", lambda: SCAN_ASOF)
    res = qr.run_quick_remarks("TEST", no_fetch=True)
    assert (res["score"], res["as_of"], res["as_of_note"]) == (6, "2026-10-06", "the last Asymmetric Growth Fetch")


def test_a_score_this_fetch_built_carries_the_payloads_session(monkeypatch):
    monkeypatch.setattr(ts, "get", lambda t: None)
    monkeypatch.setattr(qr, "fetch_technical", lambda t: {})
    monkeypatch.setattr(agh, "score_ticker", lambda t, f, tech, meta=None: {"total_points": 5, "signals": []})
    res = qr.run_quick_remarks("TEST", financials={"_fetched_at_iso": "2026-10-07T02:00:00+00:00"})
    assert (res["score"], res["as_of"], res["as_of_note"]) == (5, "2026-10-06", "this Fetch")


def test_a_refresh_shows_the_remarks_the_last_fetch_scored(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(ts, "get", lambda t: None)
    _record_remarks({"ticker": "TEST", "source": "yfinance", "score": 5, "max": 9, "signals": []}, run_id="r1")
    res = qr.run_quick_remarks("TEST", no_fetch=True)
    assert (res["score"], res["as_of"], res["as_of_note"]) == (5, "2026-10-06", "the last stored Fetch")


def test_a_refresh_skips_a_newer_fetch_that_scored_nothing(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(ts, "get", lambda t: None)
    _record_remarks({"ticker": "TEST", "source": "yfinance", "score": 4, "max": 9, "signals": [],
                     "as_of": "2026-10-05", "as_of_note": "this Fetch"}, run_id="old", run_at=TUESDAY_LATE - 86400)
    _record_remarks({"ticker": "TEST", "source": "yfinance", "score": None}, run_id="new")
    res = qr.run_quick_remarks("TEST", no_fetch=True)
    assert (res["score"], res["as_of"], res["as_of_note"]) == (4, "2026-10-05", "the last stored Fetch")


def test_a_refresh_with_nothing_stored_is_an_undated_empty_card(rawstore_tmp_db, monkeypatch):
    monkeypatch.setattr(ts, "get", lambda t: None)
    res = qr.run_quick_remarks("TEST", no_fetch=True)
    assert res["score"] is None and res["as_of"] is None


@pytest.mark.parametrize("render", [render_quick_remarks_section, render_quick_remarks_html])
def test_the_section_says_when_it_was_scored(render):
    html = render({"ticker": "TEST", "score": 6, "max": 9, "signals": [],
                   "as_of": "2026-10-06", "as_of_note": "the last Asymmetric Growth Fetch"})
    assert "Scored: 2026-10-06 (the last Asymmetric Growth Fetch)" in html
    assert "Scored: N/A" in render({"ticker": "TEST", "score": None, "signals": []})


# ── N1: an unknown ΔNWC leaves the historical FCFF unknown, not ΔNWC = 0 ─────────
def test_the_historical_fcff_needs_a_known_change_in_working_capital(financials):
    rows = run_dcf(financials)["historical_fcff"]
    assert rows[0]["dnwc"] is None and rows[0]["fcff"] is None              # no prior year to difference against
    # 2022: NOPAT 225 x (1 - the model's 50/240 rate) + D&A 45 - capex 45 - ΔNWC (112.5 - 100)
    assert rows[1]["dnwc"] == pytest.approx(12.5)
    assert rows[1]["fcff"] == pytest.approx(225 * (1 - 50 / 240) - 12.5)
    gap = run_dcf(_drop(financials, *NWC_LINES))["historical_fcff"]
    assert gap[2]["dnwc"] is None and gap[2]["fcff"] is None and gap[2]["nopat"] is not None


# ── N3: each percentile row says how many peers it rests on ─────────────────────
def _comps(counts):
    def _q(n):
        return {"p25": 9.0, "p50": 10.0, "p75": 11.0, "count": n} if n else \
            {"p25": None, "p50": None, "p75": None, "count": 0}
    return {"ticker": "TEST", "warnings": [], "pe_only": False, "peers": [],
            "multiples": {k: _q(n) for k, n in zip(("ev_ebitda", "ev_revenue", "pe"), counts)},
            "implied": dict.fromkeys(("ev_ebitda", "ev_revenue", "equity_pe", "per_share_ev_ebitda",
                                      "per_share_ev_revenue", "per_share_pe", "net_debt"))}


def test_the_percentile_rows_carry_their_peer_count(financials):
    html = _html(financials, {"comps": _comps((5, 1, 0))})
    assert "EV/EBITDA (25/50/75 of 5 peers)" in html
    assert "EV/Revenue (25/50/75 of 1 peer)" in html
    assert "P/E (25/50/75 of 0 peers)" in html


# ── N5: the help box names only the source a Fetch reads ───────────────────────
def test_the_help_box_does_not_claim_sec_edgar(financials):
    """Nothing on the Financials Fetch path reads EDGAR (module_financials, app/runners.run_quick): Yahoo only."""
    html = _html(financials, {})
    assert "EDGAR" not in html and "from <b>Yahoo</b> into the store" in html


# ── N6: the stored comps are dated by the run that fetched the peers ────────────
def test_the_stored_comps_are_dated_by_their_run_not_the_cached_target_payload(rawstore_tmp_db):
    """A Fetch reads the target's payload through the cache (it can be a day old) but fetches every peer's .info
    live: the multiples are the run's, so the Refresh's "as of" is the run's session."""
    comps = _comps((3, 3, 3))
    fin_history.record({"financials": {"ticker": "TEST", "fetched_at_iso": "2026-10-05T21:30:00+00:00"},
                        "modules": {"comps": comps}, "quick_remarks": None, "warnings": []},
                       ticker="TEST", run_id="r1", run_at=TUESDAY_LATE, modules=["comps"])
    assert fin_history.last_report("TEST")["as_of"] == "2026-10-06"


# ── N7: the model version hashes only the part of the sector bands a model reads ──
def test_a_sector_band_no_model_reads_does_not_move_the_model_version(monkeypatch):
    """No engine reads wacc_range / growth_range / ebitda_margin_range / exit_multiple_range (#49); the one key a
    model can read is a band's tax_rate (_inputs.default_tax_rate). Only that is hashed."""
    from RevenueChaser.module_financials import config
    bands = copy.deepcopy(config.load_sector_defaults())
    before = fin_history.model_version()
    bands["Technology"]["wacc_range"] = [0.01, 0.02]
    monkeypatch.setattr(config, "load_sector_defaults", lambda: bands)
    assert fin_history.model_version() == before
    bands["Technology"]["tax_rate"] = 0.15
    assert fin_history.model_version() != before
