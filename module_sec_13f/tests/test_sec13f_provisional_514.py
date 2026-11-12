"""#514: a report period whose filing deadline has not passed is still arriving, and the board showed it as settled.

Measured on the live board 2026-11-06: the change column against 2026-09-30 had a median of -92% and 62% of its
6,756 rows at or below -90%, against +2.0% for the quarter before. The store knew: 4,109 filers against 8,824 for a
settled quarter, and a filing deadline ten days away. ``rcdata.read`` already computes ``complete`` per period from
that deadline; ``core.ownership.sec13f_totals`` dropped it.
"""
from __future__ import annotations

from datetime import date, timedelta

import duckdb

from RevenueChaser.core import calendar
from RevenueChaser.module_sec_13f import engine as sec_13f
from RevenueChaser.module_sec_13f import history as sec13f_history
from RevenueChaser.module_sec_13f import report
from RevenueChaser.rcdata import read as rcread
from RevenueChaser.rcdata import serving

SETTLED, ARRIVING = date(2026, 6, 30), date(2026, 9, 30)
METRICS = {"shares": 500.0, "value": 1500.0, "holders": 3, "top10_pct": 42.5, "hhi": 120.5, "new_holders": 1,
           "exited_holders": 2, "buy_flow": 10.0, "sell_flow": 4.0, "breadth": 86.4216, "breadth_chg": 1.5}


def _store(rows) -> None:
    """``sec13f_ticker_period`` with a deadline per period: ``rows`` is ``(ticker, period, shares, deadline)``."""
    path = serving.live_path("ownership")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE OR REPLACE TABLE sec13f_cusip_map (period DATE, cusip TEXT, ticker TEXT, "
                    "map_source TEXT)")          # the history reads it; empty is a true answer here
        con.execute("CREATE OR REPLACE TABLE sec13f_ticker_period (ticker TEXT, period DATE, shares DOUBLE, "
                    "value DOUBLE, "
                    "holders INTEGER, top10_pct DOUBLE, hhi DOUBLE, new_holders INTEGER, exited_holders INTEGER, "
                    "exited_confidential INTEGER, exited_no_filing INTEGER, buy_flow DOUBLE, sell_flow DOUBLE, "
                    "breadth DOUBLE, breadth_chg DOUBLE, prev_period DATE, filers INTEGER, cusips INTEGER, "
                    "filing_deadline DATE, filed_through DATE, source TEXT)")
        con.executemany(
            "INSERT INTO sec13f_ticker_period (ticker, period, shares, value, holders, top10_pct, hhi, "
            "new_holders, exited_holders, buy_flow, sell_flow, breadth, breadth_chg, filing_deadline) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [[t, p, shares, METRICS["value"], METRICS["holders"], METRICS["top10_pct"], METRICS["hhi"],
              METRICS["new_holders"], METRICS["exited_holders"], METRICS["buy_flow"], METRICS["sell_flow"],
              METRICS["breadth"], METRICS["breadth_chg"], deadline] for t, p, shares, deadline in rows])
    finally:
        con.close()


def _two_quarters(settled_shares=1_000_000.0, arriving_shares=80_000.0) -> None:
    """One settled quarter and one still arriving, as the live store holds them: the newer quarter is a part of
    itself, so its shares are far below the one before."""
    today = calendar.today_ny()
    _store([("AAPL", SETTLED, settled_shares, today - timedelta(days=60)),
            ("AAPL", ARRIVING, arriving_shares, today + timedelta(days=10))])


def test_the_period_cell_says_whether_the_quarter_has_settled():
    _two_quarters()

    totals = sec_13f.load_totals()

    assert totals["AAPL"][SETTLED.isoformat()]["complete"] is True
    assert totals["AAPL"][ARRIVING.isoformat()]["complete"] is False


def test_the_change_against_a_quarter_still_arriving_is_not_shown():
    """-92% is the quarter not being filed yet, not institutions selling. The shares stay, as "filed so far"."""
    _two_quarters()

    quarters, entries = report.board_entries(sec_13f.load_totals(), {})

    assert quarters == [SETTLED.isoformat(), ARRIVING.isoformat()]
    e = entries[0]
    assert e["shares"] == [1_000_000.0, 80_000.0]
    assert e["chgs"][1] is None, "the change against a quarter still arriving must not be rendered"


def test_the_latest_snapshot_comes_from_the_last_settled_quarter():
    """Holders, breadth, value and the flows of a quarter still arriving are a part of themselves too."""
    _two_quarters()

    _quarters, entries = report.board_entries(sec_13f.load_totals(), {})

    assert entries[0]["last"] is not None
    assert entries[0]["last_period"] == SETTLED.isoformat()


