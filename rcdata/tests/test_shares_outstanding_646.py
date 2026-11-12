"""The shared "% of shares out" denominator (#646 PR 1).

The 13F board and Asymmetric Growth both divide a 13F holding by shares outstanding, and until now both took it
from the old ``ownership_snapshot``, which had a count for 1,636 of the 8,423 13F tickers (19.4%, measured on the
10 Nov 2026 copy). ``rcdata.read.shares_outstanding`` is the one rule for both: the SEC cover page's count, else
the market cap over the close, else nothing — and it says which, so the cell can too.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import read
from RevenueChaser.rcdata.tests import prices_store

SESSION = date(2026, 10, 1)


def _security(ticker: str, close: float, *, cap: float | None = None, outstanding: float | None = None,
              class_member: str | None = None) -> None:
    """One security with a bar, and optionally a cap and a cover-page count."""
    prices_store.put_bars([{"ticker": ticker, "session_date": SESSION, "close": close}], replace=False)
    con = prices_store._connect()
    try:
        sid = f"sym:{ticker}"
        if cap is not None:
            con.execute("INSERT INTO market_cap_daily (security_id, session_date, close, market_cap, "
                        "issuer_market_cap) VALUES (?, ?, ?, ?, ?)", [sid, SESSION, close, cap, cap])
        if outstanding is not None:
            con.execute("INSERT INTO shares_class (security_id, measure, as_of, value, symbol, class_member, "
                        "source) VALUES (?, 'outstanding', ?, ?, ?, ?, 'sec/cover_shares')",
                        [sid, SESSION, outstanding, ticker, class_member])
        con.execute("CHECKPOINT")
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _clean_prices():
    prices_store.clear()
    yield


def test_a_cover_page_count_is_the_stored_basis():
    _security("AAA", 10.0, cap=5_000.0, outstanding=1_000.0)
    row = read.shares_outstanding(["AAA"])["AAA"]
    assert row["shares"] == 1_000.0, "the cover page wins over the implied number"
    assert row["basis"] == "stored"
    assert row["as_of"] == SESSION


def test_without_a_count_the_cap_over_the_close_stands_in():
    _security("BBB", 20.0, cap=4_000.0)
    row = read.shares_outstanding(["BBB"])["BBB"]
    assert row["shares"] == 200.0                      # 4,000 / 20
    assert row["basis"] == "market_cap/price"


def test_a_name_with_neither_says_so_rather_than_guessing():
    _security("CCC", 5.0)                              # a bar, no cap and no count
    row = read.shares_outstanding(["CCC"])["CCC"]
    assert row["shares"] is None and row["basis"] == "none"


def test_a_ticker_the_store_does_not_know_is_left_out():
    _security("AAA", 10.0, outstanding=1_000.0)
    assert "ZZZZ" not in read.shares_outstanding(["AAA", "ZZZZ"])


def test_the_newest_count_wins_and_a_class_label_does_not_change_the_basis():
    """On the 10 Nov 2026 copy every covered 13F ticker had exactly one outstanding row, 3,134 of 3,248 without a
    class label. A second row is still answered - the newest - rather than refused."""
    _security("DDD", 10.0, outstanding=1_000.0, class_member="us-gaap:CommonClassAMember")
    con = prices_store._connect()
    try:
        con.execute("INSERT INTO shares_class (security_id, measure, as_of, value, symbol, class_member, source) "
                    "VALUES ('sym:DDD', 'outstanding', ?, 2_000.0, 'DDD', NULL, 'sec/cover_shares')",
                    [date(2026, 10, 2)])
        con.execute("CHECKPOINT")
    finally:
        con.close()
    row = read.shares_outstanding(["DDD"])["DDD"]
    assert row["shares"] == 2_000.0 and row["basis"] == "stored"
    assert row["as_of"] == date(2026, 10, 2)


def test_a_zero_or_negative_count_is_not_a_count():
    _security("EEE", 10.0, cap=3_000.0, outstanding=0.0)
    row = read.shares_outstanding(["EEE"])["EEE"]
    assert row["basis"] == "market_cap/price" and row["shares"] == 300.0


def test_no_tickers_is_no_read():
    assert read.shares_outstanding([]) == {}
    assert read.shares_outstanding([None, "", "  "]) == {}
