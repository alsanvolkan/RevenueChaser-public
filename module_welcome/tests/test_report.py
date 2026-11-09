"""The Welcome page (welcome plan §2): calendar summary, earnings, market snapshot, data freshness; Turkish."""
from __future__ import annotations

import json
from datetime import date, datetime

from RevenueChaser.core import rawstore
from RevenueChaser.module_news import calendar as cal
from RevenueChaser.module_welcome import report

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=cal.AMS).timestamp()        # Tue 10 Nov 09:00 Amsterdam
HOUR = 3600.0


def _at(hh, mm=0, day=10):
    return datetime(2026, 11, day, hh, mm, tzinfo=cal.AMS).timestamp()


def _news():
    rows = [(_at(14, 30), "CPI, Y/Y%", 3, "3.6%", "3.7%"), (_at(16, 0), "Fed President Musalem speaks", 2, None, None),
            (_at(19, 0), "Wholesale inventories", 1, None, None), (_at(20, 0), "Treasury budget", 1, None, None),
            (_at(8, 0), "Early release", 1, None, None)]
    rawstore.write_rows("news_events", [
        {"event_ts": t, "event": e, "type": "report", "importance": i, "forecast": f, "prev": p,
         "reported_period": None, "first_seen": 1.0, "last_seen": 1.0, "retired_at": None, "run_id": "t",
         "run_at": NOW} for t, e, i, f, p in rows])
    rawstore.write_rows("news_highlight", [{
        "session_date": "2026-11-10", "event_ts": _at(14, 30), "event": "CPI, Y/Y%", "type": "report",
        "importance": 3, "forecast": "3.6%", "prev": "3.7%", "text": "Tüketici fiyatları.", "text_source": "template",
        "run_id": "t", "run_at": NOW}])


def _welcome():
    rawstore.write_rows("welcome_snapshot", [
        {"series": "SPX", "session_date": date(2026, 11, 9), "close": 6012.5, "prev_close": 6000.0, "change": 12.5,
         "change_pct": 12.5 / 6000, "spark": json.dumps([5990.0, 6000.0, 6012.5]), "run_id": "t", "run_at": NOW},
        {"series": "VIX", "session_date": date(2026, 11, 9), "close": 17.2, "prev_close": 16.0, "change": 1.2,
         "change_pct": 0.075, "spark": json.dumps([16.0, 17.2]), "run_id": "t", "run_at": NOW}])
    rawstore.write_rows("welcome_earnings", [
        {"earnings_date": date(2026, 11, 10), "ticker": t, "report_session": rs, "full_name": f"{t} Inc",
         "expected_move_perc": em, "report_time": None, "source_session": date(2026, 11, 9), "first_seen": 1.0,
         "last_seen": 1.0, "retired_at": ret, "run_id": "t", "run_at": NOW}
        for t, rs, em, ret in (("CRWV", "afterhours", 0.121, None), ("AAA", "premarket", 0.05, None),
                               ("GONE", "premarket", 0.05, NOW))])
    rawstore.write_rows("welcome_freshness", [
        {"job": "spxgamma", "last_status": "error", "last_finished_at": NOW - HOUR, "last_ok_at": NOW - 30 * HOUR,
         "last_failed_at": NOW - HOUR, "last_error": "UW <503>", "run_id": "t", "run_at": NOW},
        {"job": "RC-Earnings-Inputs", "last_status": "done", "last_finished_at": NOW - 8 * HOUR,
         "last_ok_at": NOW - 8 * HOUR, "last_failed_at": None, "last_error": None, "run_id": "t", "run_at": NOW},
        {"job": "old_job", "last_status": "done", "last_finished_at": NOW - 30 * 24 * HOUR,
         "last_ok_at": NOW - 30 * 24 * HOUR, "last_failed_at": None, "last_error": None, "run_id": "t",
         "run_at": NOW}])


def test_the_calendar_card_reads_the_news_board_file():
    _news()
    page = report.render_html(now=NOW, watchlist={"CRWV"}, labels={})
    assert "bugün 5 veri" in page and "CPI, Y/Y%" in page and "%3,6" in page
    assert "Fed President Musalem speaks" in page and "Wholesale inventories" in page
    assert "Treasury budget" not in page and "Early release" not in page       # next two after now only
    assert "rc-open-tab" in page and "module_news" in page


def test_without_the_news_board_file_the_card_says_so():
    page = report.render_html(now=NOW, watchlist=set(), labels={})
    assert "News board henüz kurulmadı" in page


def test_earnings_list_watched_names_first_and_hide_retired():
    _welcome()
    page = report.render_html(now=NOW, watchlist={"CRWV"}, labels={})
    assert page.index("CRWV") < page.index("AAA") and "GONE" not in page
    assert "±%12,1" in page and "listemde" in page and "Kapanış sonrası" in page and "Açılış öncesi" in page


def test_the_snapshot_tiles_show_close_change_date_and_a_line():
    _welcome()
    page = report.render_html(now=NOW, watchlist=set(), labels={})
    assert "6.012,5" in page and "+12,5" in page and "9 Kas kapanışı" in page
    assert page.count("<svg") >= 2 and "VIX" in page
    assert 'data-dir="bad"' in page                         # VIX up reads as bad, SPX up as good
    assert 'data-dir="good"' in page


def test_freshness_flags_a_failed_job_and_drops_old_ones():
    _welcome()
    page = report.render_html(now=NOW, watchlist=set(), labels={"spxgamma": "SPX Gamma"})
    assert "SPX Gamma" in page and "UW &lt;503&gt;" in page and "RC-Earnings-Inputs" in page
    assert "old_job" not in page
    assert page.index("SPX Gamma") < page.index("RC-Earnings-Inputs")          # failures first


def test_the_session_clock_gets_the_next_sessions():
    page = report.render_html(now=NOW, watchlist=set(), labels={})
    sessions = json.loads(page.split("var SESSIONS = ", 1)[1].split(";", 1)[0])
    first = sessions[0]
    assert datetime.fromtimestamp(first[0], cal.AMS).strftime("%H:%M") == "15:30"
    assert datetime.fromtimestamp(first[1], cal.AMS).strftime("%H:%M") == "22:00"
    assert len(sessions) >= 5


def test_phone_friendly_and_themed():
    page = report.render_html(now=NOW, watchlist=set(), labels={})
    assert "max-width:900px" in page and "min-height:44px" in page and 'data-theme="light"' in page


def test_render_writes_the_board_file(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    assert report.render(now=NOW, watchlist=set(), labels={}) == tmp_path / "welcome_latest.html"


def test_a_change_that_rounds_to_zero_reads_flat():
    rawstore.write_rows("welcome_snapshot", [
        {"series": "US10Y", "session_date": date(2026, 11, 9), "close": 4.05, "prev_close": 4.052, "change": -0.002,
         "change_pct": -0.0005, "spark": "[4.052, 4.05]", "run_id": "t", "run_at": NOW}])
    page = report.render_html(now=NOW, watchlist=set(), labels={})
    assert "-0,00" not in page and "• 0,00" in page
