"""The Financials Fable review's fixes (build plan PR 12, group G5). No network.

S1: a base-year input the statements do not give is a default, and the board says so instead of showing it as a
measured number (the #173 convention: missing is unknown, never a silent number).
S4: the header's "Last sync" and the comps' "as of" name the same New York session (core/calendar rule B), and a
fetch inside the session says "intraday", not "EOD".
S5: Quick Remarks say when they were scored, and a Refresh shows what the last Fetch scored instead of an empty card.
S2: the rate inputs say where they come from, and the stored FRED 10-year yield is the risk-free rate when fresh.
N1-N7: the nits, each under its own heading below.
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


def test_a_measured_base_year_and_mix_say_default_nowhere(financials):
    """The rate rows say "default" since review S2 (no stored yield here); the measured rows never do."""
    html = _html(financials, {"dcf": run_dcf(financials)})
    for row in ("EBITDA Margin", "D&amp;A / Revenue", "Capex / Revenue", "NWC / Revenue", "Equity Weight",
                "Debt Weight", "Beta"):
        assert f"{row}</span><span>default" not in html


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


# ── N2: how much of the value is the terminal value, and what each method implies of the other ─
def test_the_dcf_says_the_terminal_share_and_the_implied_multiple_and_growth(financials):
    res = run_dcf(financials)
    tv, v, last = res["terminal_value"], res["valuation"], res["projection"][-1]
    wacc = res["wacc"]["wacc"]
    assert tv["share_of_ev_gordon"] == pytest.approx(tv["pv_gordon"] / v["ev_gordon"])
    assert tv["share_of_ev_exit"] == pytest.approx(tv["pv_exit"] / v["ev_exit"])
    assert tv["implied_exit_multiple"] == pytest.approx(tv["gordon"] / last["ebitda"])
    g = tv["implied_growth"]                                                  # the Gordon growth that gives the exit TV
    assert last["fcff"] * (1 + g) / (wacc - g) == pytest.approx(tv["exit_multiple"])
    html = _html(financials, {"dcf": res})
    for row in ("Terminal share of EV (Gordon / Exit)", "Exit multiple Gordon implies", "Growth the exit multiple implies"):
        assert row in html


def test_a_negative_last_free_cash_flow_implies_no_growth(financials):
    """The Opus review's S3: with the last FCFF below zero the formula gives a growth above the WACC (35% here, from
    a 90% capex), which no Gordon model admits. Unknown, not an impossible number."""
    res = run_dcf(financials, {"capex_pct_rev": 0.9})
    tv, last = res["terminal_value"], res["projection"][-1]
    assert last["fcff"] < 0 < tv["exit_multiple"]
    assert tv["implied_growth"] is None


def test_no_gordon_value_means_no_implied_multiple(financials):
    tv = run_dcf(financials, {"terminal_growth": 0.5})["terminal_value"]
    assert tv["gordon"] is None and tv["implied_exit_multiple"] is None and tv["share_of_ev_gordon"] is None
    assert tv["share_of_ev_exit"] is not None


def test_a_non_positive_ebitda_or_ev_has_no_ratio(financials):
    tv = run_dcf(financials, {"ebitda_margin": -0.1})["terminal_value"]
    assert tv["implied_exit_multiple"] is None and tv["share_of_ev_exit"] is None


# ── S2: the rate inputs say where they come from; the stored 10-year yield is the risk-free rate ─
def test_without_a_stored_yield_the_rates_are_labelled_defaults(financials):
    w = run_dcf(financials)["wacc"]
    assert (w["risk_free"], w["risk_free_source"]) == (0.04, "default")
    assert (w["equity_risk_premium"], w["cost_of_debt"], w["beta_source"]) == (0.05, 0.05, "Yahoo")
    html = _html(financials, {"dcf": run_dcf(financials)})
    for row in ("Risk-free rate</span><span>default 4.00%", "Equity Risk Premium</span><span>default 5.0%",
                "Cost of Debt (Kd, before tax)</span><span>default 5.0%"):
        assert row in html


def test_the_stored_ten_year_yield_is_the_risk_free_rate(financials):
    financials["risk_free_rate"] = {"value": 0.0425, "as_of": "2026-10-27"}
    res = run_dcf(financials)
    w = res["wacc"]
    assert (w["risk_free"], w["risk_free_source"], w["risk_free_as_of"]) == (0.0425, "stored", "2026-10-27")
    assert w["cost_of_equity"] == pytest.approx(0.0425 + 1.2 * 0.05)
    assert "Risk-free rate</span><span>4.25% (10-year Treasury, FRED, 2026-10-27)" in _html(financials, {"dcf": res})


def test_a_typed_risk_free_rate_wins_over_the_stored_one(financials):
    financials["risk_free_rate"] = {"value": 0.0425, "as_of": "2026-10-27"}
    w = run_dcf(financials, {"risk_free": 0.03})["wacc"]
    assert (w["risk_free"], w["risk_free_source"]) == (0.03, "assumption")


def test_a_missing_beta_is_a_labelled_default(financials):
    f = dict(financials, beta=None)
    res = run_dcf(f)
    assert (res["wacc"]["beta"], res["wacc"]["beta_source"]) == (1.0, "default")
    assert "Beta: assumed 1.00 (not reported)" in res["warnings"]
    assert "Beta</span><span>default 1.00" in _html(f, {"dcf": res})


def test_the_three_statement_interest_rate_is_a_labelled_default(financials):
    res = run_three_statement(financials)
    assert (res["assumptions"]["interest_rate"], res["assumptions"]["interest_rate_source"]) == (0.05, "default")
    assert "Interest rate: default 5.0%" in _html(financials, {"three_statement": res})
    typed = run_three_statement(financials, {"interest_rate": 0.06})
    assert typed["assumptions"]["interest_rate_source"] == "assumption"
    assert "Interest rate: 6.0%" in _html(financials, {"three_statement": typed})


def _store_yield(points):
    from RevenueChaser.core import rawstore
    rawstore.write_rows("macro_series", [{"series_id": "DGS10", "obs_date": date.fromisoformat(d), "source": "fred",
                                          "value": v} for d, v in points])


def test_the_run_reads_the_newest_fresh_stored_yield(rawstore_tmp_db, monkeypatch):
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core import calendar
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc))
    assert fm._stored_risk_free() is None                                     # nothing stored: the default
    _store_yield([("2026-10-26", 4.31), ("2026-10-27", 4.25)])
    assert fm._stored_risk_free() == {"value": pytest.approx(0.0425), "as_of": "2026-10-27"}


def test_a_stale_stored_yield_is_not_used(rawstore_tmp_db, monkeypatch):
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core import calendar
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc))
    _store_yield([("2026-10-13", 4.10)])                                       # fifteen days old
    assert fm._stored_risk_free() is None


def test_a_run_hands_the_stored_yield_to_the_dcf(rawstore_tmp_db, monkeypatch, financials, tmp_path):
    """The wiring: a store-only Refresh reads the yield and the DCF uses it (zero network either way)."""
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core import calendar
    from RevenueChaser.core.providers import cache as cache_manager
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    _store_yield([("2026-10-27", 4.25)])
    cache_manager.save("TEST", dict(copy.deepcopy(financials), info={"longName": "Test Corp."}))
    dcf = fm.run("TEST", modules=["dcf"], output="none", no_fetch=True)["results"]["modules"]["dcf"]
    assert (dcf["wacc"]["risk_free"], dcf["wacc"]["risk_free_source"]) == (pytest.approx(0.0425), "stored")


# ── S3: a bank or an insurer gets no FCFF DCF, no EBITDA credit grade and no WACC grid ──
def _bank(financials):
    return dict(copy.deepcopy(financials), sector="Financial Services")


@pytest.mark.parametrize("engine", ["dcf", "credit", "sensitivity"])
def test_a_financial_services_target_gets_a_p_e_note_not_a_model(financials, engine):
    from RevenueChaser.module_financials.engines.credit_capacity_engine import run_credit_capacity
    from RevenueChaser.module_financials.engines.sensitivity_engine import run_sensitivity
    run = {"dcf": run_dcf, "credit": run_credit_capacity, "sensitivity": run_sensitivity}[engine]
    res = run(_bank(financials))
    assert res["not_applicable"] is True and "valued on P/E only" in res["note"]
    assert not {"wacc", "valuation", "metrics", "grades", "matrix"} & set(res)
    assert run(financials).get("not_applicable") is None                       # any other sector is modelled


def test_the_page_says_why_a_bank_has_no_dcf_credit_or_grid(financials):
    from RevenueChaser.module_financials.engines.credit_capacity_engine import run_credit_capacity
    from RevenueChaser.module_financials.engines.sensitivity_engine import run_sensitivity
    f = _bank(financials)
    html = _html(f, {"dcf": run_dcf(f), "credit": run_credit_capacity(f), "sensitivity": run_sensitivity(f)})
    assert html.count("valued on P/E only (see Comps)") == 3
    assert "WACC Components" not in html and 'class="badge b-' not in html and "g \\ WACC" not in html
