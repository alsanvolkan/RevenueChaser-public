"""#304: the cards' next earnings date comes from the stored D26 calendar, Yahoo's value only when the store has none."""
from __future__ import annotations

from datetime import date, datetime, timezone

from RevenueChaser.core import earnings_dates as ed
from RevenueChaser.core import screen_common as sc
from RevenueChaser.core.tests.test_screen_common_147 import Board

TODAY = date(2026, 10, 22)
YAHOO = 1_790_000_000.0


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def test_the_stored_date_is_noon_utc_of_that_day():
    assert _utc(ed.epoch(date(2026, 10, 28))) == datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


def test_stored_next_dates_reads_the_newest_session_within_the_age_limit(monkeypatch):
    seen = {}

    def query(sql, params=None):
        seen["sql"], seen["params"] = sql, params
        return [{"ticker": "aaa", "d": date(2026, 10, 28)}, {"ticker": "BBB", "d": date(2026, 11, 2)},
                {"ticker": None, "d": date(2026, 11, 3)}]
    monkeypatch.setattr(ed.rawstore, "query", query)
    assert ed.stored_next_dates(TODAY) == {"AAA": date(2026, 10, 28), "BBB": date(2026, 11, 2)}
    assert seen["params"] == [TODAY, date(2026, 10, 18), TODAY]
    assert "min(earnings_date)" in seen["sql"] and "earnings_date >= ?" in seen["sql"]


def test_an_unreadable_store_leaves_yahoo_in_place(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no such table: earnings_calendar")
    monkeypatch.setattr(ed.rawstore, "query", boom)
    assert ed.stored_next_dates(TODAY) == {}


def test_with_stored_date_overlays_a_copy_and_keeps_the_rest_of_the_quote():
    row = {"ticker": "AAA", "quote": {"earningsTimestampStart": YAHOO, "price": 5.0}}
    out = ed.with_stored_date(row, {"AAA": date(2026, 10, 28)})
    assert out["quote"]["earningsTimestampStart"] == ed.epoch(date(2026, 10, 28))
    assert out["quote"]["price"] == 5.0
    assert row["quote"]["earningsTimestampStart"] == YAHOO, "the universe row itself is never changed"


def test_a_ticker_the_store_has_no_date_for_keeps_yahoos_value():
    row = {"ticker": "ZZZ", "quote": {"earningsTimestampStart": YAHOO}}
    assert ed.with_stored_date(row, {"AAA": date(2026, 10, 28)}) is row


def test_a_row_without_a_quote_gets_one():
    assert ed.with_stored_date({"ticker": "AAA"}, {"AAA": date(2026, 10, 28)})["quote"] == {
        "earningsTimestampStart": ed.epoch(date(2026, 10, 28))}


def test_run_screen_scores_the_stored_date_and_leaves_the_other_rows_alone(monkeypatch):
    rows = [{"ticker": "A", "quote": {"earningsTimestampStart": YAHOO}},
            {"ticker": "B", "quote": {"earningsTimestampStart": YAHOO}}]
    b = Board(monkeypatch, rows=rows)
    monkeypatch.setattr(sc.earnings_dates, "stored_next_dates", lambda: {"A": date(2026, 10, 28)})
    seen = {}

    def compute_row(row, spy_df, no_fetch, spine, drops):
        seen[row["ticker"]] = row["quote"]["earningsTimestampStart"]
        return {"ticker": row["ticker"]}
    b.run(compute_row=compute_row)
    assert seen == {"A": ed.epoch(date(2026, 10, 28)), "B": YAHOO}
    assert rows[0]["quote"]["earningsTimestampStart"] == YAHOO


def test_ag_scores_the_stored_date_and_falls_back_to_yahoo(rawstore_tmp_db):
    from datetime import timedelta
    from RevenueChaser.core import rawstore
    from RevenueChaser.module_asymmetric_growth import engine
    from RevenueChaser.module_asymmetric_growth.tests.test_ag_dated_momentum_380 import _QUOTE, edgar, row

    today = date.today()
    day = today + timedelta(days=6)
    rawstore.write_rows("edgar_financials", [edgar("AAA"), edgar("BBB")])
    rawstore.write_rows("earnings_calendar", [
        {"session_date": today, "earnings_date": day, "ticker": "AAA", "report_session": "pm", "item": {},
         "source": "uw"}])
    preload = engine._preload(["AAA", "BBB"], True)
    preload["as_of"] = date.min
    cards = {t: engine.enrich_and_score(t, row(t, dict(_QUOTE, earningsTimestampStart=YAHOO)), no_fetch=True, spine={},
                                        preload=preload) for t in ("AAA", "BBB")}
    assert cards["AAA"]["earnings_date"] == ed.epoch(day)
    assert cards["BBB"]["earnings_date"] == YAHOO
