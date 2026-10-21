"""yfinance provider tests (synthetic DataFrames, no network)."""
from __future__ import annotations

import pandas as pd

from RevenueChaser.core.providers import yfinance as yfp

_Y1 = pd.Timestamp("2022-12-31")
_Y2 = pd.Timestamp("2023-12-31")


class _FakeTicker:
    def __init__(self, income=None, cashflow=None, balance=None, info=None, info_error=False):
        self.income_stmt = income if income is not None else pd.DataFrame()
        self.cashflow = cashflow if cashflow is not None else pd.DataFrame()
        self.balance_sheet = balance if balance is not None else pd.DataFrame()
        self._info = info or {}
        self._info_error = info_error

    @property
    def info(self):
        if self._info_error:
            raise RuntimeError("network down")
        return self._info


def _df(rows: dict[str, dict[pd.Timestamp, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows).T


def _full_income():
    return _df({
        "Total Revenue": {_Y1: 100.0, _Y2: 150.0},
        "EBIT": {_Y1: 20.0, _Y2: 25.0},
        "EBITDA": {_Y1: 30.0, _Y2: 35.0},
        "Net Income": {_Y1: 10.0, _Y2: 15.0},
    })


def _full_cashflow():
    return _df({
        "Operating Cash Flow": {_Y1: 40.0, _Y2: 45.0},
        "Capital Expenditure": {_Y1: -5.0, _Y2: -7.0},
    })


def _full_balance_no_total_debt():
    return _df({
        "Total Assets": {_Y1: 200.0, _Y2: 220.0},
        "Current Debt": {_Y1: 3.0, _Y2: 4.0},
        "Long Term Debt": {_Y1: 12.0, _Y2: 14.0},
    })


def _full_balance_with_total_debt():
    return _df({
        "Total Debt": {_Y1: 15.0, _Y2: 18.0},
        "Current Debt": {_Y1: 999.0, _Y2: 999.0},  # should be ignored when Total Debt present
    })


def test_label_map_normalizes_keys():
    df = _df({"Total Revenue": {_Y1: 1.0}})
    m = yfp._label_map(df)
    assert "total revenue" in m


def test_pick_returns_first_matching_candidate():
    df = _df({"Operating Income": {_Y1: 5.0}})
    m = yfp._label_map(df)
    picked = yfp._pick(m, ["EBIT", "Operating Income"])
    assert picked is not None
    assert picked[_Y1] == 5.0


def test_pick_returns_none_when_no_candidate_matches():
    df = _df({"Something Else": {_Y1: 5.0}})
    m = yfp._label_map(df)
    assert yfp._pick(m, ["EBIT", "Operating Income"]) is None


def test_years_sorts_ascending_and_caps_max_years():
    df = _df({"Row": {_Y2: 2.0, _Y1: 1.0}})
    years = yfp._years(df, max_years=6)
    assert years == [_Y1, _Y2]


# The groups get_financials passes, from the provider's own label lists (a test-local copy would not catch a
# change of their order).
_DEBT_GROUPS = [yfp._BALANCE_FIELDS["short_term_debt"], yfp._BALANCE_FIELDS["long_term_debt"]]


def test_sum_series_falls_back_to_short_plus_long_debt():
    df = _full_balance_no_total_debt()
    m = yfp._label_map(df)
    cols = yfp._years(df)
    total = yfp._sum_series(m, _DEBT_GROUPS, cols)
    assert total == [15.0, 18.0]  # 3+12, 4+14


def test_sum_series_returns_none_list_when_no_candidates_present():
    df = _df({"Unrelated": {_Y1: 1.0}})
    m = yfp._label_map(df)
    cols = yfp._years(df)
    total = yfp._sum_series(m, _DEBT_GROUPS, cols)
    assert total == [None]


def test_sum_series_year_with_every_part_missing_is_none_not_zero():
    """DF38 / #169b: the part rows exist but hold nothing for a year, so the total is unknown, not 0.0."""
    df = _df({
        "Total Assets": {_Y1: 200.0, _Y2: 220.0},
        "Current Debt": {_Y1: 3.0, _Y2: float("nan")},
        "Long Term Debt": {_Y1: 12.0, _Y2: float("nan")},
    })
    m = yfp._label_map(df)
    total = yfp._sum_series(m, _DEBT_GROUPS, yfp._years(df))
    assert total == [15.0, None]


def test_sum_series_does_not_add_alternative_labels_of_the_same_debt():
    """#169 re-review: with both current labels and both long-term labels present the old sum gave 29,
    not 13 (plain labels) or 16 (lease-inclusive labels). One label per group and year counts."""
    df = _df({
        "Current Debt": {_Y1: 1.0},
        "Current Debt And Capital Lease Obligation": {_Y1: 4.0},
        "Long Term Debt": {_Y1: 12.0},
        "Long Term Debt And Capital Lease Obligation": {_Y1: 15.0},
    })
    m = yfp._label_map(df)
    total = yfp._sum_series(m, _DEBT_GROUPS, yfp._years(df))
    assert total == [13.0]  # Current Debt + Long Term Debt; not 32 (all four) or 19 (lease-inclusive)


def test_sum_series_takes_the_next_label_of_a_group_when_the_first_is_empty_that_year():
    df = _df({
        "Current Debt": {_Y1: 2.0, _Y2: float("nan")},
        "Current Debt And Capital Lease Obligation": {_Y1: 5.0, _Y2: 6.0},
        "Long Term Debt": {_Y1: 10.0, _Y2: 11.0},
    })
    m = yfp._label_map(df)
    total = yfp._sum_series(m, _DEBT_GROUPS, yfp._years(df))
    assert total == [12.0, 17.0]  # 2+10; Current Debt empty in Y2, so the lease-inclusive 6+11


def test_sum_series_one_known_part_gives_its_value():
    df = _df({
        "Current Debt": {_Y1: float("nan"), _Y2: float("nan")},
        "Long Term Debt": {_Y1: 7.0, _Y2: float("nan")},
        "Total Assets": {_Y1: 1.0, _Y2: 1.0},
    })
    m = yfp._label_map(df)
    assert yfp._sum_series(m, _DEBT_GROUPS, yfp._years(df)) == [7.0, None]


def test_get_financials_latest_year_without_debt_values_has_total_debt_none(monkeypatch):
    """The payload a Fetch stores: a year whose debt rows are empty carries None, so the DCF says
    "net debt unknown" instead of valuing the equity with zero debt."""
    balance = _df({
        "Total Assets": {_Y1: 200.0, _Y2: 220.0},
        "Current Debt": {_Y1: 3.0, _Y2: float("nan")},
        "Long Term Debt": {_Y1: 12.0, _Y2: float("nan")},
    })
    fake = _FakeTicker(income=_full_income(), cashflow=_full_cashflow(), balance=balance)
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)
    annual = yfp.get_financials("aapl")["annual"]
    assert [r["total_debt"] for r in annual] == [15.0, None]


def test_year_label_from_timestamp_and_plain_value():
    assert yfp._year_label(_Y1) == "2022"
    assert yfp._year_label("2021") == "2021"
    assert yfp._year_label(2020) == "2020"


def test_get_financials_happy_path_with_debt_fallback(monkeypatch):
    fake = _FakeTicker(
        income=_full_income(),
        cashflow=_full_cashflow(),
        balance=_full_balance_no_total_debt(),
        info={
            "sector": "Technology",
            "sharesOutstanding": 1_000_000,
            "marketCap": 5_000_000.0,
            "beta": 1.2,
            "currentPrice": 10.0,
            "currency": "USD",
        },
    )
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    result = yfp.get_financials("aapl")

    assert result["ticker"] == "AAPL"
    assert result["sector"] == "Technology"
    assert result["shares_outstanding"] == 1_000_000
    assert result["market_cap"] == 5_000_000.0
    assert result["beta"] == 1.2
    assert len(result["annual"]) == 2
    latest = result["annual"][-1]
    assert latest["fiscal_year"] == "2023"
    assert latest["revenue"] == 150.0
    assert latest["ebit"] == 25.0
    # capex reported negative by yfinance -> stored as positive magnitude
    assert latest["capital_expenditure"] == 7.0
    # Total Debt absent -> fallback sum of short + long term debt
    assert latest["total_debt"] == 18.0


def test_get_financials_records_carry_the_period_end_date(monkeypatch):
    """#297: the vendor_financials key needs the fiscal period's end date, not only its year label."""
    fake = _FakeTicker(income=_full_income(), cashflow=_full_cashflow(), balance=_full_balance_no_total_debt())
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)
    annual = yfp.get_financials("aapl")["annual"]
    assert [r["period_end"] for r in annual] == ["2022-12-31", "2023-12-31"]


