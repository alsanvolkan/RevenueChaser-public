"""#385 FIN-4: a store-only Refresh renders the last stored run's comps, labelled with its fetch date.

A Fetch records its report (``module_financials/history.record`` -> ``core/history.record_run``); the Refresh reads
the newest stored run through ``history.last_report`` and renders that run's comps instead of the old
"comps requires a live Fetch" error. It fetches nothing, it raises nothing, and with no stored run the board says
so. ``last_report`` opens the history file read-only: with no recorded run it writes nothing (no file is made).

Offline: a temp DuckDB, no provider call, no store write beyond the test's own seeding.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

import RevenueChaser.module_financials.run as fm
from RevenueChaser.core import rawstore
from RevenueChaser.core.providers import cache as cache_manager
from RevenueChaser.module_financials import history as fin_history

# 2026-10-06 is a Tuesday: 22:00 UTC = 18:00 ET (session complete).
COMPLETE = datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc).timestamp()
# What ``run.py`` stamps from ``ticker_payload_cache.fetched_at_iso``: when the numbers were fetched.
FETCHED_AT = "2026-10-06T21:30:00+00:00"


def _comps(peer: str) -> dict:
    return {
        "ticker": "TEST", "warnings": [], "pe_only": False,
        "peers": [{"ticker": peer, "market_cap": 2.0e9, "enterprise_value": 9.0e9,
                   "ev_ebitda": 9.0, "ev_revenue": 2.25, "pe": 18.0}],
        "multiples": {
            "ev_ebitda": {"p25": 9.0, "p50": 9.0, "p75": 9.0, "count": 1},
            "ev_revenue": {"p25": 2.25, "p50": 2.25, "p75": 2.25, "count": 1},
            "pe": {"p25": 18.0, "p50": 18.0, "p75": 18.0, "count": 1},
        },
        "implied": {"ev_ebitda": 2700.0, "ev_revenue": 2250.0, "equity_pe": 2700.0,
                    "per_share_ev_ebitda": 2.7, "per_share_ev_revenue": 2.25, "per_share_pe": 2.7,
                    "net_debt": 180.0},
    }


def _results(peer: str) -> dict:
    """What a Financials Fetch hands ``history.record``: the financials it used and the comps it computed."""
    return {
        "financials": {"ticker": "TEST", "sector": "Technology", "fetched_at_iso": FETCHED_AT},
        "modules": {"comps": _comps(peer)},
        "quick_remarks": {"ticker": "TEST", "source": "yfinance"},
        "warnings": [],
    }


def _record(peer: str, *, run_id: str, run_at: float = COMPLETE):
    return fin_history.record(_results(peer), ticker="TEST", run_id=run_id, run_at=run_at, modules=["comps"])


def _cached(financials: dict) -> dict:
    """The raw payload a Fetch leaves in ``ticker_payload_cache`` (the model inputs of a store-only render)."""
    out = copy.deepcopy(financials)
    out["info"] = {"longName": "Test Corp.", "currency": "USD"}
    return out


def _refresh(tmp_path, monkeypatch, financials: dict) -> dict:
    """The store-only Refresh: no network, the one board file, the results it rendered."""
    monkeypatch.setattr(fm, "OUTPUT_DIR", tmp_path)
    cache_manager.save("TEST", _cached(financials))
    return fm.run("TEST", modules=["comps"], output="html", no_fetch=True)


def _html(res: dict) -> str:
    return Path(res["paths"]["html"]).read_text(encoding="utf-8")


def test_last_report_returns_the_newest_stored_runs_report(rawstore_tmp_db):
    """The reader itself: the newest run, decoded (the compressed ``modules`` cell included)."""
    _record("AAA", run_id="old")
    _record("BBB", run_id="new", run_at=COMPLETE + 3600)

    report = fin_history.last_report("TEST")
    assert report["run_id"] == "new"
    assert [p["ticker"] for p in report["modules"]["comps"]["peers"]] == ["BBB"]
    assert report["as_of"] == "2026-10-06"


def test_last_report_writes_nothing_to_the_store(rawstore_tmp_db):
    """Read-only: the history file is untouched (same mtime, same row count) and a missing one is never created."""
    _record("AAA", run_id="r1")
    path = rawstore.history_db_path()
    before = path.stat().st_mtime_ns

    assert fin_history.last_report("TEST")["run_id"] == "r1"
    assert path.stat().st_mtime_ns == before
    assert len(rawstore.query("SELECT * FROM financials_report_history", db="history")) == 1

    rawstore.replace_all("ticker_payload_cache", [], allow_empty=True)
    path.unlink()
    assert fin_history.last_report("TEST") is None
    assert not path.exists()


def test_a_refresh_renders_the_last_stored_runs_comps(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """FIN-4: no error payload, the stored peers and multiples, and no module error/warning about a live Fetch."""
    _record("AAA", run_id="r1")
    res = _refresh(tmp_path, monkeypatch, financials)

    comps = res["results"]["modules"]["comps"]
    assert "error" not in comps
    assert [p["ticker"] for p in comps["peers"]] == ["AAA"]
    assert comps["multiples"]["ev_ebitda"]["p50"] == 9.0
    assert comps["stored_run_id"] == "r1"
    html = _html(res)
    assert "AAA" in html and "Comps Analysis" in html
    assert not [w for w in res["results"]["warnings"] if "comps" in w]


# ── the data date is the market's day, not UTC's (review S-4) ─────────────────────────────────────────
# 2026-10-07 02:00 UTC = 2026-10-06 22:00 ET: the UTC date is already the next day, the New York one is not.
LATE_ET = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc).timestamp()


def test_the_fetch_date_is_the_new_york_date_not_the_utc_one(rawstore_tmp_db):
    """A 22:00 ET Fetch is already Wednesday in UTC; the board's "as of" is the session's Tuesday.

    The run's moment and the stamp run.py copies from ``ticker_payload_cache.fetched_at_iso`` are both UTC
    instants. Read as UTC they labelled a Tuesday-evening Fetch Wednesday, a day the market had not traded. (The
    "as of" is the run's own session since review N6: the comps' peers are fetched by the run.)
    """
    results = _results("AAA")
    results["financials"]["fetched_at_iso"] = "2026-10-07T02:00:00+00:00"
    fin_history.record(results, ticker="TEST", run_id="late", run_at=LATE_ET, modules=["comps"])

    report = fin_history.last_report("TEST")
    assert report["data_fetched_at"] == "2026-10-07T02:00:00+00:00"      # stored as it was stamped
    assert report["as_of"] == "2026-10-06"                              # read in New York


def test_a_run_with_no_stamp_dates_from_its_moment_in_new_york(rawstore_tmp_db):
    """The fallback (the run's own moment) is read the same way: 02:00 UTC is still the previous NY day."""
    results = _results("AAA")
    results["financials"].pop("fetched_at_iso")
    fin_history.record(results, ticker="TEST", run_id="late", run_at=LATE_ET, modules=["comps"])

    report = fin_history.last_report("TEST")
    assert report["data_fetched_at"] is None
    assert report["as_of"] == "2026-10-06"


def test_the_comps_are_labelled_with_the_fetch_date(rawstore_tmp_db, tmp_path, monkeypatch, financials):
    """The label is the stored run's fetch date, not the Refresh's own clock."""
    _record("AAA", run_id="r1")
    html = _html(_refresh(tmp_path, monkeypatch, financials))
    assert "as of 2026-10-06" in html


def test_with_no_stored_run_the_board_says_so_instead_of_raising(rawstore_tmp_db, tmp_path, monkeypatch,
                                                                 financials):
    """No stored run: no comps error, an explicit empty state on the board, and nothing written to history."""
    res = _refresh(tmp_path, monkeypatch, financials)

    comps = res["results"]["modules"]["comps"]
    assert comps["not_stored"] is True and "error" not in comps
    assert "No stored comps" in _html(res)
    assert not rawstore.history_db_path().exists()


# ── FIN-4: the newest run that HAS comps ────────────────────────────────────────────────────────────────────────
def _record_failed_comps(*, run_id: str, run_at: float = COMPLETE):
    """A Fetch whose comps step errored: ``run_comps`` stores ``{"error": ...}``, and the run is recorded as partial."""
    results = _results("AAA")
    results["modules"]["comps"] = {"error": "target financial data missing", "warnings": []}
    return fin_history.record(results, ticker="TEST", run_id=run_id, run_at=run_at, modules=["comps"])


class _DecodeSpy:
    """Counts ``rawstore._decode_row`` calls, so a test can see how many stored runs the reader actually decoded."""

    def __init__(self, monkeypatch):
        self.rows = 0
        self._real = rawstore._decode_row
        monkeypatch.setattr(rawstore, "_decode_row", self._decode)

    def _decode(self, name, row):
        self.rows += 1
        return self._real(name, row)


def test_last_report_prefers_the_newest_run_that_has_comps(rawstore_tmp_db, monkeypatch):
    """A newer Fetch whose comps step errored must not hide the comps an older Fetch did compute.

    The walk is lazy: it decodes the two rows up to the hit and stops there, never the third.
    """
    _record_failed_comps(run_id="older", run_at=COMPLETE - 3600)
    _record("AAA", run_id="old")
    _record_failed_comps(run_id="new", run_at=COMPLETE + 3600)
    spy = _DecodeSpy(monkeypatch)

    report = fin_history.last_report("TEST")
    assert report["run_id"] == "old"
    assert [p["ticker"] for p in report["modules"]["comps"]["peers"]] == ["AAA"]
    assert report["modules"]["comps"]["multiples"]["ev_ebitda"]["p50"] == 9.0
    assert spy.rows == 2                             # the hit is the second row: nothing after it is decoded


def test_last_report_reads_at_most_twenty_runs(rawstore_tmp_db, monkeypatch):
    """Only the oldest of 25 stored runs has comps, and it is beyond the window: the read is bounded.

    The Refresh decodes on the thread that holds the DuckDB writer, so an unbounded read stalls the store; the
    reader looks at the newest 20 runs and falls back to the newest row, and it decodes no more than those 20.
    """
    _record("AAA", run_id="r1")                      # the only run with comps: the 25th newest
    for i in range(2, 26):
        _record_failed_comps(run_id=f"r{i}", run_at=COMPLETE + 3600 * i)
    spy = _DecodeSpy(monkeypatch)

    report = fin_history.last_report("TEST")
    assert report["run_id"] == "r25"                 # the newest row, not the 25th row back
    assert fin_history.has_comps(report) is False
    assert spy.rows <= 20


def test_with_no_run_having_comps_the_newest_run_is_returned(rawstore_tmp_db):
    """Nothing usable stored: the newest row, as before, so the board still shows its empty state."""
    _record_failed_comps(run_id="old")
    _record_failed_comps(run_id="new", run_at=COMPLETE + 3600)

    report = fin_history.last_report("TEST")
    assert report["run_id"] == "new"
    assert fin_history.has_comps(report) is False
