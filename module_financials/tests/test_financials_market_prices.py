"""Financials' price, market cap, shares and beta from the prices store (G6 follow-up), Yahoo's only where the
store has none. Offline: a served ``prices.duckdb`` written from rows (``rcdata/tests/prices_store``)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import pytest

from RevenueChaser.core.tests.test_fundamentals_layer import TICKER, built, grw_facts  # noqa: F401
from RevenueChaser.module_financials import market_inputs as mi
from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.tests import prices_store
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401  (fixtures)

NEWEST = date(2026, 10, 27)
YAHOO = {"shares_outstanding": 50.0, "market_cap": 5000.0, "beta": 1.1, "current_price": 100.0}


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    """Today is the day after the store's newest session, unless a test moves it."""
    _clock(monkeypatch)


def _clock(monkeypatch, now: datetime = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)):
    from RevenueChaser.core import calendar
    monkeypatch.setattr(calendar, "_now", lambda: now)


def _month_end_series(n: int, beta: float, end: date = date(2026, 9, 30)):
    """``n`` month ends up to ``end`` (index level, stock close) where every stock return is 0.002 + beta x the
    index return."""
    import pandas as pd
    months = pd.period_range(end=pd.Period(end, "M"), periods=n, freq="M")
    index, stock = [3000.0], [50.0]
    for i in range(1, n):
        r = 0.03 * ((i * 7) % 5 - 2) / 2          # a deterministic, varied index return
        index.append(index[-1] * (1 + r))
        stock.append(stock[-1] * (1 + 0.002 + beta * r))
    days = [m.to_timestamp(how="end").date() for m in months]
    return list(zip(days, stock)), list(zip(days, index))


def test_the_beta_is_the_slope_of_monthly_returns_on_the_index():
    stock, index = _month_end_series(61, 1.5)
    assert mi.monthly_beta(stock, index, NEWEST) == pytest.approx(1.5)


def test_only_the_last_sixty_complete_months_count():
    stock, index = _month_end_series(90, 1.5)
    # The 29 oldest months move the other way: they fall outside the window and change nothing.
    early = [(d, c * (1 + (0.05 if i % 2 else -0.05))) for i, (d, c) in enumerate(stock[:29])]
    assert mi.monthly_beta(early + stock[29:], index, NEWEST) == pytest.approx(1.5)


def test_the_running_month_is_not_a_return():
    stock, index = _month_end_series(61, 1.5)
    assert mi.monthly_beta(stock + [(date(2026, 10, 20), 1.0)], index + [(date(2026, 10, 20), 3000.0)],
                           NEWEST) == pytest.approx(1.5)


def test_too_few_months_give_no_beta():
    stock, index = _month_end_series(mi.BETA_MIN_MONTHS, 1.5)        # one return short of the minimum
    assert mi.monthly_beta(stock, index, NEWEST) is None


def test_a_missing_month_makes_no_return_across_the_gap():
    stock, index = _month_end_series(61, 1.5)
    gap = stock[:30] + [(d, c * 3) for d, c in stock[31:]]      # month 31 missing, later closes on another level
    assert mi.monthly_beta(gap, index, NEWEST) == pytest.approx(1.5)


def _store(ticker: str = TICKER, *, months: int = 61, price: float = 120.0, own_cap: float | None = 5500.0,
           issuer_cap: float | None = 6000.0, beta: float = 1.5, cap_session: date = NEWEST) -> None:
    stock, index = _month_end_series(months, beta)
    prices_store.put_bars([{"ticker": ticker, "session_date": d, "close": c} for d, c in stock]
                          + [{"ticker": ticker, "session_date": NEWEST, "close": price}]
                          + [{"ticker": "SPX", "security_id": mi.INDEX_ID, "session_date": d, "close": c}
                             for d, c in index])
    if own_cap is not None:
        con = duckdb.connect(str(serving.live_path("prices")))
        try:
            con.execute("INSERT INTO market_cap_daily (security_id, session_date, close, market_cap, "
                        "issuer_market_cap) VALUES (?, ?, ?, ?, ?)",
                        [f"sym:{ticker}", cap_session, price, own_cap, issuer_cap])
            con.execute("CHECKPOINT")
        finally:
            con.close()


def test_the_store_gives_the_price_the_issuers_cap_the_shares_and_the_beta(market_roots):  # noqa: F811
    _store()
    got = mi.from_store(TICKER.lower())
    assert got["current_price"] == 120.0 and got["price_session"] == NEWEST
    assert got["market_cap"] == 6000.0                       # the issuer's, every listed class
    assert got["shares_outstanding"] == pytest.approx(50.0)  # 6000 / 120: the class-equivalent count
    assert got["beta"] == pytest.approx(1.5)


