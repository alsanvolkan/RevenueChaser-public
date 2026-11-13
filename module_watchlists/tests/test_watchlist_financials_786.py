"""The Watch lists board's Financials tab (#786 step 4c): revenue, EPS, P/E, beta, dividend and yield.

The arithmetic is ``module_watchlists/financials.py``, pure functions over rows handed in, so most tests here hand it
rows directly. The last ones drive ``board.columns`` with every ``rcdata.read`` call and the fundamentals layer
stubbed, to pin which number lands under which symbol and that a symbol without a source gets no row.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from RevenueChaser.module_watchlists import board, financials, store

pytestmark = pytest.mark.rawstore_schema_from_scratch

TODAY = date(2026, 11, 12)


def _year(fy: int, revenue: float, net_income: float, shares: float, eps: float | None = None,
          start: str = "2024-10-01", end: str = "2025-09-30") -> dict:
    """An annual record in ``core.fundamentals_layer``'s shape."""
    rec = {"fiscal_year": str(fy), "revenue": revenue, "net_income": net_income, "diluted_shares": shares,
           "period_starts": {"revenue": start, "net_income": start, "diluted_shares": start},
           "period_ends": {"revenue": end, "net_income": end, "diluted_shares": end}}
    if eps is not None:
        rec["eps_diluted"] = eps
    return rec


def _quarter(end: str, start: str, months: int, revenue: float, net_income: float, shares: float) -> dict:
    """A quarterly record carrying its fiscal year to date, as ``_add_ytd`` writes it."""
    return {"period": end, "revenue": revenue, "net_income": net_income, "diluted_shares": shares,
            "ytd": {"revenue": {"start": start, "months": months, "val": revenue},
                    "net_income": {"start": start, "months": months, "val": net_income}}}


# ── revenue and EPS ──────────────────────────────────────────────────────────

def test_a_10q_after_the_year_gives_the_trailing_twelve_months():
    """FY25 (Oct-Sep) revenue 400, net income 40. Q1 FY26 (to 31 Dec 2025) 120 / 15; the same quarter a year
    earlier 100 / 10. Trailing: 400 + 120 - 100 = 420 revenue, 40 + 15 - 10 = 45 net income, over 10 shares."""
    annual = [_year(2025, 400.0, 40.0, 10.0, eps=4.0)]
    quarterly = [_quarter("2024-12-31", "2024-10-01", 3, 100.0, 10.0, 10.0),
                 _quarter("2025-12-31", "2025-10-01", 3, 120.0, 15.0, 10.0)]
    got = financials.statements(annual, quarterly)
    assert got["rev"] == pytest.approx(420.0)
    assert got["eps"] == pytest.approx(4.5), "trailing net income over the newest quarter's diluted shares"
    assert got["fb"] == "TTM to 2025-12-31"


def test_with_no_quarter_after_the_year_the_year_itself_stands_with_the_filers_own_eps():
    annual = [_year(2024, 300.0, 20.0, 10.0, eps=1.9, start="2023-10-01", end="2024-09-30"),
              _year(2025, 400.0, 40.0, 10.0, eps=3.9)]
    got = financials.statements(annual, [])
    assert got == {"rev": 400.0, "eps": 3.9, "fb": "FY2025"}, "the newest year, and the EPS the filer tagged"


def test_a_year_without_a_tagged_eps_divides_net_income_by_the_diluted_shares():
    got = financials.statements([_year(2025, 400.0, -30.0, 10.0)], [])
    assert got["eps"] == pytest.approx(-3.0)


def test_a_company_the_layer_does_not_hold_has_no_numbers():
    assert financials.statements([], []) == {"rev": None, "eps": None, "fb": None}


# ── dividend, P/E and yield ──────────────────────────────────────────────────

def _div(day: date, amount: float, **extra) -> dict:
    return {"security_id": "S1", "ex_date": day, "action": "cash_dividend", "amount": amount, "currency": "USD",
            **extra}


