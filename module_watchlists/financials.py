"""The watch list's Financials columns: revenue, EPS, P/E, beta, dividend and yield (#786 step 4c).

TradingView's Financials tab shows these six over a list's rows (avo's screenshot 3, 12 Nov 2026). Here each one
is worked out from what this repository already holds, through the read layer only:

- **Revenue** and **EPS** come from the fundamentals layer (``core/fundamentals_layer``, the SEC's statements). The
  trailing twelve months where the newest 10-Q follows the fiscal year (``core/trailing_year``, the same rule the
  Fundamentals and Asymmetric Growth boards use), the newest fiscal year otherwise. The basis rides the row
  (``fb``: ``TTM`` or ``FY``, and the period end), so a reader can tell which one a number is.
- **EPS on the trailing year** is the trailing net income over the newest quarter's diluted share count. EPS is not
  additive (``rcdata/build/fundamentals/facts.py``), so a year to date of it cannot be differenced the way a
  revenue can. On the fiscal year it is the filer's own diluted EPS, or the same quotient when the filer tags none.
- **P/E** is the newest close over that EPS. A loss gives no P/E ("—"), as TradingView shows it.
- **Dividend** is the cash dividends per share with an ex-date in the last 365 days, specials left out, each put on
  today's share basis by the splits that came after it. **Yield** is that over the newest close.
- **Beta** is the one-year beta against the S&P 500 ETF, the Risk tab's own measure (``indicators.beta``).

Everything here is a pure function of rows handed in: the board does the reads, so a test needs no store.
"""
from __future__ import annotations

from datetime import date, timedelta

from RevenueChaser.core import trailing_year

#: The flows the trailing year must carry, and the balance figure the EPS quotient divides by.
TTM_FLOWS = ("revenue", "net_income")
TTM_BALANCE = ("diluted_shares",)
#: How far back a dividend counts: one year of ex-dates.
DIVIDEND_DAYS = 365
#: The cash-dividend action as ``corporate_action`` names it, and the actions that change the share count.
CASH_DIVIDEND = "cash_dividend"
SHARE_ACTIONS = ("split", "stock_dividend")


def _f(value) -> float | None:
    try:
        out = None if value is None else float(value)
    except (TypeError, ValueError):
        return None
    return None if out is None or out != out else out


def _eps(net_income, shares) -> float | None:
    ni, sh = _f(net_income), _f(shares)
    return ni / sh if ni is not None and sh not in (None, 0.0) and sh > 0 else None


def statements(annual: list[dict], quarterly: list[dict]) -> dict:
    """``{rev, eps, fb}`` of one company: the trailing year where the layer gives one, else its newest fiscal year.

    ``annual`` and ``quarterly`` are ``core.fundamentals_layer.records_many``'s, oldest first. A company with no
    annual record gives no numbers at all, never a quarter's figure dressed as a year's.
    """
    if not annual:
        return {"rev": None, "eps": None, "fb": None}
    newest = annual[-1]
    ttm = trailing_year.trailing_year(annual, quarterly, len(annual) - 1, TTM_FLOWS, TTM_BALANCE)
    if ttm is not None:
        return {"rev": _f(ttm.get("revenue")), "eps": _eps(ttm.get("net_income"), ttm.get("diluted_shares")),
                "fb": f"TTM to {ttm['period']}"}
    eps = _f(newest.get("eps_diluted"))
    if eps is None:
        eps = _eps(newest.get("net_income"), newest.get("diluted_shares"))
    return {"rev": _f(newest.get("revenue")), "eps": eps, "fb": f"FY{newest.get('fiscal_year')}"}


def _day(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def dividends_per_share(actions: list[dict], today: date) -> float | None:
    """The cash paid per share over the last ``DIVIDEND_DAYS``, on today's share basis; None when none was paid.

    ``actions`` are one security's ``corporate_action`` rows. A special dividend is left out (it does not recur, and
    a yield that counts it overstates what the name pays). A split after a dividend's ex-date divides that dividend
    by the split's ratio (new shares over old), so a 2-for-1 halves what each of today's shares received. A
    dividend in another currency is left out, because the price it is compared with is in dollars.
    """
    start = today - timedelta(days=DIVIDEND_DAYS)
    splits = [(_day(a.get("ex_date")), _f(a.get("ratio"))) for a in actions if a.get("action") in SHARE_ACTIONS]
    total, paid = 0.0, False
    for a in actions:
        if a.get("action") != CASH_DIVIDEND or a.get("special"):
            continue
        if (a.get("currency") or "USD").upper() != "USD":
            continue
        day, amount = _day(a.get("ex_date")), _f(a.get("amount"))
        if day is None or amount is None or not start < day <= today:
            continue
        for split_day, ratio in splits:
            if split_day is not None and ratio and ratio > 0 and split_day > day:
                amount /= ratio
        total += amount
        paid = True
    return total if paid else None


def row(found: dict, price: float | None, dividend: float | None, beta: float | None) -> dict:
    """One symbol's Financials values: what ``statements`` found, and what the price makes of it."""
    eps = found.get("eps")
    return {"rev": found.get("rev"), "eps": eps, "fb": found.get("fb"),
            "pe": price / eps if price is not None and eps is not None and eps > 0 else None,
            "beta": beta, "div": dividend,
            "yld": dividend / price * 100.0 if dividend is not None and price not in (None, 0.0) and price > 0
            else None}