def test_without_an_issuer_sum_the_class_cap_counts(market_roots):  # noqa: F811
    _store(issuer_cap=None)
    assert mi.from_store(TICKER)["market_cap"] == 5500.0


def test_a_funds_cap_is_its_own_not_its_trusts_sum(market_roots):  # noqa: F811
    from RevenueChaser.core import rawstore
    rawstore.ensure_table("security_master")
    rawstore.write_rows("security_master", [{"ticker": TICKER, "security_type": "etp"}])
    _store()                                                 # own 5500, issuer 6000: the trust's CIK files many funds
    got = mi.from_store(TICKER)
    assert got["market_cap"] == 5500.0 and got["shares_outstanding"] == pytest.approx(5500.0 / 120.0)


def test_a_bar_without_a_cap_gives_the_price_alone(market_roots):  # noqa: F811
    _store(own_cap=None, months=12)
    got = mi.from_store(TICKER)
    assert set(got) == {"current_price", "price_session"}


@pytest.mark.parametrize("today, fresh", [(date(2026, 11, 10), True), (date(2026, 11, 11), False)])
def test_a_close_older_than_the_rates_limit_gives_nothing(market_roots, monkeypatch, today, fresh):  # noqa: F811
    # The same 14 days as the risk-free rate on that card, counted from today, not from the store's newest session.
    from RevenueChaser.module_financials.config import RISK_FREE_MAX_AGE_DAYS
    assert (date(2026, 11, 10) - NEWEST).days == RISK_FREE_MAX_AGE_DAYS
    _store()
    _clock(monkeypatch, datetime(today.year, today.month, today.day, 20, 0, tzinfo=timezone.utc))
    got = mi.from_store(TICKER)
    assert (got.get("current_price") == 120.0) is fresh and (got == {}) is not fresh


def test_a_cap_from_another_session_than_the_price_is_not_divided_by_it(market_roots):  # noqa: F811
    # The cap of 2026-10-23 over the close of 2026-10-27 is no share count: Yahoo's cap and shares stay.
    _store(cap_session=date(2026, 10, 23))
    got = mi.from_store(TICKER)
    assert got["current_price"] == 120.0 and "market_cap" not in got and "shares_outstanding" not in got


def test_no_prices_file_gives_nothing(market_roots):  # noqa: F811
    assert mi.from_store(TICKER) == {}


def test_the_overlay_keeps_yahoos_value_where_the_store_has_none(market_roots):  # noqa: F811
    _store(months=12)                                        # too short for a beta
    out, part = mi.overlay(TICKER, {**YAHOO, "annual": []})
    assert out["current_price"] == 120.0 and out["market_cap"] == 6000.0 and out["beta"] == 1.1
    assert part == "prices store, 2026-10-27 close (price, market cap, shares) · Yahoo (beta)"
    assert out["market_meta"]["price_session"] == "2026-10-27"
    assert "beta_source" not in out


def test_the_overlay_names_yahoos_cap_where_the_store_has_a_price_alone(market_roots):  # noqa: F811
    _store(own_cap=None, months=12)
    out, part = mi.overlay(TICKER, dict(YAHOO))
    assert out["current_price"] == 120.0 and out["market_cap"] == 5000.0 and out["shares_outstanding"] == 50.0
    assert part == "prices store, 2026-10-27 close (price) · Yahoo (market cap, shares, beta)"


def test_the_overlay_with_nothing_in_the_store_names_yahoo(market_roots):  # noqa: F811
    out, part = mi.overlay(TICKER, dict(YAHOO))
    assert {k: out[k] for k in YAHOO} == YAHOO and part == "Yahoo (price, market cap, shares, beta)"


def _run(monkeypatch, tmp_path):
    import RevenueChaser.module_financials.run as fm
    from RevenueChaser.core.providers import cache as cache_manager
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    cache_manager.save(TICKER, {"ticker": TICKER, "info": {"longName": "Growth Co."}, **YAHOO, "currency": "USD",
                                "source": "yfinance",
                                "annual": [{"fiscal_year": "2024", "revenue": 1.0, "ebitda": 0.5, "ebit": 0.4,
                                            "depreciation": 0.1, "capital_expenditure": 0.1, "total_debt": 1.0,
                                            "cash": 1.0}]})
    return fm.run(TICKER, modules=["dcf"], output="none", no_fetch=True)["results"]


