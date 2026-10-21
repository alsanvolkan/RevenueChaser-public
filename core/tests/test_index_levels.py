"""SPX daily levels (D03, #293, M-SPX-0 #376): yfinance ^GSPC into ``index_levels_daily`` (prices.duckdb).

Every test is offline: a fake ``yf.download`` counts the requests, the clock is passed explicitly and the
per-night markers live in the test's own folder. avo's typed words (2026-10-16): a one-time ``period="max"`` fill,
then at most 1 yfinance request a night; writes ``index_levels_daily`` only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from RevenueChaser.core import index_levels, job_lock, prices, rawstore

UTC = timezone.utc
NOW = datetime(2026, 10, 8, 20, 35, tzinfo=UTC)       # Thursday 16:35 EDT: RC-Daily-Bars at 22:35 Amsterdam
LATEST = date(2026, 10, 8)
GSPC = "^GSPC"
_FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits", "Capital Gains"]


def _d(iso: str) -> date:
    return date.fromisoformat(iso)


def _bar(close, volume=2.0e9):
    return {"Open": close - 1, "High": close + 2, "Low": close - 3, "Close": close, "Adj Close": close,
            "Volume": volume, "Dividends": 0.0, "Stock Splits": 0.0, "Capital Gains": None}


def _frame(by_date: dict[str, dict], flat: bool = False) -> pd.DataFrame:
    dates = sorted(by_date)
    index = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    if flat:
        return pd.DataFrame({f: [by_date[d][f] for d in dates] for f in _FIELDS}, index=index)
    return pd.DataFrame({(GSPC, f): [by_date[d][f] for d in dates] for f in _FIELDS}, index=index)


class FakeDownload:
    """``yf.download`` stand-in: records every request; ``raise_all`` makes each one raise."""

    def __init__(self, by_date: dict[str, dict] | None = None, flat: bool = False):
        self.by_date = dict(by_date or {})
        self.flat = flat
        self.calls: list[tuple[list[str], dict]] = []
        self.raise_all = False

    def __call__(self, tickers, **kw):
        self.calls.append((list(tickers), dict(kw)))
        if self.raise_all:
            raise RuntimeError("Yahoo is down")
        if not self.by_date:
            return pd.DataFrame()
        return _frame(self.by_date, flat=self.flat)


def _seed_levels(*isos: str, close: float = 5000.0) -> None:
    rawstore.write_rows("index_levels_daily", [
        {"symbol": GSPC, "session_date": _d(i), "open": close, "high": close, "low": close, "close": close,
         "volume": 1.0, "source": "yfinance", "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0} for i in isos])


def _seed_spy(*isos: str) -> None:
    rawstore.write_rows("price_bars_daily", [
        {"ticker": "SPY", "session_date": _d(i), "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
         "adj_close": None, "volume": 1.0, "split_ratio": None, "adj_source": None, "source": "yfinance",
         "fetched_at": 0.0, "run_id": "seed", "run_at": 0.0} for i in isos])
    rawstore.write_rows("price_checks", [{"dataset": prices.CHECK_FILL[0], "source": prices.CHECK_FILL[1],
                                           "key": "SPY", "checked_at": 0.0, "run_id": "seed"}])


def _levels() -> dict[date, dict]:
    rows = rawstore.query("SELECT * FROM index_levels_daily ORDER BY session_date", db="prices")
    return {r["session_date"]: r for r in rows}


def _night(tmp_path, dl, now=NOW, run_id="night"):
    return index_levels.nightly(run_id=run_id, now=now, yf_download=dl, marker_dir=tmp_path / "markers")


# ── the nightly step: at most one yfinance request a night ───────────────────────────────────────────────────────

def test_the_nightly_appends_the_new_complete_sessions_with_one_request(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-05", "2026-10-06")
    dl = FakeDownload({"2026-10-06": _bar(6000.0), "2026-10-07": _bar(6010.0), "2026-10-08": _bar(6020.0)})
    res = _night(tmp_path, dl)
    assert len(dl.calls) == 1 and res["requests"] == 1
    assert res["status"] == "ok" and res["rows_inserted"] == 2
    got = _levels()
    assert sorted(got) == [_d("2026-10-05"), _d("2026-10-06"), _d("2026-10-07"), _d("2026-10-08")]
    row = got[_d("2026-10-08")]
    assert (row["symbol"], row["open"], row["high"], row["low"], row["close"], row["volume"]) == (
        GSPC, 6019.0, 6022.0, 6017.0, 6020.0, 2.0e9)
    assert (row["source"], row["run_id"], row["run_at"], row["fetched_at"]) == (
        "yfinance", "night", NOW.timestamp(), NOW.timestamp())
    assert res["newest_after"] == "2026-10-08" and res["latest_session"] == "2026-10-08"


def test_the_nightly_request_is_one_gspc_window_from_before_the_newest_stored_session(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    dl = FakeDownload({"2026-10-07": _bar(6010.0)})
    _night(tmp_path, dl)
    (tickers, kw), = dl.calls
    assert tickers == [GSPC]
    assert kw["start"] == _d("2026-10-06") - timedelta(days=index_levels.LOOKBACK_DAYS)
    assert kw["end"] == LATEST + timedelta(days=1)
    assert kw["auto_adjust"] is False and kw["interval"] == "1d" and "period" not in kw


def test_a_rerun_on_the_same_night_makes_no_request_and_adds_no_row(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    dl = FakeDownload({"2026-10-07": _bar(6010.0), "2026-10-08": _bar(6020.0)})
    _night(tmp_path, dl)
    before = _levels()
    again = _night(tmp_path, dl, now=NOW + timedelta(hours=1), run_id="again")
    assert len(dl.calls) == 1 and again["requests"] == 0
    assert again["status"] == "current" and again["rows_inserted"] == 0
    assert _levels() == before


def test_a_second_run_the_same_night_asks_nothing_even_while_still_behind(rawstore_tmp_db, tmp_path):
    """Yahoo had not served tonight's bar yet: the run stays behind, but tonight's one request is spent."""
    _seed_levels("2026-10-06")
    dl = FakeDownload({"2026-10-07": _bar(6010.0)})
    first = _night(tmp_path, dl)
    assert first["status"] == "partial" and first["newest_after"] == "2026-10-07"
    second = _night(tmp_path, dl, now=NOW + timedelta(hours=2), run_id="second")
    assert len(dl.calls) == 1 and second["requests"] == 0
    assert second["status"] == "skipped" and "2026-10-08" in second["reason"]
    dl.by_date["2026-10-08"] = _bar(6020.0)                     # the next night asks again and closes the gap
    nxt = _night(tmp_path, dl, now=NOW + timedelta(days=1), run_id="next")
    assert len(dl.calls) == 2 and nxt["requests"] == 1 and nxt["rows_inserted"] == 1
    assert _d("2026-10-08") in _levels()


def test_the_nightly_makes_no_request_before_the_fill(rawstore_tmp_db, tmp_path):
    dl = FakeDownload({"2026-10-08": _bar(6020.0)})
    res = _night(tmp_path, dl)
    assert dl.calls == [] and res["requests"] == 0
    assert res["status"] == "skipped" and "--index-fill" in res["reason"]
    assert _levels() == {}


def test_a_weekend_run_after_a_stored_friday_is_current(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-09")                                  # Friday
    dl = FakeDownload({"2026-10-09": _bar(6020.0)})
    res = _night(tmp_path, dl, now=datetime(2026, 10, 10, 20, 35, tzinfo=UTC))
    assert dl.calls == [] and res["status"] == "current" and res["latest_session"] == "2026-10-09"


def test_an_intraday_run_writes_no_row_of_the_open_session(rawstore_tmp_db, tmp_path):
    """Rule B: at 14:00 New York the session of the day is not complete, so its bar is never written."""
    _seed_levels("2026-10-06")
    dl = FakeDownload({"2026-10-07": _bar(6010.0), "2026-10-08": _bar(6020.0)})
    res = _night(tmp_path, dl, now=datetime(2026, 10, 8, 18, 0, tzinfo=UTC))
    assert res["latest_session"] == "2026-10-07" and len(dl.calls) == 1
    assert sorted(_levels()) == [_d("2026-10-06"), _d("2026-10-07")]
    assert res["dropped_off_calendar"] == 1


def test_a_stored_row_is_never_rewritten(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06", close=5000.0)
    dl = FakeDownload({"2026-10-06": _bar(7777.0), "2026-10-07": _bar(6010.0)})
    res = _night(tmp_path, dl)
    assert res["rows_inserted"] == 1
    assert _levels()[_d("2026-10-06")]["close"] == 5000.0


def test_a_failed_request_is_one_request_with_no_retry_and_never_raises(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    dl = FakeDownload()
    dl.raise_all = True
    res = _night(tmp_path, dl)
    assert len(dl.calls) == 1 and res["requests"] == 1
    assert res["status"] == "failed" and "Yahoo is down" in res["error"]
    assert sorted(_levels()) == [_d("2026-10-06")]


def test_an_empty_download_is_a_failure_that_writes_nothing(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    dl = FakeDownload({})
    res = _night(tmp_path, dl)
    assert len(dl.calls) == 1 and res["status"] == "failed" and res["rows_inserted"] == 0


def test_a_reply_without_gspc_rows_is_a_failure(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    frame = _frame({"2026-10-07": _bar(6010.0)})
    frame.columns = pd.MultiIndex.from_tuples([("SPY", f) for _, f in frame.columns])
    calls = []
    res = _night(tmp_path, lambda tickers, **kw: calls.append(kw) or frame)
    assert len(calls) == 1 and res["status"] == "failed" and GSPC in res["error"]
    assert sorted(_levels()) == [_d("2026-10-06")]


def test_a_store_error_is_a_failed_status_not_an_exception(rawstore_tmp_db, tmp_path, monkeypatch):
    _seed_levels("2026-10-06")
    dl = FakeDownload({"2026-10-07": _bar(6010.0)})

    def boom(*a, **k):
        raise RuntimeError("store is locked")

    monkeypatch.setattr(rawstore, "atomic", boom)
    res = _night(tmp_path, dl)
    assert res["status"] == "failed" and "store is locked" in res["error"] and len(dl.calls) == 1


def test_an_unwritable_marker_makes_no_request(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    (tmp_path / "markers").write_text("a file, not a folder", encoding="utf-8")
    dl = FakeDownload({"2026-10-07": _bar(6010.0)})
    res = _night(tmp_path, dl)
    assert dl.calls == [] and res["status"] == "failed" and res["requests"] == 0


def test_a_held_lock_makes_no_request(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    dl = FakeDownload({"2026-10-07": _bar(6010.0)})
    with job_lock.acquire(index_levels.LOCK, wait=0) as got:
        assert got
        res = _night(tmp_path, dl)
    assert dl.calls == [] and res["status"] == "skipped" and res["requests"] == 0


def test_the_nightly_writes_index_levels_daily_only(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    _seed_spy("2026-10-06")
    other = {t: rawstore.query(f"SELECT * FROM {t} ORDER BY ALL", db="prices")
             for t in ("price_bars_daily", "price_actions", "price_checks")}
    raw_checks = rawstore.query("SELECT * FROM source_checks ORDER BY ALL")
    dl = FakeDownload({"2026-10-07": {**_bar(6010.0), "Dividends": 1.5, "Stock Splits": 2.0}})
    assert _night(tmp_path, dl)["rows_inserted"] == 1
    assert {t: rawstore.query(f"SELECT * FROM {t} ORDER BY ALL", db="prices") for t in other} == other
    assert rawstore.query("SELECT * FROM source_checks ORDER BY ALL") == raw_checks


def test_a_flat_single_ticker_frame_is_read_too(rawstore_tmp_db, tmp_path):
    _seed_levels("2026-10-06")
    dl = FakeDownload({"2026-10-07": _bar(6010.0)}, flat=True)
    assert _night(tmp_path, dl)["rows_inserted"] == 1


# ── the one-time period="max" fill ───────────────────────────────────────────────────────────────────────────────

SPY_DATES = ("1993-01-29", "1993-02-01", "2024-12-31", "2025-01-02", "2026-10-08")
MAX_FRAME = {
    "1950-01-03": _bar(16.66, volume=1.26e6),          # before SPY's first bar: ^GSPC is the reference (rule C)
    "1950-01-04": {**_bar(16.85), "Close": 0.0},       # a zero close is a bad bar, never stored
    "1993-01-29": _bar(438.78),                        # on SPY's dates from SPY's first bar
    "1993-02-01": _bar(442.52),
    "1993-02-02": _bar(442.55),                        # no SPY bar that day: off the reference
    "2024-12-31": _bar(5881.63),
    "2025-01-01": _bar(5881.63),                       # a holiday: off the calendar
    "2025-01-02": _bar(5868.55),
    "2026-10-08": _bar(6020.0),
    "2026-10-09": _bar(6030.0),                        # after the latest complete session
}


def test_the_fill_is_one_period_max_request_kept_on_the_rule_c_dates(rawstore_tmp_db):
    _seed_spy(*SPY_DATES)
    dl = FakeDownload(MAX_FRAME)
    res = index_levels.fill(run_id="fill", now=NOW, yf_download=dl)
    (tickers, kw), = dl.calls
    assert tickers == [GSPC] and kw["period"] == "max" and kw["auto_adjust"] is False
    assert "start" not in kw and "end" not in kw
    assert res["status"] == "ok" and res["requests"] == 1
    kept = ["1950-01-03", "1993-01-29", "1993-02-01", "2024-12-31", "2025-01-02", "2026-10-08"]
    assert sorted(_levels()) == [_d(i) for i in kept]
    assert res["rows_inserted"] == 6 and res["rows_stored"] == 6
    assert (res["dropped_off_reference"], res["dropped_off_calendar"], res["dropped_no_close"]) == (1, 2, 1)
    assert res["first_session"] == "1950-01-03" and res["newest_after"] == "2026-10-08"


def test_a_second_fill_adds_no_row(rawstore_tmp_db):
    _seed_spy(*SPY_DATES)
    dl = FakeDownload(MAX_FRAME)
    index_levels.fill(run_id="fill", now=NOW, yf_download=dl)
    before = _levels()
    again = index_levels.fill(run_id="fill-2", now=NOW, yf_download=dl)
    assert again["status"] == "ok" and again["rows_inserted"] == 0 and again["requests"] == 1
    assert _levels() == before


def test_the_fill_makes_no_request_without_spys_d01_fill(rawstore_tmp_db):
    dl = FakeDownload(MAX_FRAME)
    res = index_levels.fill(run_id="fill", now=NOW, yf_download=dl)
    assert dl.calls == [] and res["status"] == "failed" and "SPY" in res["error"]
    assert _levels() == {}


def test_the_fill_makes_no_request_while_the_lock_is_held(rawstore_tmp_db):
    _seed_spy(*SPY_DATES)
    dl = FakeDownload(MAX_FRAME)
    with job_lock.acquire(index_levels.LOCK, wait=0):
        res = index_levels.fill(run_id="fill", now=NOW, yf_download=dl)
    assert dl.calls == [] and res["status"] == "skipped"


def test_a_failed_fill_request_writes_nothing(rawstore_tmp_db):
    _seed_spy(*SPY_DATES)
    dl = FakeDownload()
    dl.raise_all = True
    res = index_levels.fill(run_id="fill", now=NOW, yf_download=dl)
    assert len(dl.calls) == 1 and res["status"] == "failed" and _levels() == {}


def test_after_the_fill_the_nightly_asks_one_window(rawstore_tmp_db, tmp_path):
    _seed_spy(*SPY_DATES)
    index_levels.fill(run_id="fill", now=NOW - timedelta(days=1), yf_download=FakeDownload(
        {k: v for k, v in MAX_FRAME.items() if k < "2026-10-08"}))
    dl = FakeDownload({"2026-10-08": _bar(6020.0)})
    res = _night(tmp_path, dl)
    assert len(dl.calls) == 1 and res["rows_inserted"] == 1 and res["status"] == "ok"


# ── the summary line ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("res, words", [
    ({"status": "ok", "latest_session": "2026-10-08", "rows_inserted": 2, "newest_after": "2026-10-08",
      "requests": 1}, ["^GSPC", "2 rows", "1 yfinance request"]),
    ({"status": "current", "latest_session": "2026-10-08", "requests": 0}, ["current", "no request"]),
    ({"status": "skipped", "reason": "fill not done", "requests": 0}, ["skipped", "fill not done"]),
    ({"status": "failed", "error": "Yahoo is down", "requests": 1}, ["failed", "Yahoo is down", "1 yfinance"]),
])
def test_the_summary_line_names_the_outcome_and_the_request_count(res, words):
    line = index_levels.summary_line(res)
    assert line.startswith("Index levels")
    for w in words:
        assert w in line