def test_period_end_is_none_for_a_column_without_a_date():
    assert yfp._period_end("2023") is None
    assert yfp._period_end(pd.Timestamp("2024-06-30")) == "2024-06-30"


def test_get_financials_prefers_total_debt_when_present(monkeypatch):
    fake = _FakeTicker(
        income=_full_income(),
        cashflow=_full_cashflow(),
        balance=_full_balance_with_total_debt(),
    )
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    result = yfp.get_financials("aapl")
    latest = result["annual"][-1]
    assert latest["total_debt"] == 18.0  # explicit Total Debt row, not the ignored Current Debt


def test_get_financials_no_statements_returns_empty_annual(monkeypatch):
    fake = _FakeTicker(info={"sector": "Industrials"})
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    result = yfp.get_financials("ZZZZ")
    assert result["annual"] == []
    assert result["sector"] == "Industrials"


def test_get_financials_info_exception_is_swallowed(monkeypatch):
    fake = _FakeTicker(income=_full_income(), info_error=True)
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)

    result = yfp.get_financials("ZZZZ")
    assert result["info"] == {}
    assert result["sector"] is None


# --- #169a / P21: "Net Interest Income" is not an interest expense ---------------------
#
# ``_pick`` takes the first label that is present. A bank-like filer with neither "Interest
# Expense" nor "Interest Expense Non Operating" used to get its *net interest income* stored as
# ``interest_expense``, and Credit Capacity then graded ``EBITDA / net interest income``.