def test_the_board_marks_the_quarter_still_arriving():
    _two_quarters()

    html = report.render_sec13f_dashboard(sec_13f.load_totals(), {})

    assert "still arriving" in html.lower()
    # The column head carries the mark; "*" alone would also match the stylesheet.
    assert f'{report._qlabel(ARRIVING.isoformat())}{report.ARRIVING_MARK}' in html
    assert f'{report._qlabel(SETTLED.isoformat())}{report.ARRIVING_MARK}' not in html


def test_a_board_whose_newest_quarter_has_settled_marks_nothing():
    today = calendar.today_ny()
    _store([("AAPL", SETTLED, 1_000_000.0, today - timedelta(days=60)),
            ("AAPL", ARRIVING, 1_020_000.0, today - timedelta(days=5))])

    quarters, entries = report.board_entries(sec_13f.load_totals(), {})

    assert entries[0]["chgs"][1] is not None
    assert entries[0]["last_period"] == ARRIVING.isoformat()
    html = report.render_sec13f_dashboard(sec_13f.load_totals(), {})
    assert "still arriving" not in html.lower()
    assert f'{report._qlabel(ARRIVING.isoformat())}{report.ARRIVING_MARK}' not in html


# ── The set itself, and the shape of the board over more than two quarters ────────────────────────────────────

Q = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]


def _four_quarters(shares=(100.0, 110.0, 120.0, 10.0), arriving_last=True) -> None:
    """Three settled quarters of rising holdings and a fourth that is still arriving (or settled)."""
    today = calendar.today_ny()
    rows = []
    for i, (q, sh) in enumerate(zip(Q, shares)):
        last = i == len(Q) - 1
        deadline = today + timedelta(days=10) if (last and arriving_last) else today - timedelta(days=10 * (4 - i))
        rows.append(("AAPL", q, sh, deadline))
    _store(rows)


def test_the_arriving_set_is_the_newest_quarters_and_nothing_else():
    _four_quarters()
    totals = sec_13f.load_totals()
    quarters = sorted(totals["AAPL"])

    assert report.arriving(totals, quarters) == {Q[-1].isoformat()}


def test_a_period_with_no_filing_deadline_counts_as_settled_and_never_blanks_the_board():
    """Reading "unknown" as "arriving" would hold a whole board back over missing metadata."""
    _store([("AAPL", Q[-2], 100.0, None), ("AAPL", Q[-1], 110.0, None)])
    totals = sec_13f.load_totals()
    quarters = sorted(totals["AAPL"])

    assert report.arriving(totals, quarters) == set()
    _qs, entries = report.board_entries(totals, {})
    assert entries[0]["chgs"][1] is not None
    assert entries[0]["last_period"] == Q[-1].isoformat()


def test_the_streak_and_the_acceleration_skip_the_quarter_still_arriving():
    """Three settled quarters of rising holdings: the streak is 2 and survives a partial fourth, which would
    otherwise end it (the live board read 0 for every name)."""
    _four_quarters()

    _qs, entries = report.board_entries(sec_13f.load_totals(), {})

    assert entries[0]["streak"] == 2
    assert entries[0]["accel"] is not None
    with_partial = entries[0]["streak"]

    _four_quarters(shares=(100.0, 110.0, 120.0, 130.0), arriving_last=False)
    _qs, entries = report.board_entries(sec_13f.load_totals(), {})
    assert entries[0]["streak"] == 3 > with_partial


def test_the_board_is_ordered_by_the_last_settled_quarter():
    """The partial quarter inverts the order, so sorting on it puts the smaller holder on top."""
    today = calendar.today_ny()
    settled, arriving = Q[-2], Q[-1]
    _store([("BIG", settled, 1_000.0, today - timedelta(days=30)),
            ("SMALL", settled, 10.0, today - timedelta(days=30)),
            ("BIG", arriving, 1.0, today + timedelta(days=10)),
            ("SMALL", arriving, 9.0, today + timedelta(days=10))])

    _qs, entries = report.board_entries(sec_13f.load_totals(), {})

    assert [e["ticker"] for e in entries] == ["BIG", "SMALL"]


