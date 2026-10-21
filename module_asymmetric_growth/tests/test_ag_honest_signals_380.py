"""#380 AG W1 S1 honest signals (AG-1, AG-14, #66 gates): the C18 rules of ``core.fiscal_year`` on the AG card.

- growth reads ``fiscal_year.growth``: a prior year that is not positive gives an unknown growth (flag
  ``prior_nonpositive``); growth above +500 % keeps its value, is flagged ``suspicious_growth`` and leaves the sort key;
- gross margin reads ``fiscal_year.gross_margin``: gross profit above revenue, or revenue not positive, is unknown and
  flagged;
- dilution compares the same share basis in both years (diluted, else basic); no common basis is unknown and flagged
  ``mixed_share_basis``;
- card O-2 ("Dışla", 2026-10-15): a US name whose newest annual record ended more than ``STALE_DAYS`` (470) before
  the run day gets no card; the funnel lists it as ``stale_annual`` (by design). A name whose newest record is fresh
  but whose last revenue figure is older keeps its card: it reads the newest record (revenue unknown), is flagged
  ``no_recent_revenue`` and gets no tier (the PC measurement on 4d2782a).
Synthetic records, no network, temp store.
"""
from __future__ import annotations

import copy
from datetime import date

import pytest

from RevenueChaser.core import fiscal_year, funnel, ledger, rawstore
from RevenueChaser.module_asymmetric_growth import engine, freeze
from RevenueChaser.module_asymmetric_growth.engine import score_ticker
from RevenueChaser.module_asymmetric_growth.tests.test_ag_info_only_150 import _ANNUAL, _QUOTE, _edgar
from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import seed_momentum

_TECH = {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6, "above_sma50": True}


def _rec(fy, revenue, **kw) -> dict:
    base = {"fiscal_year": fy, "revenue": revenue, "gross_profit": revenue * 0.5 if revenue else None}
    base.update(kw)
    return base


def _card(annual) -> dict:
    fin = {"info": {"country": "United States", "exchange": "NMS", "longName": "Honest Co"},
           "annual": annual, "market_cap": 5e9, "source": "sec_edgar"}
    return score_ticker("HNST", fin, _TECH, spine={})


def _sub(card: dict, param: str) -> dict:
    return next(s for sig in card["signals"] for s in sig["subs"] if s["param"] == param)


# ── growth ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prior", [-50.0, 0.0])
def test_a_prior_year_that_is_not_positive_gives_unknown_growth(prior):
    card = _card([_rec(2023, 80.0), _rec(2024, prior), _rec(2025, 150.0)])
    assert card["rev_growth"] is None
    assert _sub(card, "rev_growth")["passed"] is None                 # unknown, not a fail (#151)
    assert "prior_nonpositive" in card["flags"]


def test_growth_above_500_pct_keeps_its_value_and_is_flagged():
    card = _card([_rec(2024, 100.0), _rec(2025, 700.0)])              # +600 %
    assert card["rev_growth"] == pytest.approx(6.0)
    assert "suspicious_growth" in card["flags"]
    assert _sub(card, "rev_growth")["passed"] is True                 # the rule still reads the value


def test_growth_at_500_pct_is_not_flagged():
    card = _card([_rec(2024, 100.0), _rec(2025, 600.0)])              # +500 % exactly
    assert card["rev_growth"] == pytest.approx(5.0)
    assert "suspicious_growth" not in card["flags"]


def test_a_clean_card_has_no_flags():
    card = _card([_rec(2023, 100.0), _rec(2024, 130.0), _rec(2025, 190.0)])
    assert card["flags"] == []


def test_suspicious_growth_is_kept_out_of_the_sort_key():
    cards = [{"ticker": "AAA", "total_points": 9, "rev_growth": 6.0, "flags": ["suspicious_growth"]},
             {"ticker": "BBB", "total_points": 9, "rev_growth": 0.5, "flags": []},
             {"ticker": "CCC", "total_points": 8, "rev_growth": 0.9, "flags": []}]
    engine._sort_cards(cards)
    assert [c["ticker"] for c in cards] == ["BBB", "AAA", "CCC"]      # points still lead


