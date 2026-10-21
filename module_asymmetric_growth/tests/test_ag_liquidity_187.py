"""#187: AG net debt and runway count short-term investments next to cash.

The investments come from the SAME annual record that supplies ``cash``, never from an
older year. ``total_debt`` / ``total_assets`` / FCF come from that anchor year too (#150 U8).
Synthetic companyfacts, no network.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core.providers import sec_edgar
from RevenueChaser.module_asymmetric_growth.engine import score_ticker

_CASH = "CashAndCashEquivalentsAtCarryingValue"


def _fy(year: str, val: float) -> dict:
    return {"start": f"{year}-01-01", "end": f"{year}-12-31", "val": val, "form": "10-K",
            "fp": "FY", "filed": f"{int(year) + 1}-02-01"}


def _inst(year: str, val: float) -> dict:
    return {"end": f"{year}-12-31", "val": val, "form": "10-K", "fp": "FY",
            "filed": f"{int(year) + 1}-02-01"}


def _facts(*, cash, sti=None, debt=300.0, assets=1000.0, ocf=-90.0, capex=10.0) -> dict:
    """``cash`` / ``sti`` map year -> value; the flow facts are given for the years of ``cash``."""
    years = sorted(set(cash) | set(sti or {}) | {"2023"})

    def flow(val):
        return {"units": {"USD": [_fy(y, val) for y in years]}}

    def inst(series):
        return {"units": {"USD": [_inst(y, v) for y, v in series.items()]}}

    gaap = {
        "Revenues": flow(800.0),
        "NetCashProvidedByUsedInOperatingActivities": flow(ocf),
        "PaymentsToAcquirePropertyPlantAndEquipment": flow(capex),
        "LongTermDebtNoncurrent": inst({y: debt for y in years}),
        "Assets": inst({y: assets for y in years}),
        _CASH: inst(cash),
    }
    if sti:
        gaap["ShortTermInvestments"] = inst(sti)
    return {"entityName": "Liquidity Co", "facts": {"us-gaap": gaap}}


def _card(facts: dict) -> dict:
    financials = {"info": {"country": "United States", "exchange": "NMS", "longName": "Liquidity Co",
                           "industry": "Biotechnology"},
                  "annual": sec_edgar.extract_annual(facts), "market_cap": 5e9, "source": "sec_edgar"}
    tech = {"price": 100.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6, "above_sma50": True}
    return score_ticker("LIQD", financials, tech)


def _sub(card: dict, param: str) -> dict:
    survival = next(s for s in card["signals"] if s["key"] == "survival")
    return next(s for s in survival["subs"] if s["param"] == param)


def test_net_debt_and_runway_count_the_short_term_investments():
    card = _card(_facts(cash={"2023": 100.0}, sti={"2023": 400.0}))  # debt 300, assets 1,000, FCF -100
    sub = _sub(card, "net_debt_assets")
    assert sub["value"] == pytest.approx((300.0 - 500.0) / 1000.0)  # -20%
    assert sub["passed"] is True
    assert card["runway_years"] == pytest.approx(500.0 / 100.0)
    assert _sub(card, "runway")["passed"] is True


def test_cash_alone_would_have_failed_the_same_company():
    card = _card(_facts(cash={"2023": 100.0}))  # no investments: (300 - 100) / 1,000 = 20%
    sub = _sub(card, "net_debt_assets")
    assert sub["value"] == pytest.approx(0.20)
    assert sub["passed"] is False
    assert card["runway_years"] == pytest.approx(1.0)


def test_tgtx_shaped_pin():
    kw = dict(debt=245.6e6, assets=1063e6)
    with_sti = _sub(_card(_facts(cash={"2023": 79.1e6}, sti={"2023": 62.8e6}, **kw)), "net_debt_assets")
    cash_only = _sub(_card(_facts(cash={"2023": 79.1e6}, **kw)), "net_debt_assets")
    assert with_sti["value"] == pytest.approx(0.0976, abs=1e-4)  # 103.7M / 1,063M
    assert with_sti["passed"] is True
    assert cash_only["value"] == pytest.approx(0.1566, abs=1e-4)  # 166.5M / 1,063M
    assert cash_only["passed"] is False


def test_investments_of_an_older_year_are_not_borrowed():
    card = _card(_facts(cash={"2022": 100.0, "2023": 100.0}, sti={"2022": 400.0}))
    sub = _sub(card, "net_debt_assets")
    assert sub["value"] == pytest.approx(0.20)  # cash only: the 2022 investments were sold since
    assert sub["passed"] is False
    assert card["runway_years"] == pytest.approx(1.0)


def test_an_older_years_cash_and_investments_are_not_borrowed():
    # 2023 has no cash fact at all. Since #150 U8 the score reads the 2023 record only: no cash, no liquidity.
    card = _card(_facts(cash={"2022": 100.0}, sti={"2022": 400.0}))
    assert _sub(card, "net_debt_assets")["value"] is None
    assert card["runway_years"] is None


def test_a_missing_sti_changes_nothing_against_the_cash_only_formula():
    card = _card(_facts(cash={"2023": 150.0}, debt=300.0, assets=1000.0))
    assert _sub(card, "net_debt_assets")["value"] == pytest.approx((300.0 - 150.0) / 1000.0)
    assert card["runway_years"] == pytest.approx(150.0 / 100.0)


def test_a_yfinance_shaped_annual_record_is_cash_only():
    # yfinance records have no investments key and their ``cash`` may already include them.
    annual = [{"fiscal_year": "2023", "revenue": 800.0, "cash": 150.0, "total_debt": 300.0,
               "total_assets": 1000.0, "operating_cash_flow": -90.0, "capex": 10.0}]
    financials = {"info": {"country": "United States", "exchange": "NMS", "longName": "Yahoo Co"},
                  "annual": annual, "market_cap": 5e9, "source": "yfinance"}
    card = score_ticker("YHOO", financials, {"price": 10.0, "dist_52w_pct": 0.05, "vol_ratio": 1.6,
                                             "above_sma50": True})
    assert _sub(card, "net_debt_assets")["value"] == pytest.approx(0.15)
    assert card["runway_years"] == pytest.approx(1.5)


def test_no_cash_means_no_net_debt_even_with_investments():
    card = _card(_facts(cash={}, sti={"2023": 400.0}))
    assert _sub(card, "net_debt_assets")["value"] is None
    assert card["runway_years"] is None


def test_the_card_gets_no_new_liquidity_key():
    card = _card(_facts(cash={"2023": 100.0}, sti={"2023": 400.0}))
    assert [k for k in card if "liquid" in k or "invest" in k or "sti" == k] == []