def _income_with(extra: dict[str, dict]):
    return _df({**{"Total Revenue": {_Y1: 100.0, _Y2: 150.0}, "EBIT": {_Y1: 20.0, _Y2: 25.0}}, **extra})


def _interest_for(monkeypatch, extra):
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: _FakeTicker(income=_income_with(extra)))
    return [row["interest_expense"] for row in yfp.get_financials("ZZZZ")["annual"]]


def test_a_frame_with_only_net_interest_income_gives_no_interest_expense(monkeypatch):
    got = _interest_for(monkeypatch, {"Net Interest Income": {_Y1: 40.0, _Y2: 55.0}})
    assert got == [None, None]


def test_interest_expense_wins_over_net_interest_income(monkeypatch):
    got = _interest_for(monkeypatch, {"Interest Expense": {_Y1: 4.0, _Y2: 5.0},
                                      "Net Interest Income": {_Y1: 40.0, _Y2: 55.0}})
    assert got == [4.0, 5.0]


def test_interest_expense_non_operating_is_still_used_when_it_is_the_only_label(monkeypatch):
    got = _interest_for(monkeypatch, {"Interest Expense Non Operating": {_Y1: 6.0, _Y2: 7.0}})
    assert got == [6.0, 7.0]


def test_interest_expense_non_operating_wins_over_net_interest_income(monkeypatch):
    got = _interest_for(monkeypatch, {"Interest Expense Non Operating": {_Y1: 6.0, _Y2: 7.0},
                                      "Net Interest Income": {_Y1: 40.0, _Y2: 55.0}})
    assert got == [6.0, 7.0]


def test_net_interest_income_is_not_in_the_interest_expense_labels():
    assert "Net Interest Income" not in yfp._INCOME_FIELDS["interest_expense"]
    assert yfp._INCOME_FIELDS["interest_expense"][:2] == ["Interest Expense", "Interest Expense Non Operating"]


class _InfoOnlyTicker:
    """A Yahoo ticker whose statements must never be touched by ``get_info`` (#150 U13)."""

    def __init__(self, info):
        self._info = info
        self.statement_reads = 0

    @property
    def info(self):
        return self._info

    def _statement(self):
        self.statement_reads += 1
        raise AssertionError("get_info must not read a statement")

    income_stmt = financials = cashflow = cash_flow = balance_sheet = balance_sheets = property(_statement)


def test_get_info_reads_info_only_and_marks_the_payload(monkeypatch):
    fake = _InfoOnlyTicker({"sector": "Technology", "marketCap": 5e9, "currentPrice": 12.5,
                            "country": "United States"})
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)
    out = yfp.get_info("abc")
    assert fake.statement_reads == 0
    assert out["ticker"] == "ABC"
    assert out["annual"] == []
    assert out["info_only"] is True
    assert out["info"]["country"] == "United States"
    assert out["market_cap"] == 5e9 and out["current_price"] == 12.5
    assert out["source"] == "yfinance"


def test_get_info_swallows_a_failed_info_call(monkeypatch):
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: _FakeTicker(info_error=True))
    out = yfp.get_info("ABC")
    assert out["info"] == {} and out["annual"] == []


def test_get_financials_keeps_its_shape_and_is_not_info_only(monkeypatch):
    fake = _FakeTicker(income=_full_income(), info={"marketCap": 1e9})
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)
    out = yfp.get_financials("ABC")
    assert "info_only" not in out
    assert len(out["annual"]) == 2


def test_get_financials_with_both_label_variants_counts_each_debt_once(monkeypatch):
    """#169 case through the provider: the four labels present, no "Total Debt": the plain labels (13), not all
    four (32) or the lease-inclusive pair (19). Distinct long-term values, so either list's order is pinned."""
    balance = _df({
        "Total Assets": {_Y1: 200.0},
        "Current Debt": {_Y1: 1.0},
        "Current Debt And Capital Lease Obligation": {_Y1: 4.0},
        "Long Term Debt": {_Y1: 12.0},
        "Long Term Debt And Capital Lease Obligation": {_Y1: 15.0},
    })
    fake = _FakeTicker(income=_df({"Total Revenue": {_Y1: 100.0}}), balance=balance)
    monkeypatch.setattr("yfinance.Ticker", lambda ticker: fake)
    assert [r["total_debt"] for r in yfp.get_financials("aapl")["annual"]] == [13.0]