# ── gross margin ─────────────────────────────────────────────────────────────

def test_gross_profit_above_revenue_is_unknown_and_flagged():
    card = _card([_rec(2024, 100.0), _rec(2025, 150.0, gross_profit=900.0)])
    assert card["gross_margin"] is None
    assert _sub(card, "gross_margin")["passed"] is None
    assert "gross_profit_exceeds_revenue" in card["flags"]


def test_revenue_that_is_not_positive_gives_unknown_margin():
    card = _card([_rec(2024, 100.0), _rec(2025, -10.0, gross_profit=5.0)])
    assert card["gross_margin"] is None
    assert "revenue_nonpositive" in card["flags"]


# ── dilution: the same share basis in both years ─────────────────────────────

def test_diluted_against_basic_shares_is_unknown_and_flagged():
    card = _card([_rec(2024, 100.0, diluted_shares=100.0), _rec(2025, 150.0, basic_shares=150.0)])
    assert card["dilution_yoy"] is None
    assert card["dilution_warning"] is False
    assert "mixed_share_basis" in card["flags"]


def test_basic_shares_in_both_years_are_compared_when_diluted_is_missing_in_one():
    card = _card([_rec(2024, 100.0, basic_shares=100.0),
                  _rec(2025, 150.0, diluted_shares=200.0, basic_shares=130.0)])
    assert card["dilution_yoy"] == pytest.approx(0.30)
    assert card["dilution_warning"] is True
    assert "mixed_share_basis" not in card["flags"]


def test_diluted_shares_lead_when_both_years_have_them():
    card = _card([_rec(2024, 100.0, diluted_shares=100.0, basic_shares=90.0),
                  _rec(2025, 150.0, diluted_shares=110.0, basic_shares=135.0)])
    assert card["dilution_yoy"] == pytest.approx(0.10)


@pytest.mark.parametrize("prior", [0.0, -5.0])
def test_a_prior_share_count_that_is_not_positive_gives_unknown_dilution(prior):
    card = _card([_rec(2024, 100.0, diluted_shares=prior), _rec(2025, 150.0, diluted_shares=110.0)])
    assert card["dilution_yoy"] is None


def test_no_share_figure_at_all_is_unknown_without_a_flag():
    card = _card([_rec(2024, 100.0), _rec(2025, 150.0)])
    assert card["dilution_yoy"] is None and card["flags"] == []


# ── card O-2: a stale anchor gets no card and a funnel row ───────────────────

RUN_DAY = date(2026, 10, 15)


def _dated(rows, end_year: int, month_day: str = "12-31"):
    out = copy.deepcopy(rows)
    for k, r in enumerate(out):
        r["period_ends"] = {"revenue": f"{end_year - len(out) + 1 + k}-{month_day}"}
    return out


@pytest.fixture
def scan(rawstore_tmp_db, monkeypatch):
    """FRESH ended 2025-12-31 (288 days before the run day); STALE ended 2024-06-30 (837 days); UNDATED has no
    ``period_ends`` (records stored before #239), so it is not judged; FORN is stale and foreign: foreign wins."""
    rawstore.write_rows("edgar_financials", [
        _edgar("FRESH", _dated(_ANNUAL, 2025)), _edgar("STALE", _dated(_ANNUAL, 2024, "06-30")),
        _edgar("UNDATED"), _edgar("FORN", _dated(_ANNUAL, 2024, "06-30"), is_us=False)])
    rows = [{"ticker": t, "quote": dict(_QUOTE), "market_cap": 5e9} for t in ("FRESH", "STALE", "UNDATED", "FORN")]
    seed_momentum(["FRESH", "STALE", "UNDATED", "FORN"])      # the momentum signal comes from D01 (#380 S4)
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    monkeypatch.setattr(engine, "_run_day", lambda: RUN_DAY)
    return engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)


def _steps(result):
    return {s["key"]: s for s in result["funnel_steps"]}