def test_a_ticker_only_in_the_quarter_still_arriving_has_no_settled_snapshot():
    """It is a real case once a name first appears: the shares show, the snapshot block is empty, and it sorts
    last rather than claiming another quarter's numbers."""
    today = calendar.today_ny()
    _store([("OLD", Q[-2], 500.0, today - timedelta(days=30)),
            ("OLD", Q[-1], 50.0, today + timedelta(days=10)),
            ("NEW", Q[-1], 900.0, today + timedelta(days=10))])

    _qs, entries = report.board_entries(sec_13f.load_totals(), {})
    new = next(e for e in entries if e["ticker"] == "NEW")

    assert new["last"] is None and new["last_period"] is None
    assert new["shares"][-1] == 900.0
    assert [e["ticker"] for e in entries] == ["OLD", "NEW"]


def test_the_history_records_the_quarter_its_metrics_are_from():
    """#514 moved the snapshot to the last settled quarter; labelling it with the arriving one would record one
    period's numbers under another's name."""
    _four_quarters()
    totals = sec_13f.load_totals()

    quarter, rows = sec13f_history.metric_rows(totals, {}, None)

    assert quarter == Q[-2].isoformat(), "the recorded quarter is the settled one the metrics come from"
    assert rows[0]["shares"] == 120.0                       # the settled quarter's, not the partial 10.0
    assert rows[0]["shares_by_quarter"][Q[-1].isoformat()] == 10.0      # every quarter still carries its own


def test_tickers_13f_is_the_newest_settled_period_and_ignores_the_old_table():
    today = calendar.today_ny()
    _store([("GONE", Q[-3], 1.0, today - timedelta(days=60)),
            ("LIVE", Q[-2], 1.0, today - timedelta(days=30)),
            ("LIVE", Q[-2], 1.0, today - timedelta(days=30)),          # a duplicate row collapses
            ("TOOSOON", Q[-1], 1.0, today + timedelta(days=10))])

    assert rcread.tickers_13f() == ["LIVE"]
    assert rcread.tickers_13f(Q[-3]) == ["GONE"]


# ── #521: the chart modal and the snapshot cells ────────────────────────────────────────────────────────────

def _chart(ticker="AAPL"):
    """Both panes as one record (#772 split the chart in two): the traces and annotations the assertions below
    look for are the same, they just live in two figures now."""
    totals = sec_13f.load_totals()
    figs = report.build_chart_figs(ticker, totals[ticker])
    return {"data": [t for f in figs for t in f["data"]],
            "layout": {"annotations": [a for f in figs for a in f["layout"].get("annotations", [])]}}


def _trace(fig, name):
    return next(t for t in fig["data"] if t.get("name") == name)


def test_the_chart_marks_the_quarter_still_arriving():
    """The drill-down plotted the partial bar next to the settled one with no mark (#521)."""
    _two_quarters()

    fig = _chart()

    settled, arriving = report._qlabel(SETTLED.isoformat()), report._qlabel(ARRIVING.isoformat())
    for name in ("Shares", "Value ($)"):
        bar = _trace(fig, name)
        assert list(bar["x"]) == [settled, arriving + report.ARRIVING_MARK]
        assert list(bar["marker"]["pattern"]["shape"]) == ["", "/"], "the arriving bar is hatched"
    # The holders line is solid over the settled quarters and dashed into the one arriving.
    assert list(_trace(fig, "Holders")["x"]) == [settled]
    tail = _trace(fig, "Holders (filed so far)")
    assert list(tail["x"]) == [settled, arriving + report.ARRIVING_MARK] and tail["line"]["dash"] == "dash"
    notes = " ".join(a.get("text", "") for a in fig["layout"].get("annotations", []))
    assert "still arriving" in notes and arriving in notes


def test_a_chart_whose_newest_quarter_has_settled_marks_nothing():
    today = calendar.today_ny()
    _store([("AAPL", SETTLED, 1_000_000.0, today - timedelta(days=60)),
            ("AAPL", ARRIVING, 1_020_000.0, today - timedelta(days=5))])

    fig = _chart()

    assert list(_trace(fig, "Shares")["marker"]["pattern"]["shape"]) == ["", ""]
    assert not any(t.get("name") == "Holders (filed so far)" for t in fig["data"])
    notes = " ".join(a.get("text", "") for a in fig["layout"].get("annotations", []))
    assert "still arriving" not in notes


def test_the_snapshot_cells_name_their_quarter():
    """The legend says the snapshot is the last settled quarter's, "named on each cell"; nothing named it (#521)."""
    _two_quarters()
    quarters, entries = report.board_entries(sec_13f.load_totals(), {})
    payload = report._payload(quarters, entries)
    cells = dict(zip(payload["cols"], payload["rows"][0][3]))

    for col in ("value", "pct_out", "holders", "new_exit", "breadth", "top10", "hhi", "netflow"):
        assert len(cells[col]) == 4 and report._qlabel(SETTLED.isoformat()) in cells[col][3], col