def test_a_run_models_the_stores_market_inputs_and_says_so(built, monkeypatch, tmp_path):  # noqa: F811
    _store()
    out = _run(monkeypatch, tmp_path)
    fin, dcf = out["financials"], out["modules"]["dcf"]
    assert fin["current_price"] == 120.0 and fin["market_cap"] == 6000.0
    assert fin["source"].endswith(" · prices store, 2026-10-27 close (price, market cap, shares, beta)")
    assert "Yahoo (price" not in fin["source"]
    assert dcf["wacc"]["beta"] == pytest.approx(1.5)
    assert dcf["wacc"]["beta_source"] == "prices store (5y monthly split-adjusted price returns vs S&P 500)"


def test_a_run_without_the_prices_store_keeps_yahoos_and_says_so(built, monkeypatch, tmp_path):  # noqa: F811
    out = _run(monkeypatch, tmp_path)
    fin, dcf = out["financials"], out["modules"]["dcf"]
    assert fin["current_price"] == 100.0 and fin["market_cap"] == 5000.0
    assert fin["source"].endswith(" · Yahoo (price, market cap, shares, beta)")
    assert dcf["wacc"]["beta"] == 1.1 and dcf["wacc"]["beta_source"] == "Yahoo"


def test_off_the_fundamentals_layer_the_source_line_still_names_the_market_inputs(market_roots, monkeypatch,
                                                                                   tmp_path):  # noqa: F811
    _store()
    out = _run(monkeypatch, tmp_path)
    assert out["financials"]["source"] == ("Yahoo (statements) · prices store, 2026-10-27 close "
                                           "(price, market cap, shares, beta)")


def test_the_card_names_the_session_of_the_stores_price():
    from RevenueChaser.module_financials.reporting.html_builder import build_report_html
    fin = {**YAHOO, "market_meta": {"from_store": ["price"], "price_session": "2026-10-27"}}
    html = build_report_html({"ticker": TICKER, "financials": fin, "modules": {}, "warnings": []})
    assert "Price: $100.00 (close 2026-10-27)<" in html
    yahoo = build_report_html({"ticker": TICKER, "financials": dict(YAHOO), "modules": {}, "warnings": []})
    assert "Price: $100.00<" in yahoo


def test_the_comps_basis_names_where_the_targets_shares_came_from(market_roots):  # noqa: F811
    # The peer multiples stay Yahoo's (their .info); the target's per-share values divide by the store's shares.
    from RevenueChaser.module_financials.engines import comps_engine
    from RevenueChaser.module_financials.reporting.html_builder import build_report_html
    _store()
    fin, _part = mi.overlay(TICKER, {**YAHOO, "annual": [{"fiscal_year": "2025", "revenue": 100.0, "ebitda": 30.0,
                                                          "net_income": 10.0, "total_debt": 5.0, "cash": 5.0}]})
    peer = {"enterpriseValue": 900.0, "ebitda": 90.0, "totalRevenue": 300.0, "marketCap": 800.0,
            "netIncomeToCommon": 40.0}
    comps = comps_engine.run_comps(fin, ["PEER"], fetch_info=lambda p: peer)
    assert comps["basis"]["target_shares"] == "prices store, 2026-10-27 close"
    html = build_report_html({"ticker": TICKER, "financials": fin, "modules": {"comps": comps}, "warnings": []})
    assert "peer multiples are trailing twelve months (Yahoo)" in html
    assert "per share divides by the target's shares from the prices store, 2026-10-27 close." in html
    yahoo = comps_engine.run_comps({**YAHOO, "annual": fin["annual"]}, ["PEER"], fetch_info=lambda p: peer)
    assert yahoo["basis"]["target_shares"] == "Yahoo"


def test_the_cards_ev_says_it_is_yahoos_next_to_the_stores_cap():
    # Shortcut kept on purpose (review should 2): the EV stays Yahoo's (its newest quarter's debt and cash); the
    # statements' net debt is up to a year old, so a store cap plus it would be no better. The card says so.
    from RevenueChaser.module_financials.reporting.html_builder import build_report_html
    fin = {**YAHOO, "enterprise_value": 5200.0,
           "market_meta": {"from_store": ["price", "market cap, shares"], "price_session": "2026-10-27",
                           "shares_source": "prices store, 2026-10-27 close"}}
    html = build_report_html({"ticker": TICKER, "financials": fin, "modules": {}, "warnings": []})
    assert "EV: $5.2K (Yahoo)<" in html
    yahoo = build_report_html({"ticker": TICKER, "financials": {**YAHOO, "enterprise_value": 5200.0},
                               "modules": {}, "warnings": []})
    assert "EV: $5.2K<" in yahoo