def test_a_stale_anchor_gets_no_card(scan):
    assert sorted(c["ticker"] for c in scan["cards"]) == ["FRESH", "UNDATED"]


def test_the_funnel_lists_the_stale_anchor_by_design(scan):
    step = _steps(scan)["stale_annual"]
    assert step["count"] == 1 and step["tickers"] == ["STALE"] and step["by_design"] is True
    assert "470" in step["label"]
    assert _steps(scan)["foreign"]["tickers"] == ["FORN"]
    assert funnel.validate(scan["funnel_steps"]) == []


def test_the_stale_line_is_the_c18_limit(monkeypatch, rawstore_tmp_db):
    """One day inside the limit is a card; one day past it is not."""
    end = date.fromordinal(RUN_DAY.toordinal() - fiscal_year.STALE_DAYS)
    annual = copy.deepcopy(_ANNUAL)
    annual[-1]["period_ends"] = {"revenue": end.isoformat()}
    meta = {"ticker": "EDGE", "quote": dict(_QUOTE), "market_cap": 5e9}
    preload = {"edgar": {"EDGE": (annual, {"is_us": True})}, "payload": {}, "themes": {}, "drops": engine._Drops(),
               "as_of": RUN_DAY}
    assert engine.enrich_and_score("EDGE", meta, True, {}, preload) is not None
    preload["as_of"] = date.fromordinal(RUN_DAY.toordinal() + 1)
    assert engine.enrich_and_score("EDGE", meta, True, {}, preload) is None
    assert preload["drops"].by_reason == {"stale_annual": ["EDGE"]}


# ── O-2 fix: the filer's newest record decides, not its revenue anchor ───────
# PC measurement on 4d2782a (2026-10-15): 341 of the 384 stale_annual names had a newest record within 470 days and
# fell out because their newer years carry no revenue figure, so the revenue anchor fell back years (AKAM: FY2017).

def _gap(newest_year: int, month_day: str = "12-31") -> list[dict]:
    """Revenue only in FY2017, like AKAM; FY2018 onwards carry the cash and balance-sheet fields only."""
    first = dict(copy.deepcopy(_ANNUAL[0]), fiscal_year=2017, period_ends={"revenue": f"2017-{month_day}"})
    return [first] + [{"fiscal_year": y, "operating_cash_flow": 300.0, "capex": 50.0, "total_debt": 10.0,
                       "cash": 200.0, "total_assets": 1000.0, "diluted_shares": 100.0,
                       "period_ends": {"total_assets": f"{y}-{month_day}", "operating_cash_flow": f"{y}-{month_day}"}}
                      for y in range(2018, newest_year + 1)]


@pytest.fixture
def gap_scan(rawstore_tmp_db, monkeypatch):
    """GAP: newest record 2025-12-31 (fresh), revenue last in FY2017. OLDGAP: the same, but its newest record ended
    2024-06-30 (837 days): a stale filer. FRESH: an ordinary name."""
    rawstore.write_rows("edgar_financials", [
        _edgar("FRESH", _dated(_ANNUAL, 2025)), _edgar("GAP", _gap(2025)), _edgar("OLDGAP", _gap(2024, "06-30"))])
    rows = [{"ticker": t, "quote": dict(_QUOTE), "market_cap": 5e9} for t in ("FRESH", "GAP", "OLDGAP")]
    seed_momentum(["FRESH", "GAP", "OLDGAP"])                 # the momentum signal comes from D01 (#380 S4)
    monkeypatch.setattr(engine, "fetch_universe_stats", lambda *a, **k: (rows, {}))
    import RevenueChaser.module_sec_13f.engine as s13
    monkeypatch.setattr(s13, "load_totals", lambda: {})
    monkeypatch.setattr(engine._spine, "spine_gics", lambda *a, **k: {})
    monkeypatch.setattr(engine._spine, "excluded_tickers", lambda: set())
    monkeypatch.setattr(engine, "_run_day", lambda: RUN_DAY)
    return engine.run_asymmetric_growth_hunter(None, max_workers=1, no_fetch=True)