def test_the_dividend_is_a_years_cash_with_specials_and_older_ones_left_out():
    actions = [_div(TODAY - timedelta(days=30), 0.25), _div(TODAY - timedelta(days=120), 0.25),
               _div(TODAY - timedelta(days=200), 0.25), _div(TODAY - timedelta(days=300), 0.25),
               _div(TODAY - timedelta(days=60), 5.00, special=True),
               _div(TODAY - timedelta(days=400), 0.25)]
    assert financials.dividends_per_share(actions, TODAY) == pytest.approx(1.00)


def test_a_split_after_a_dividend_puts_that_dividend_on_todays_shares():
    """A 2-for-1 (ratio 2: new shares over old) 100 days ago halves the two dividends paid before it."""
    actions = [_div(TODAY - timedelta(days=300), 0.50), _div(TODAY - timedelta(days=200), 0.50),
               {"security_id": "S1", "ex_date": TODAY - timedelta(days=100), "action": "split", "ratio": 2.0},
               _div(TODAY - timedelta(days=30), 0.25)]
    assert financials.dividends_per_share(actions, TODAY) == pytest.approx(0.25 + 0.25 + 0.25)


def test_no_dividend_is_none_and_not_zero():
    assert financials.dividends_per_share([], TODAY) is None
    assert financials.dividends_per_share([_div(TODAY - timedelta(days=10), 0.3, currency="CAD")], TODAY) is None


def test_pe_is_the_close_over_eps_and_a_loss_has_none():
    profit = financials.row({"rev": 1e9, "eps": 4.0, "fb": "FY2025"}, 100.0, 2.0, 1.1)
    assert profit["pe"] == pytest.approx(25.0)
    assert profit["yld"] == pytest.approx(2.0)
    loss = financials.row({"rev": 1e9, "eps": -1.3, "fb": "FY2025"}, 100.0, None, None)
    assert loss["pe"] is None, "TradingView shows a loss's P/E as a dash"
    assert loss["yld"] is None and loss["eps"] == -1.3


# ── the board's tab ──────────────────────────────────────────────────────────

@pytest.fixture
def stubbed(monkeypatch):
    import pandas as pd
    monkeypatch.setattr(board.read, "ticker_bars", lambda *a, **k: pd.DataFrame(
        columns=["ticker", "session_date", "close", "high", "low", "volume"]))
    monkeypatch.setattr(board.read, "latest_market", lambda *a, **k: {"NVDA": {"price": 180.0}})
    monkeypatch.setattr(board.read, "security_ids", lambda *a, **k: {"NVDA": "S-NVDA"})
    monkeypatch.setattr(board.read, "actions", lambda *a, **k: [
        {"security_id": "S-NVDA", "ex_date": date.today() - timedelta(days=20), "action": "cash_dividend",
         "amount": 0.04, "currency": "USD"}])
    monkeypatch.setattr(board.fundamentals_layer, "records_many", lambda tickers: {
        "NVDA": ([_year(2025, 130e9, 72e9, 24.5e9, eps=2.94)], [], {})})


def test_the_financials_tab_answers_one_row_per_us_listing(stubbed):
    list_id = store.create_list("Growth")
    for symbol in ("NASDAQ:NVDA", "TVC:VIX", "BIST:THYAO"):
        store.add_symbol(list_id, symbol)
    answer = board.columns(list_id, "financials")
    assert answer["tab"] == "financials"
    assert set(answer["values"]) == {"NASDAQ:NVDA"}, "a market series and a symbol with no source get no row"
    row = answer["values"]["NASDAQ:NVDA"]
    assert row["rev"] == 130e9 and row["eps"] == 2.94 and row["fb"] == "FY2025"
    assert row["pe"] == pytest.approx(180.0 / 2.94)
    assert row["div"] == pytest.approx(0.04)
    assert row["yld"] == pytest.approx(0.04 / 180.0 * 100)
    assert row["beta"] is None, "no bars, no beta"


def test_the_page_has_a_column_for_every_financials_value():
    keys = [k for k, *_ in board.COLUMNS["financials"]]
    assert keys == ["t", "rev", "eps", "pe", "beta", "div", "yld"]
    assert "financials" in board.DERIVED_TABS
