"""#732 (owner's decisions, 12 Nov 2026): the 13F board puts every quarter on today's share basis, and shows a rolling
window of the last 6 settled quarters.

* A split between two quarters is not buying: the earlier quarter's shares and flows are divided by its quarter end's
  split factor and its close multiplied by it, so Δ%, Streak, Net flow, % Out and Price Δ see one share basis.
* ``core.ownership.split_factors_13f`` is the one split rule for 13F share counts (the board and Asymmetric Growth).
* The board's quarter columns are the newest ``WINDOW_QUARTERS`` settled quarters plus any still arriving.

Offline; no store: the split factors are passed in, or the prices store is replaced at the read boundary.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.module_sec_13f import history, report

Q1, Q2 = "2025-03-31", "2025-06-30"
META = {"AAA": {"name": "Alpha", "sector": "Technology"}}


def _cell(shares, buy=None, sell=None, complete=True):
    return {"shares": shares, "value": 1000.0, "holders": 3, "new_holders": 0, "exited_holders": 0,
            "top10_pct": 90.0, "hhi": 4000.0, "buy_flow": buy, "sell_flow": sell, "complete": complete}


def _cells(payload, ticker="AAA"):
    for row in payload["rows"]:
        if row[0] == ticker:
            return dict(zip(payload["cols"], row[3]))
    raise AssertionError(f"{ticker} not in the payload")


# ── one share basis ──────────────────────────────────────────────────────────

# A 10-for-1 split after Q1: Q1 was filed as 100 shares at a close of 100, Q2 as 1,000 shares at 10. Nobody traded.
SPLIT_TOTALS = {"AAA": {Q1: _cell(100.0), Q2: _cell(1000.0, buy=0.0, sell=0.0)}}
SPLIT_INPUTS = {"AAA": {"split_factor": {Q1: 0.1, Q2: 1.0}, "price": {Q1: 100.0, Q2: 10.0},
                        "shares_outstanding": 4000.0}}


def test_a_split_between_two_quarters_is_not_read_as_buying():
    cells = _cells(report.board_payload(SPLIT_TOTALS, META, SPLIT_INPUTS))
    assert cells[f"sh_{Q1}"][1] == pytest.approx(1000.0)            # 100 as filed, ÷ 0.1 = today's 1,000
    assert cells[f"ch_{Q2}"][1] == pytest.approx(0.0)
    assert cells["streak"][1] == 0
    assert cells["price_delta"][1] == pytest.approx(0.0)            # 100 × 0.1 = 10, then 10
    assert cells["pct_out"][1] == pytest.approx(25.0)               # 1,000 of 4,000
    # Without a factor (the prices store unreadable) the board shows the split as the old board did.
    bare = _cells(report.board_payload(SPLIT_TOTALS, META, {"AAA": {"price": {Q1: 100.0, Q2: 10.0}}}))
    assert bare[f"ch_{Q2}"][1] == pytest.approx(900.0)
    assert bare["price_delta"][1] == pytest.approx(-90.0)


def test_the_cell_says_what_was_filed():
    cells = _cells(report.board_payload(SPLIT_TOTALS, META, SPLIT_INPUTS))
    title = cells[f"sh_{Q1}"][3]
    assert "today's basis" in title and "100 as filed" in title


def test_the_flows_of_the_snapshot_are_on_todays_basis():
    totals = {"AAA": {Q1: _cell(100.0), Q2: _cell(150.0, buy=60.0, sell=10.0)}}
    inputs = {"AAA": {"split_factor": {Q1: 0.5, Q2: 0.5}}}           # a 2-for-1 split after both quarters
    cells = _cells(report.board_payload(totals, META, inputs))
    assert cells["netflow"][1] == pytest.approx(100.0)              # (60 − 10) ÷ 0.5
    assert cells[f"ch_{Q2}"][1] == pytest.approx(50.0)              # the same factor on both sides: unchanged


def test_the_result_history_records_the_board_numbers(monkeypatch):
    monkeypatch.setattr(history, "cusips_by_ticker", lambda quarter: {})       # the CUSIP map is not this test's
    _q, rows = history.metric_rows(SPLIT_TOTALS, META, SPLIT_INPUTS)
    (row,) = rows
    assert row["shares_by_quarter"] == {Q1: pytest.approx(1000.0), Q2: pytest.approx(1000.0)}
    assert row["chg_by_quarter"] == {Q2: pytest.approx(0.0)}
    assert history.SELECTION_VERSION == "8"
    assert history.effective_config()["WINDOW_QUARTERS"] == report.WINDOW_QUARTERS == 6


def test_the_chart_is_on_the_same_basis():
    fig = report.build_chart_fig("AAA", SPLIT_TOTALS["AAA"], SPLIT_INPUTS["AAA"])
    (shares,) = [t for t in fig["data"] if t.get("name") == "Shares"]
    (price,) = [t for t in fig["data"] if t.get("name") == "Price"]
    assert list(shares["y"]) == [pytest.approx(1000.0), pytest.approx(1000.0)]
    assert list(price["y"]) == [pytest.approx(10.0), pytest.approx(10.0)]


# ── the shared split rule ────────────────────────────────────────────────────

def test_split_factors_13f_reads_one_bar_window_per_quarter(monkeypatch):
    from RevenueChaser.core import ownership
    from RevenueChaser.rcdata import read
    asked = []
    monkeypatch.setattr(read, "security_ids", lambda tickers: {t: f"sid:{t}" for t in tickers})

    def bars(ids, start=None, end=None, basis="nominal"):
        asked.append((sorted(ids), start, end, basis))
        factor = {date(2025, 3, 31): 0.1, date(2025, 6, 30): 1.0}[end]
        return [{"security_id": i, "session_date": end, "split_factor": factor} for i in sorted(ids)]

    monkeypatch.setattr(read, "bars", bars)
    out = ownership.split_factors_13f({"AAA": [Q1, Q2], "BBB": [Q2]})
    assert out == {"AAA": {Q1: 0.1, Q2: 1.0}, "BBB": {Q2: 1.0}}
    assert [(a[0], a[2], a[3]) for a in asked] == [(["sid:AAA"], date(2025, 3, 31), "split"),
                                                   (["sid:AAA", "sid:BBB"], date(2025, 6, 30), "split")]


def test_an_unreadable_prices_store_gives_no_factor(monkeypatch):
    from RevenueChaser.core import ownership
    from RevenueChaser.rcdata import read

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(read, "security_ids", broken)
    assert ownership.split_factors_13f({"AAA": [Q1]}) == {}


# ── the rolling window ───────────────────────────────────────────────────────

EIGHT = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30",
         "2026-12-31"]


def test_the_board_shows_the_last_six_settled_quarters():
    totals = {"AAA": {q: _cell(100.0 + i) for i, q in enumerate(EIGHT)}}
    cols = report.board_payload(totals, META, {})["cols"]
    assert [c for c in cols if c.startswith("sh_")] == [f"sh_{q}" for q in EIGHT[2:]]


def test_a_quarter_still_arriving_rides_after_the_six():
    totals = {"AAA": {q: _cell(100.0 + i, complete=(q != EIGHT[-1])) for i, q in enumerate(EIGHT)}}
    cols = report.board_payload(totals, META, {})["cols"]
    assert [c for c in cols if c.startswith("sh_")] == [f"sh_{q}" for q in EIGHT[1:]]
