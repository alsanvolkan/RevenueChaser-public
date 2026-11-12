"""The 13F board's break mark and quarter state (#732, owner 12 Nov 2026).

The flows count only the filers that reported in both quarters; a filer that started or stopped filing is left out.
The board marks such a ticker beside its name, and the mark opens the list of breaks by quarter, newest first
("akista bir kesilme var ise uyari notu olarak gosterelim o hissenin yaninda, tiklayinca da siraya nerede
gerceklestigi gozuksun"). The header says whether the newest settled quarter is closed: the SEC data set covering
its deadline is published ("Çeyrek SEC o 3 ayı kapsayan zip dosyasini yazdiginda tam sayılsin ... kapansin").
"""
from __future__ import annotations

from datetime import date

from RevenueChaser.core import ownership
from RevenueChaser.module_sec_13f import report

Q1, Q2, Q3 = "2026-03-31", "2026-06-30", "2026-09-30"
META = {"AAA": {"name": "Alpha", "sector": "Technology"}, "BBB": {"name": "Beta", "sector": "Technology"}}


def _cell(shares, complete=True, closed=None):
    c = {"shares": shares, "value": None, "holders": 1, "new_holders": 0, "exited_holders": 0, "top10_pct": None,
         "hhi": None, "buy_flow": 0.0, "sell_flow": 0.0, "breadth": None, "breadth_chg": None, "complete": complete}
    if closed is not None:
        c["closed"] = closed
    return c


TOTALS = {"AAA": {Q1: _cell(100.0), Q2: _cell(90.0, closed=True)},
          "BBB": {Q1: _cell(50.0), Q2: _cell(55.0, closed=True)}}
BREAKS = {"AAA": {"breaks": [
    {"period": Q2, "kind": "stopped", "shares": 8.0, "cik": "1", "filer_name": "Big Fund"},
    {"period": Q2, "kind": "stopped", "shares": 2.0, "cik": "2", "filer_name": "Small Fund"},
    {"period": Q2, "kind": "started", "shares": 1.0, "cik": "3", "filer_name": "New Fund"},
    {"period": Q1, "kind": "stopped", "shares": 4.0, "cik": "4", "filer_name": "Old Fund"},
]}}


def _row(payload, ticker):
    (row,) = [r for r in payload["rows"] if r[0] == ticker]
    return row


def test_a_ticker_with_breaks_carries_them_newest_first_and_one_without_carries_none():
    payload = report.board_payload(TOTALS, META, BREAKS)
    breaks = _row(payload, "AAA")[4]
    assert [b[0] for b in breaks] == ["2026 Q2", "2026 Q1"]
    assert breaks[0][1][0] == "2 filers stopped filing, 10 shares"
    assert breaks[0][1][1] == "Largest: Big Fund (8), Small Fund (2)"
    assert breaks[0][1][2] == "1 filer started filing, 1 shares"
    assert _row(payload, "BBB")[4] == []


def test_the_break_shares_are_on_todays_basis():
    inputs = {"AAA": {**BREAKS["AAA"], "split_factor": {Q1: 0.5, Q2: 0.5}}}      # a 2-for-1 split after both
    breaks = _row(report.board_payload(TOTALS, META, inputs), "AAA")[4]
    assert breaks[0][1][0] == "2 filers stopped filing, 20 shares"


def test_a_break_of_a_quarter_still_arriving_or_outside_the_window_is_not_listed():
    totals = {"AAA": {Q1: _cell(100.0), Q2: _cell(90.0), Q3: _cell(10.0, complete=False)}}
    inputs = {"AAA": {"breaks": [{"period": Q3, "kind": "stopped", "shares": 5.0, "cik": "9", "filer_name": "X"},
                                 {"period": "2020-03-31", "kind": "stopped", "shares": 5.0, "cik": "9",
                                  "filer_name": "X"}]}}
    assert _row(report.board_payload(totals, META, inputs), "AAA")[4] == []