def test_a_fresh_filer_with_an_old_revenue_figure_keeps_its_card(gap_scan):
    cards = {c["ticker"]: c for c in gap_scan["cards"]}
    assert sorted(cards) == ["FRESH", "GAP"]
    gap = cards["GAP"]
    assert (gap["anchor_fy"], gap["prior_fy"]) == (2025, 2024)      # the newest record, not FY2017
    assert gap["rev_growth"] is None and gap["gross_margin"] is None
    assert "no_recent_revenue" in gap["flags"]
    assert gap["total_points"] == 6 and gap["tier"] is None          # survival 3 + momentum 3, still no tier
    assert gap["earnings_date"] is not None                          # the Earnings filter keeps the name


def test_only_a_stale_newest_record_is_dropped(gap_scan):
    step = _steps(gap_scan)["stale_annual"]
    assert step["tickers"] == ["OLDGAP"] and step["by_design"] is True
    assert funnel.validate(gap_scan["funnel_steps"]) == []


def test_the_summary_counts_the_old_revenue_cards(gap_scan):
    assert gap_scan["summary"]["no_recent_revenue"] == 1


def test_score_ticker_judges_the_revenue_anchor_only_with_a_run_day():
    fin = {"info": {"country": "United States", "exchange": "NMS", "longName": "Gap Co"},
           "annual": _gap(2025), "market_cap": 5e9, "source": "sec_edgar"}
    card = score_ticker("GAP", fin, _TECH, spine={}, as_of=RUN_DAY)
    assert card["anchor_fy"] == 2025 and "no_recent_revenue" in card["flags"] and card["tier"] is None
    plain = score_ticker("GAP", fin, _TECH, spine={})                # tests and research scripts: nothing judged
    assert plain["anchor_fy"] == 2017 and "no_recent_revenue" not in plain["flags"]


def test_a_fresh_revenue_anchor_behind_the_newest_record_is_kept():
    """The newest revenue figure ended 2025-12-31 (288 days) and the next record, ended 2026-06-30, has none: the
    card reads the revenue year, unflagged."""
    annual = _dated(_ANNUAL, 2025) + [{"fiscal_year": 2024, "total_assets": 1000.0,      # _ANNUAL keys 2021-2023
                                       "period_ends": {"total_assets": "2026-06-30"}}]
    fin = {"info": {"country": "United States", "exchange": "NMS"}, "annual": annual, "market_cap": 5e9}
    card = score_ticker("KEEP", fin, _TECH, spine={}, as_of=RUN_DAY)
    assert card["anchor_fy"] == 2023 and "no_recent_revenue" not in card["flags"]
    assert card["rev_growth"] == pytest.approx(190.0 / 130.0 - 1)


def test_the_board_explains_the_old_revenue_flag(tmp_path, rawstore_tmp_db):
    from RevenueChaser.core.tests.board_js import run_board_js
    from RevenueChaser.module_asymmetric_growth import report
    from RevenueChaser.module_asymmetric_growth.tests.test_ag_payload_route_380 import _card as _stored, _result

    result = _result([_stored("GAP", tier=None, flags=["no_recent_revenue"], model_version=freeze.model_version())])
    log = run_board_js(report.render_asymmetric_growth_hunter_html(result), "setTier('all');\n"
                       "console.log(JSON.stringify({grid: _els['grid'].innerHTML}));", tmp_path,
                       payload=report.board_payload(result))
    assert "No revenue figure in the last 470 days" in log["grid"]
    assert "no_recent_revenue" not in log["grid"]                    # the text, not the key


# ── the ledger: the rules are part of the model version ──────────────────────

def test_the_rules_are_in_the_effective_config():
    rules = freeze.effective_config()["RULES"]
    assert (rules["stale_days"], rules["suspicious_growth"]) == (fiscal_year.STALE_DAYS, fiscal_year.SUSPICIOUS_GROWTH)
    assert freeze.model_version() == ledger.model_version(engine.SELECTION_VERSION, freeze.effective_config())
