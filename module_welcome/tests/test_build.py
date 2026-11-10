"""The Welcome board's incremental build into ``boards/welcome.duckdb`` (welcome plan §4, DECISIONS S16).

Three parts, each upserted from what changed since its own mark, nothing deleted: the market snapshot (SPX, VIX,
MOVE, US 10-year), the earnings of today and the next trading day, and the last run of every job.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from RevenueChaser.core import rawstore
from RevenueChaser.module_welcome import build as wb

NY = wb.NY
NOW = datetime(2026, 11, 10, 8, 0, tzinfo=NY).timestamp()          # Tue 10 Nov, before the open
DAYS = [date(2026, 10, 12) + timedelta(days=i) for i in range(29)]
SESSIONS = [d for d in DAYS if d.weekday() < 5 and d <= date(2026, 11, 9)]


def _prices(sessions, base=6000.0):
    rawstore.write_rows("index_levels_daily", [
        {"symbol": "^GSPC", "session_date": d, "open": None, "high": None, "low": None, "close": base + i,
         "volume": None, "source": "yfinance", "fetched_at": 1.0, "run_id": "s", "run_at": 1.0}
        for i, d in enumerate(sessions)])


def _vol(symbol, sessions, base):
    rows = []
    for i, d in enumerate(sessions):
        for src, bump, at in (("cboe", 0.0, 1.0), ("uw", 0.5, 2.0)):          # two sources; the newest fetch wins
            rows.append({"symbol": symbol, "session_date": d, "source": src, "open": None, "high": None, "low": None,
                         "close": base + i + bump, "operator": None, "fetched_at": at, "run_id": "s", "run_at": at})
    rawstore.write_rows("vol_index_daily", rows)


def _ten_year(sessions):
    rawstore.write_rows("macro_series", [
        {"series_id": "DGS10", "obs_date": d, "source": "fred", "value": 4.0 + i / 100, "frequency": "daily",
         "proxy_for": None, "session_date": d, "fetched_at": 1.0, "run_id": "s", "run_at": 1.0}
        for i, d in enumerate(sessions)])


def _snap():
    return {r["series"]: r for r in rawstore.query("SELECT * FROM welcome_snapshot", db="welcome")}


def test_the_snapshot_keeps_the_last_close_its_change_and_a_20_day_line():
    _prices(SESSIONS)
    _vol("VIX", SESSIONS, 15.0)
    _ten_year(SESSIONS)
    out = wb.build(now=NOW)
    snap = _snap()
    spx = snap["SPX"]
    assert spx["session_date"] == SESSIONS[-1] and spx["close"] == 6000.0 + len(SESSIONS) - 1
    assert spx["change"] == 1.0 and len(json.loads(spx["spark"])) == wb.SPARK_DAYS
    assert snap["VIX"]["close"] == 15.0 + len(SESSIONS) - 1 + 0.5               # the newer source
    assert snap["US10Y"]["close"] == 4.0 + (len(SESSIONS) - 1) / 100
    assert "MOVE" not in snap and out["snapshot"] == 3


def test_a_second_build_with_no_new_session_writes_no_snapshot_row():
    _prices(SESSIONS[:-1])
    wb.build(now=NOW)
    assert wb.build(now=NOW + 60)["snapshot"] == 0
    _prices(SESSIONS)
    assert wb.build(now=NOW + 120)["snapshot"] == 1
    rows = rawstore.query("SELECT session_date FROM welcome_snapshot WHERE series = 'SPX' ORDER BY 1", db="welcome")
    assert [r["session_date"] for r in rows] == SESSIONS[-2:]                    # both days kept, none deleted


def _earn(session, rows, fetched_at):
    rawstore.write_rows("earnings_calendar", [
        {"session_date": session, "earnings_date": d, "ticker": t, "report_session": rs, "sector": None,
         "full_name": f"{t} Inc", "marketcap": None, "expected_move_perc": em, "street_mean_est": None,
         "pre_earnings_close": None, "post_earnings_close": None, "report_time": None, "item": "{}",
         "source": "uw", "fetched_at": fetched_at, "run_id": "s", "run_at": fetched_at}
        for d, t, rs, em in rows])


def _earnings():
    return {r["ticker"]: r for r in rawstore.query("SELECT * FROM welcome_earnings", db="welcome")}


TUE, WED = date(2026, 11, 10), date(2026, 11, 11)


def test_earnings_of_today_and_the_next_trading_day_only():
    _earn(date(2026, 11, 9), [(TUE, "CRWV", "afterhours", 0.12), (WED, "OKLO", "afterhours", 0.09),
                              (date(2026, 11, 20), "NVDA", "afterhours", 0.07)], 100.0)
    out = wb.build(now=NOW)
    got = _earnings()
    assert set(got) == {"CRWV", "OKLO"} and out["earnings"] == 2
    assert got["CRWV"]["expected_move_perc"] == 0.12 and got["CRWV"]["retired_at"] is None


def test_a_name_the_newest_snapshot_drops_from_its_day_and_session_is_retired():
    _earn(date(2026, 11, 9), [(TUE, "CRWV", "afterhours", 0.12), (TUE, "AAA", "afterhours", 0.05),
                              (TUE, "BBB", "premarket", 0.04)], 100.0)
    wb.build(now=NOW)
    _earn(date(2026, 11, 10), [(TUE, "CRWV", "afterhours", 0.11)], 200.0)     # BBB's session is not in this one
    out = wb.build(now=NOW + 60)
    got = _earnings()
    assert got["AAA"]["retired_at"] == NOW + 60 and got["BBB"]["retired_at"] is None
    assert got["CRWV"]["expected_move_perc"] == 0.11 and out["retired"] == 1


def _run(run_id, job, status, finished, registry=None, error=None):
    return {"run_id": run_id, "job_id": run_id, "module": job, "lane": "fast", "trigger": "manual",
            "source": "dashboard", "pid": 1, "registry_name": registry, "params": "{}", "queued_at": NOW - 5,
            "started_at": NOW - 4, "finished_at": finished, "status": status, "error": error, "message": None,
            "counts": None}


def test_freshness_keeps_each_jobs_last_run_last_success_and_last_failure():
    rawstore.write_rows("job_runs", [
        _run("a", "spxgamma", "done", NOW - 7200), _run("b", "spxgamma", "error", NOW - 3600, error="UW 503"),
        _run("c", "nightly", "done", NOW - 600, registry="RC-Earnings-Calendar"),
        _run("d", "module_leaps", "running", None)])
    wb.build(now=NOW)
    fr = {r["job"]: r for r in rawstore.query("SELECT * FROM welcome_freshness", db="welcome")}
    assert fr["spxgamma"]["last_status"] == "error" and fr["spxgamma"]["last_ok_at"] == NOW - 7200
    assert fr["spxgamma"]["last_failed_at"] == NOW - 3600 and fr["spxgamma"]["last_error"] == "UW 503"
    assert fr["RC-Earnings-Calendar"]["last_status"] == "done"
    assert "module_leaps" not in fr                                          # a running job has not finished


def test_a_done_run_that_finished_with_gaps_is_not_a_success():
    gap = {**_run("b", "module_news", "done", NOW - 600),
           "message": "Finished with gaps: 12 of 12 Interactive Brokers calls failed (headlines: no API session)."}
    rawstore.write_rows("job_runs", [_run("a", "module_news", "done", NOW - 7200), gap])
    wb.build(now=NOW)
    fr = {r["job"]: r for r in rawstore.query("SELECT * FROM welcome_freshness", db="welcome")}["module_news"]
    assert fr["last_status"] == "gaps" and fr["last_ok_at"] == NOW - 7200
    assert fr["last_error"].startswith("Finished with gaps: 12 of 12")


def test_freshness_reads_only_runs_finished_after_its_mark():
    rawstore.write_rows("job_runs", [_run("a", "spxgamma", "done", NOW - 7200)])
    wb.build(now=NOW)
    assert wb.build(now=NOW + 60)["freshness"] == 0
    rawstore.write_rows("job_runs", [_run("b", "spxgamma", "done", NOW)])
    assert wb.build(now=NOW + 120)["freshness"] == 1


def test_the_build_writes_only_its_own_board_file():
    _prices(SESSIONS)
    wb.build(now=NOW)
    assert rawstore.query("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'welcome_%'") == []


def test_a_corrected_close_for_the_stored_session_is_written():
    _ten_year(SESSIONS)
    wb.build(now=NOW)
    rawstore.write_rows("macro_series", [
        {"series_id": "DGS10", "obs_date": SESSIONS[-1], "source": "fred_revised", "value": 4.5, "frequency": "daily",
         "proxy_for": None, "session_date": SESSIONS[-1], "fetched_at": 5.0, "run_id": "s", "run_at": 5.0}])
    assert wb.build(now=NOW + 60)["snapshot"] == 1
    assert _snap()["US10Y"]["close"] == 4.5