def test_the_page_has_the_mark_and_the_list_and_says_the_quarter_is_closed():
    html = report.render_sec13f_dashboard(TOTALS, META, BREAKS)
    assert 'id="breaks"' in html and "openBreaks(" in html and "break-mark" in html
    assert '<span class="closed">2026 Q2 closed</span>' in html


def test_a_settled_quarter_not_yet_closed_waits_for_the_data_set():
    totals = {"AAA": {Q1: _cell(100.0), Q2: _cell(90.0, closed=False)}}
    assert report.quarter_status(totals, [Q1, Q2]) == [("waiting", "2026 Q2 settled, waits for the SEC data set")]


def test_a_store_that_cannot_say_shows_no_closed_state_but_still_the_arriving_quarter():
    totals = {"AAA": {Q1: _cell(100.0), Q2: _cell(90.0), Q3: _cell(10.0, complete=False)}}
    assert report.quarter_status(totals, [Q1, Q2, Q3]) == [("waiting", "2026 Q3 still arriving")]


def test_the_shared_reader_groups_the_breaks_by_ticker(monkeypatch):
    from RevenueChaser.rcdata import read
    seen = {}

    def fake(tickers):
        seen["tickers"] = tickers
        return [{"ticker": "AAA", "period": date(2026, 6, 30), "kind": "stopped", "shares": 8.0, "cik": "1",
                 "filer_name": "Big Fund"}]
    monkeypatch.setattr(read, "filer_breaks_13f", fake)
    assert ownership.sec13f_filer_breaks(["aaa", "AAA", None]) == {
        "AAA": [{"period": Q2, "kind": "stopped", "shares": 8.0, "cik": "1", "filer_name": "Big Fund"}]}
    assert seen["tickers"] == ["AAA"]


def test_an_unreadable_store_gives_no_breaks_never_a_failed_board(monkeypatch):
    from RevenueChaser.rcdata import read

    def boom(tickers):
        raise RuntimeError("store locked")
    monkeypatch.setattr(read, "filer_breaks_13f", boom)
    assert ownership.sec13f_filer_breaks(["AAA"]) == {}


def test_a_break_under_one_percent_of_the_quarter_is_not_marked():
    """Owner, 12 Nov 2026 (screenshot: the mark on nearly every large name): only a quarter whose started and
    stopped filers hold at least 1% of the ticker's 13F shares is marked."""
    totals = {"AAA": {Q1: _cell(1000.0), Q2: _cell(1000.0, closed=True)}}
    inputs = {"AAA": {"breaks": [
        {"period": Q2, "kind": "stopped", "shares": 6.0, "cik": "1", "filer_name": "Small"},
        {"period": Q2, "kind": "started", "shares": 3.0, "cik": "2", "filer_name": "Smaller"},      # 0.9%
        {"period": Q1, "kind": "stopped", "shares": 10.0, "cik": "3", "filer_name": "Big enough"},  # 1.0%
    ]}}
    breaks = _row(report.board_payload(totals, META, inputs), "AAA")[4]
    assert [b[0] for b in breaks] == ["2026 Q1"]


def test_the_sector_comes_from_the_equity_spine_when_the_board_meta_has_none():
    """Owner's screenshot, 12 Nov 2026: every row read ``Unclassified`` because the board's own meta table was no
    longer filled; the spine's sector (the one the Sector filter matches) is shown."""
    meta = {"AAA": {"name": "Alpha"}, "BBB": {"name": "Beta", "sector": "Technology"}}
    fields = [{"ticker": "AAA", "name": "Alpha", "sector": "Healthcare"}, {"ticker": "BBB", "name": "Beta"}]
    payload = report.board_payload(TOTALS, meta, BREAKS, fields)
    assert _row(payload, "AAA")[2] == "Healthcare"
    assert _row(payload, "BBB")[2] == "Technology"
