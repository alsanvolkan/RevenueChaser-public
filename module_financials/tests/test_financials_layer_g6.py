"""Financials on the data layer (G6, build plan PR 15): statements from the SEC facts, the risk-free rate from
``market_series``.

The DCF, the three-statement model and the credit grades read the annual statements. On the fundamentals layer
those are the SEC's (``core/fundamentals_layer``), not Yahoo's: a figure the SEC facts do not carry (pre-tax income,
the tax provision, interest expense, current assets and liabilities) stays empty, so the engines' labelled defaults
show (review S1). EBITDA is operating income plus depreciation when the year has both. Price, shares and beta are
still Yahoo's until the prices store is in (G2). The ten-year yield is read from ``market_series`` (DGS10, FRED;
review S2), the old ``macro_series`` row only while the market file is not there.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core.tests.test_fundamentals_layer import CIK, TICKER, built, grw_facts  # noqa: F401
from RevenueChaser.rcdata import ops, serving
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401  (fixtures)


def _serve_dgs10(points):
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE market_series (series_id TEXT, obs_date DATE, source TEXT, value DOUBLE, "
                      "frequency TEXT, session_date DATE, source_as_of DATE, fetched_at DOUBLE, run_id TEXT)")
        for day, value in points:
            b.con.execute("INSERT INTO market_series VALUES ('DGS10', ?, 'fred', ?, 'daily', ?, ?, 1.0, 'r1')",
                          [day, value, day, day])
        b.swap({"market_series": len(points)})
    ops.record_check("market_series", "changed", run_id="r1")


def _clock(monkeypatch):
    from RevenueChaser.core import calendar
    monkeypatch.setattr(calendar, "_now", lambda: datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc))


def test_the_risk_free_rate_is_read_from_market_series(monkeypatch, market_roots):  # noqa: F811
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core import rawstore
    _clock(monkeypatch)
    rawstore.write_rows("macro_series", [{"series_id": "DGS10", "obs_date": date(2026, 10, 27), "source": "fred",
                                          "value": 3.0}])
    assert fm._stored_risk_free() == {"value": pytest.approx(0.03), "as_of": "2026-10-27"}    # no market file yet
    _serve_dgs10([("2026-10-23", 4.1), ("2026-10-26", 4.2)])
    assert fm._stored_risk_free() == {"value": pytest.approx(0.042), "as_of": "2026-10-26"}


def test_a_stale_market_series_yield_is_not_used(monkeypatch, market_roots):  # noqa: F811
    import RevenueChaser.module_financials.run as fm
    _clock(monkeypatch)
    _serve_dgs10([("2026-10-13", 4.1)])                                         # fifteen days old
    assert fm._stored_risk_free() is None


def test_the_statements_are_the_secs_on_the_layer(built):  # noqa: F811
    import RevenueChaser.module_financials.run as fm
    annual, meta = fm._sec_statements(TICKER)
    assert [r["fiscal_year"] for r in annual] == ["2024", "2025"]
    assert [r["period_end"] for r in annual] == ["2024-12-31", "2025-12-31"]
    newest = annual[-1]
    assert newest["revenue"] == 140.0 + 150 + 160 + 170                          # 2025's four quarters
    assert newest["capex"] == newest["capital_expenditure"] and newest["total_debt"] == 320.0
    # Nothing the SEC facts do not carry is filled: the engines' labelled defaults show (S1).
    for missing in ("ebitda", "pretax_income", "tax_provision", "interest_expense", "current_assets"):
        assert newest.get(missing) is None
    assert meta["newest_filed"] == "2026-08-01"


def test_ebitda_is_operating_income_plus_depreciation():
    import RevenueChaser.module_financials.run as fm
    row = fm._financials_record({"fiscal_year": "2025", "revenue": 100.0, "ebit": 20.0, "depreciation": 5.0,
                                 "period_ends": {"revenue": "2025-12-31"}})
    assert row["ebitda"] == 25.0 and row["period_end"] == "2025-12-31"
    assert "ebitda" not in fm._financials_record({"fiscal_year": "2025", "revenue": 100.0, "ebit": 20.0})


def test_off_the_layer_there_are_no_sec_statements(monkeypatch):
    import RevenueChaser.module_financials.run as fm
    assert fm._sec_statements(TICKER) is None                                  # no fundamentals.duckdb


def test_a_run_on_the_layer_models_the_sec_statements(built, monkeypatch, tmp_path):  # noqa: F811
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core.providers import cache as cache_manager
    _clock(monkeypatch)
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    # Yahoo's payload: its own statements (which the run no longer models) plus price, shares and beta.
    payload = {"ticker": TICKER, "info": {"longName": "Growth Co."}, "shares_outstanding": 50.0,
               "market_cap": 5000.0, "beta": 1.1, "current_price": 100.0, "currency": "USD", "source": "yfinance",
               "annual": [{"fiscal_year": "2024", "revenue": 1.0, "ebitda": 0.5, "ebit": 0.4, "depreciation": 0.1,
                           "capital_expenditure": 0.1, "total_debt": 1.0, "cash": 1.0}]}     # older than the SEC's
    cache_manager.save(TICKER, payload)
    out = fm.run(TICKER, modules=["dcf"], output="none", no_fetch=True)["results"]
    years = [r["fiscal_year"] for r in out["financials"]["annual"]]
    assert years == ["2024", "2025"] and "SEC" in out["financials"]["source"]
    dcf = out["modules"]["dcf"]
    assert dcf["base_year"]["revenue"] == out["financials"]["annual"][-1]["revenue"]
    assert "ebitda_margin" in dcf["base_year"]["defaults"]      # GRW tags no operating income


# ── which statements a run models: the SEC's unless Yahoo's same year gives an input the SEC's lacks ──

_FULL = {"revenue": 100.0, "ebitda": 30.0, "ebit": 25.0, "depreciation": 5.0, "capex": 4.0,
         "receivables": 10.0, "inventory": 5.0, "payables": 8.0}


def _year(fy, drop=(), **extra):
    return {"fiscal_year": str(fy), **{k: v for k, v in _FULL.items() if k not in drop}, **extra}


@pytest.mark.parametrize("sec, yahoo, used, missing", [
    ([_year(2025)], [_year(2025)], "sec", []),                                                  # both whole
    ([_year(2025, drop=("ebitda", "depreciation"))], [_year(2025)], "yahoo", ["depreciation", "ebitda"]),  # MSFT
    ([_year(2025, drop=("inventory",))], [_year(2025, drop=("receivables", "inventory", "payables"),
                                                current_assets=50.0, current_liabilities=30.0)], "yahoo",
     ["working capital"]),                                                                      # a software filer
    ([_year(2013)], [_year(2026)], "yahoo", ["the fiscal year 2026"]),                          # TM: SEC years stale
    # The SEC's year is newer: its statements are modelled, and what they lack is named (Opus review S1).
    ([_year(2026, drop=("depreciation",))], [_year(2025)], "sec", ["depreciation"]),
    ([_year(2025, drop=("depreciation",))], [_year(2025, drop=("depreciation",))], "sec", ["depreciation"]),
    # The years compare on revenue (Opus review S2): a newer year with only a balance sheet is no newer year.
    ([_year(2025, drop=("depreciation",)), {"fiscal_year": "2026", "cash": 1.0}], [_year(2025)], "yahoo",
     ["depreciation"]),
    ([_year(2024)], [_year(2025), {"fiscal_year": "2026", "cash": 1.0}], "yahoo", ["the fiscal year 2025"]),
    ([_year(2025)], [], "sec", []),                                                             # no Yahoo payload
])
def test_the_statements_a_run_models(sec, yahoo, used, missing):
    import RevenueChaser.module_financials.run as fm
    assert fm._statements_choice(sec, yahoo) == (used, missing)


def test_the_sec_statements_end_on_the_newest_year_with_revenue(monkeypatch):
    # Opus review S2: a newest year the layer serves with only balance-sheet fields is not the DCF's base year.
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core import fundamentals_layer
    years = [{"fiscal_year": "2024", "revenue": 90.0}, {"fiscal_year": "2025", "revenue": 100.0},
             {"fiscal_year": "2026", "cash": 5.0}]
    monkeypatch.setattr(fundamentals_layer, "reads_layer", lambda: True)
    monkeypatch.setattr(fundamentals_layer, "records", lambda t: (years, [], {"newest_filed": "2026-08-01"}))
    annual, _meta = fm._sec_statements(TICKER)
    assert [r["fiscal_year"] for r in annual] == ["2024", "2025"]


def test_a_run_on_the_secs_statements_names_what_they_lack(built, monkeypatch, tmp_path):  # noqa: F811
    # Opus review S1: the SEC's newer year wins, and the Source line names the inputs that run on a default.
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core.providers import cache as cache_manager
    _clock(monkeypatch)
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    cache_manager.save(TICKER, {"ticker": TICKER, "info": {"longName": "Growth Co."}, "shares_outstanding": 50.0,
                                "market_cap": 5000.0, "beta": 1.1, "current_price": 100.0, "currency": "USD",
                                "source": "yfinance", "annual": [_year(2024)]})
    out = fm.run(TICKER, modules=["dcf"], output="none", no_fetch=True)["results"]
    fin = out["financials"]
    assert fin["statements_meta"]["used"] == "sec" and "ebitda" in fin["statements_meta"]["sec_lacks"]
    assert fin["source"].startswith("SEC company facts (statements") and "defaults: " in fin["source"]
    assert "ebitda" in fin["source"].split("defaults: ")[1]


def test_a_run_keeps_yahoos_statements_and_says_why(built, monkeypatch, tmp_path):  # noqa: F811
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core.providers import cache as cache_manager
    _clock(monkeypatch)
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    yahoo = [{"fiscal_year": "2025", "revenue": 10.0, "ebitda": 5.0, "ebit": 4.0, "depreciation": 1.0,
              "capital_expenditure": 1.0, "capex": 1.0, "total_debt": 1.0, "cash": 1.0}]
    cache_manager.save(TICKER, {"ticker": TICKER, "info": {"longName": "Growth Co."}, "shares_outstanding": 50.0,
                                "market_cap": 5000.0, "beta": 1.1, "current_price": 100.0, "currency": "USD",
                                "source": "yfinance", "annual": yahoo})
    out = fm.run(TICKER, modules=["dcf"], output="none", no_fetch=True)["results"]
    assert out["financials"]["annual"][-1]["revenue"] == 10.0                    # GRW's SEC year has no EBIT
    assert "Yahoo (statements" in out["financials"]["source"] and "ebitda" in out["financials"]["source"]
    assert out["financials"]["statements_meta"]["used"] == "yahoo"


def test_the_model_version_moves_with_the_statements():
    from RevenueChaser.module_financials import history
    assert history.SELECTION_VERSION == "4"


def test_a_bank_names_no_working_capital_gap():
    # Opus review N4: no model of a Financial Services target reads working capital (P/E only), so the choice
    # neither counts it nor names it there.
    import RevenueChaser.module_financials.run as fm
    sec = [_year(2025, drop=("receivables", "inventory", "payables"))]
    yahoo = [_year(2025)]
    assert fm._statements_choice(sec, yahoo) == ("yahoo", ["working capital"])
    assert fm._statements_choice(sec, yahoo, bank=True) == ("sec", [])


def test_a_run_tells_the_choice_when_the_target_is_a_bank(built, monkeypatch, tmp_path):  # noqa: F811
    import types
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core import universe
    from RevenueChaser.core.providers import cache as cache_manager
    _clock(monkeypatch)
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(universe, "gics_of", lambda t, g: types.SimpleNamespace(sector="Financial Services"))
    seen = []
    monkeypatch.setattr(fm, "_statements_choice", lambda sec, yahoo, bank=False: seen.append(bank) or ("sec", []))
    cache_manager.save(TICKER, {"ticker": TICKER, "info": {"longName": "Growth Co."}, "shares_outstanding": 50.0,
                                "market_cap": 5000.0, "beta": 1.1, "current_price": 100.0, "currency": "USD",
                                "source": "yfinance", "annual": [_year(2024)]})
    fm.run(TICKER, modules=["dcf"], output="none", no_fetch=True)
    assert seen == [True]
