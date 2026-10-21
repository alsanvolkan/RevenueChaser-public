"""yfinance client — finansal tabloları (income/balance/cashflow/info) normalize eder.

yfinance DataFrames'i (satır=kalem, kolon=fiskal yıl) standart bir dict yapısına
dönüştürür: ``financials["annual"]`` kronolojik (eskiden yeniye) kayıt listesi.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from ..config import to_float
from .result import ProviderHealth, ProviderResult

logger = logging.getLogger(__name__)

# Every ``.info`` and statement read, and the ones that raised (Common K3): a failed read is counted for the
# board banner, not only logged. A run reads it with ``health()`` after ``reset_health()``.
_HEALTH = ProviderHealth("yfinance")


def health() -> dict:
    """The Yahoo reads since the last ``reset_health()``, in the shared provider-health shape."""
    return _HEALTH.health()


def reset_health() -> None:
    _HEALTH.reset()

# Kanonik alan -> olası yfinance satır etiketleri (öncelik sırasıyla)
_INCOME_FIELDS = {
    "revenue": ["Total Revenue", "Operating Revenue", "Total Operating Revenues"],
    "cost_of_revenue": ["Cost Of Revenue", "Cost of Goods Sold", "Total Expenses"],
    "gross_profit": ["Gross Profit"],
    "ebit": ["EBIT", "Operating Income", "Operating Income Or Loss"],
    "ebitda": ["EBITDA", "Normalized EBITDA"],
    "pretax_income": ["Pretax Income", "Income Before Tax"],
    "tax_provision": ["Tax Provision", "Income Tax Expense"],
    "net_income": ["Net Income", "Net Income Common Stockholders"],
    # "Net Interest Income" is deliberately absent: it is income minus expense, not an expense (#169a).
    "interest_expense": ["Interest Expense", "Interest Expense Non Operating"],
    "diluted_shares": ["Diluted Average Shares", "Diluted Weighted Average Shares"],
    "basic_shares": ["Basic Average Shares", "Basic Weighted Average Shares"],
    "rd_expense": ["Research And Development", "Research & Development"],
}

_CASHFLOW_FIELDS = {
    "operating_cash_flow": [
        "Operating Cash Flow", "Cash Flow From Continuing Operating Activities",
        "Total Cash From Operating Activities",
    ],
    "capital_expenditure": [
        "Capital Expenditure", "Capital Expenditures", "Purchase Of Property Plant And Equipment",
    ],
    "depreciation": [
        "Depreciation & Amortization", "Depreciation And Amortization",
        "Depreciation Amortization Depletion",
    ],
}

_BALANCE_FIELDS = {
    "total_assets": ["Total Assets"],
    "total_equity": ["Stockholders Equity", "Total Equity Gross Minority Interest", "Common Stock Equity"],
    "cash": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments", "Cash"],
    "short_term_debt": ["Current Debt", "Current Debt And Capital Lease Obligation", "Short Term Debt"],
    "long_term_debt": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"],
    "total_debt": ["Total Debt"],
    "receivables": ["Accounts Receivable", "Net Receivables", "Accounts Receivables Net"],
    "inventory": ["Inventory", "Inventories"],
    "payables": ["Accounts Payable", "Payables"],
    "current_assets": ["Current Assets"],
    "current_liabilities": ["Current Liabilities"],
}


def _label_map(df: pd.DataFrame) -> dict[str, pd.Series]:
    """{küçük-harfli etiket: seri} haritası (boşluk/altçizgi normalize)."""
    out: dict[str, pd.Series] = {}
    for idx, row in df.iterrows():
        label = str(idx).strip().lower().replace("_", " ")
        if label and label != "nan":
            out[label] = row
    return out


def _pick(series_map: dict[str, pd.Series], candidates: list[str]) -> pd.Series | None:
    for cand in candidates:
        key = cand.strip().lower().replace("_", " ")
        if key in series_map:
            return series_map[key]
    return None


def _years(df: pd.DataFrame, max_years: int = 6) -> list[Any]:
    """Kronolojik (eskiden yeniye) geçerli yıl kolonları."""
    cols = []
    for c in df.columns:
        s = df[c]
        if s.notna().any():
            cols.append(c)
    cols.sort()  # datetime kolonları sıralanabilir
    return cols[-max_years:]


def _col_values(series: pd.Series | None, cols: list[Any]) -> list[float | None]:
    if series is None:
        return [None] * len(cols)
    return [to_float(series.get(c)) for c in cols]


def _sum_series(maps: dict[str, pd.Series], groups: list[list[str]], cols: list[Any]) -> list[float | None]:
    """Sums one value per group and year (e.g. short-term + long-term debt).

    The labels of a group are alternatives for the same item, in priority order: per year the first label with
    a value counts and the others are never added (#169b: "Current Debt" plus its capital-lease variant counted the
    same debt twice). A year where no group has a value is None, not 0.0 (DF38).
    """
    group_rows = []
    for labels in groups:
        rows = [maps.get(c.strip().lower().replace("_", " ")) for c in labels]
        group_rows.append([r for r in rows if r is not None])
    total: list[float | None] = []
    for c in cols:
        year_total: float | None = None
        for rows in group_rows:
            v = next((x for x in (to_float(r.get(c)) for r in rows) if x is not None), None)
            if v is not None:
                year_total = v if year_total is None else year_total + v
        total.append(year_total)
    return total


def get_financials(ticker: str, max_years: int = 6) -> dict:
    """yfinance'tan normalize finansal tabloları çeker.

    Returns:
        {"ticker", "info", "sector", "annual": [ {fiscal_year, revenue, ...} ], ...}
    """
    import yfinance as yf

    t = yf.Ticker(ticker)
    info = _read_info(t, ticker)

    def _stmt(attrs: tuple[str, ...]) -> pd.DataFrame:
        for attr in attrs:
            try:
                df = getattr(t, attr)
            except Exception as exc:  # noqa: BLE001 - network; counted for the banner, then the next label
                logger.debug("yfinance %s.%s: %s", ticker, attr, exc)
                _HEALTH.call(error=str(exc) or type(exc).__name__, endpoint=f"{ticker}.{attr}")
                continue
            _HEALTH.call()
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        return pd.DataFrame()

    income = _stmt(("income_stmt", "financials"))
    cashflow = _stmt(("cashflow", "cash_flow"))
    balance = _stmt(("balance_sheet", "balance_sheets"))

    annual: list[dict] = []

    # Ortak yıl ekseni: üç tablodan en geniş kapsamı kullan
    year_sets = [set(_years(df, max_years)) for df in (income, cashflow, balance) if not df.empty]
    if not year_sets:
        return {"ticker": ticker, "info": info, "sector": _sector(info), "annual": annual}

    common = sorted(set.union(*year_sets))

    inc_map = _label_map(income) if not income.empty else {}
    cf_map = _label_map(cashflow) if not cashflow.empty else {}
    bal_map = _label_map(balance) if not balance.empty else {}

    revenue = _col_values(_pick(inc_map, _INCOME_FIELDS["revenue"]), common)
    ebit = _col_values(_pick(inc_map, _INCOME_FIELDS["ebit"]), common)
    ebitda = _col_values(_pick(inc_map, _INCOME_FIELDS["ebitda"]), common)
    net_income = _col_values(_pick(inc_map, _INCOME_FIELDS["net_income"]), common)
    tax_provision = _col_values(_pick(inc_map, _INCOME_FIELDS["tax_provision"]), common)
    pretax_income = _col_values(_pick(inc_map, _INCOME_FIELDS["pretax_income"]), common)
    gross_profit = _col_values(_pick(inc_map, _INCOME_FIELDS["gross_profit"]), common)
    cost_of_revenue = _col_values(_pick(inc_map, _INCOME_FIELDS["cost_of_revenue"]), common)
    interest_expense = _col_values(_pick(inc_map, _INCOME_FIELDS["interest_expense"]), common)
    diluted_shares = _col_values(_pick(inc_map, _INCOME_FIELDS["diluted_shares"]), common)
    basic_shares = _col_values(_pick(inc_map, _INCOME_FIELDS["basic_shares"]), common)
    rd_expense = _col_values(_pick(inc_map, _INCOME_FIELDS["rd_expense"]), common)

    ocf = _col_values(_pick(cf_map, _CASHFLOW_FIELDS["operating_cash_flow"]), common)
    capex_raw = _col_values(_pick(cf_map, _CASHFLOW_FIELDS["capital_expenditure"]), common)
    # yfinance capex'i negatif (nakit çıkışı) raporlar -> çıkış büyüklüğü olarak pozitife çevir
    capex = [abs(v) if v is not None else None for v in capex_raw]
    depreciation = _col_values(_pick(cf_map, _CASHFLOW_FIELDS["depreciation"]), common)

    total_assets = _col_values(_pick(bal_map, _BALANCE_FIELDS["total_assets"]), common)
    total_equity = _col_values(_pick(bal_map, _BALANCE_FIELDS["total_equity"]), common)
    cash = _col_values(_pick(bal_map, _BALANCE_FIELDS["cash"]), common)
    receivables = _col_values(_pick(bal_map, _BALANCE_FIELDS["receivables"]), common)
    inventory = _col_values(_pick(bal_map, _BALANCE_FIELDS["inventory"]), common)
    payables = _col_values(_pick(bal_map, _BALANCE_FIELDS["payables"]), common)
    current_assets = _col_values(_pick(bal_map, _BALANCE_FIELDS["current_assets"]), common)
    current_liabilities = _col_values(_pick(bal_map, _BALANCE_FIELDS["current_liabilities"]), common)

    # Toplam borç: "Total Debt" yoksa kısa+uzun vadeli borç toplamı
    total_debt = _col_values(_pick(bal_map, _BALANCE_FIELDS["total_debt"]), common)
    if all(v is None for v in total_debt):
        total_debt = _sum_series(
            bal_map,
            [_BALANCE_FIELDS["short_term_debt"], _BALANCE_FIELDS["long_term_debt"]],
            common,
        )

    for i, year in enumerate(common):
        annual.append({
            "fiscal_year": _year_label(year),
            "period_end": _period_end(year),
            "revenue": revenue[i],
            "cost_of_revenue": cost_of_revenue[i],
            "gross_profit": gross_profit[i],
            "ebit": ebit[i],
            "ebitda": ebitda[i],
            "pretax_income": pretax_income[i],
            "tax_provision": tax_provision[i],
            "net_income": net_income[i],
            "interest_expense": interest_expense[i],
            "diluted_shares": diluted_shares[i],
            "basic_shares": basic_shares[i],
            "rd_expense": rd_expense[i],
            "operating_cash_flow": ocf[i],
            "capital_expenditure": capex[i],
            "depreciation": depreciation[i],
            "total_assets": total_assets[i],
            "total_equity": total_equity[i],
            "cash": cash[i],
            "receivables": receivables[i],
            "inventory": inventory[i],
            "payables": payables[i],
            "current_assets": current_assets[i],
            "current_liabilities": current_liabilities[i],
            "total_debt": total_debt[i],
        })

    return _payload(ticker, info, annual)


def _payload(ticker: str, info: dict, annual: list[dict]) -> dict:
    return {
        "ticker": ticker.upper(),
        "info": info,
        "sector": _sector(info),
        "annual": annual,
        "shares_outstanding": to_float(info.get("sharesOutstanding")),
        "market_cap": to_float(info.get("marketCap")),
        "enterprise_value": to_float(info.get("enterpriseValue")),
        "beta": to_float(info.get("beta")),
        "current_price": to_float(info.get("currentPrice")),
        "currency": info.get("currency", "USD"),
        "source": "yfinance",
    }


def _read_info(t, ticker: str) -> dict:
    try:
        info = t.info or {}
    except Exception as exc:  # noqa: BLE001 - network; counted for the banner
        logger.debug("yfinance info %s: %s", ticker, exc)
        _HEALTH.call(error=str(exc) or type(exc).__name__, endpoint=f"{ticker}.info")
        return {}
    _HEALTH.call()
    return info


def get_info(ticker: str) -> dict:
    """The ``get_financials`` payload from ONE Yahoo request (``.info``), with no statements (#150 U13).

    ``annual`` is empty and the payload carries ``info_only``: ``cache`` hands such a row only to a
    reader that asks for it (Asymmetric Growth, whose annual figures come from EDGAR), so no other
    board reads it as a full payload.
    """
    import yfinance as yf

    out = _payload(ticker, _read_info(yf.Ticker(ticker), ticker), [])
    out["info_only"] = True
    return out


# The four ``earningsTrend`` blocks D14 stores (#323), as yfinance's ``Ticker.get_<block>`` methods name them.
TREND_BLOCKS = ("earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions")


def get_earnings_trend(ticker: str) -> ProviderResult:
    """A ticker's analyst estimates (E0 D14, #323): ``{block: {field: {period: value}}}`` for ``TREND_BLOCKS``,
    each block as ``get_<block>(as_dict=True)`` gives it (periods ``0q``, ``+1q``, ``0y``, ``+1y``; a missing value
    is NaN).

    yfinance 1.7.0 reads all four blocks from ONE quoteSummary ``earningsTrend`` request, cached on the ticker's
    ``Analysis`` (``scrapers/analysis.py``); the PC's installed version is checked by the M1 probe
    (``module_earnings_radar/inputs/estimates.probe``). ``ok`` with empty blocks when Yahoo has none, ``failed``
    when the read raised. Counted as one call in ``health()``.
    """
    import yfinance as yf

    endpoint = f"{ticker}.earningsTrend"
    try:
        t = yf.Ticker(ticker)
        blocks = {name: getattr(t, f"get_{name}")(as_dict=True) for name in TREND_BLOCKS}
    except Exception as exc:  # noqa: BLE001 - network or a changed library; counted for the banner
        logger.debug("yfinance earningsTrend %s: %s", ticker, exc)
        _HEALTH.call(error=str(exc)[:200] or type(exc).__name__, endpoint=endpoint)
        return ProviderResult("failed", {}, {"error": type(exc).__name__, "attempts": 1}, endpoint)
    _HEALTH.call()
    return ProviderResult("ok", {k: v if isinstance(v, dict) else {} for k, v in blocks.items()}, None, endpoint)


def _sector(info: dict) -> str | None:
    return info.get("sector") or None


def _period_end(year: Any) -> str | None:
    """The fiscal period's end date as ISO text (the ``vendor_financials`` key, #297); None without a date."""
    if hasattr(year, "date") and callable(year.date):
        try:
            return year.date().isoformat()
        except (TypeError, ValueError):
            return None
    return None


def _year_label(year: Any) -> str:
    """Fiskal yıl kolonunu '2021' gibi etikete çevirir."""
    if hasattr(year, "year"):
        return str(year.year)
    return str(year)[:4]
