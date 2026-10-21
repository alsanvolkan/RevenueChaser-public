"""E1 (#298, D13): the Earnings Fetch keeps the UW earnings rows it already reads in ``earnings_history``.

``_score`` reads ``/earnings/{t}`` (R4's tape and the drawer) and ``/stock/{t}/earnings`` (R2's EPS prints). The
scoring workers hand both answers to the run's capture; the runner flushes it once, after the run, so a worker never
writes. A print is appended only when its fields changed (change history, rule A); a daytime run appends a change
too. No request is added. Offline fake UW, temp store.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import ledger, rawstore
from RevenueChaser.module_earnings_radar import earnings_store as es
from RevenueChaser.module_earnings_radar.pipeline import EarningsPipeline
from RevenueChaser.module_earnings_radar.tests.test_pipeline_score import _FakeUW

# Thu 2026-10-08: 20:35 UTC = 16:35 ET (session complete); 15:00 UTC = 11:00 ET (intraday).
COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=timezone.utc).timestamp()
INTRADAY = datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc).timestamp()

EARN = [  # /earnings/{t}, newest first: the upcoming event, then a past print
    {"report_date": "2026-10-29", "report_time": "postmarket", "actual_eps": None, "street_mean_est": "1.70",
     "expected_move_perc": "0.061", "post_earnings_move_1d": None, "ending_fiscal_quarter": "2026-09-30",
     "source": "company"},
    {"report_date": "2026-07-30", "report_time": "postmarket", "actual_eps": "1.64", "street_mean_est": "1.60",
     "expected_move_perc": "0.052", "post_earnings_move_1d": "-0.031", "ending_fiscal_quarter": "2026-06-30",
     "source": "company"},
]
STOCK = [  # /stock/{t}/earnings: statement-style prints
    {"ticker": "AAPL", "report_date": "2026-07-30", "report_time": "postmarket", "report_type": "quarterly",
     "reported_eps": "1.64", "estimated_eps": "1.60", "surprise_percentage": "2.5", "fiscal_date_ending": "2026-06-30",
     "inserted_at": "2026-07-31 12:00:00+00:00", "updated_at": "2026-07-31 12:00:00+00:00"},
    {"ticker": "AAPL", "report_date": None, "report_type": "quarterly", "reported_eps": None, "estimated_eps": "1.7",
     "fiscal_date_ending": "2026-09-30"},
]


@pytest.fixture(autouse=True)
def store(rawstore_tmp_db, monkeypatch):
    ledger.take_notes()
    monkeypatch.setattr(es, "_clock", lambda: COMPLETE)
    return rawstore_tmp_db


def _rows():
    return rawstore.query(f"SELECT * FROM {es.TABLE} ORDER BY endpoint, report_date, run_at")


def _capture(earn=EARN, stock=STOCK, ticker="AAPL"):
    c = es.Capture()
    c.add(ticker, es.EARNINGS, earn)
    c.add(ticker, es.STOCK_EARNINGS, stock)
    return c


# ── the mappers ──

def test_an_earnings_row_maps_to_the_typed_columns():
    (up, past) = es.rows_for("aapl", es.EARNINGS, EARN)
    assert (past["ticker"], past["endpoint"], past["report_date"]) == ("AAPL", "/earnings/{ticker}", date(2026, 7, 30))
    assert (past["actual_eps"], past["eps_estimate"], past["expected_move_perc"], past["post_earnings_move_1d"]) == \
        (1.64, 1.60, 0.052, -0.031)
    assert past["fiscal_period_end"] == date(2026, 6, 30) and past["report_time"] == "postmarket"
    assert past["surprise_pct"] is None and past["report_type"] is None and past["source"] == "uw"
    assert past["payload"] == EARN[1]
    assert up["actual_eps"] is None and up["eps_estimate"] == 1.70       # the upcoming print is kept too


def test_a_stock_earnings_row_maps_to_the_typed_columns_and_one_without_a_date_is_skipped():
    (row,) = es.rows_for("AAPL", es.STOCK_EARNINGS, STOCK)
    assert row["endpoint"] == "/stock/{ticker}/earnings" and row["report_date"] == date(2026, 7, 30)
    assert (row["actual_eps"], row["eps_estimate"], row["surprise_pct"]) == (1.64, 1.60, 2.5)
    assert row["report_type"] == "quarterly" and row["fiscal_period_end"] == date(2026, 6, 30)
    assert row["expected_move_perc"] is None and row["post_earnings_move_1d"] is None


@pytest.mark.parametrize("annual_first", [True, False])
def test_an_annual_row_on_the_date_of_a_quarterly_row_yields_to_it(annual_first):
    annual = {**STOCK[0], "report_type": "annual", "reported_eps": "6.10", "estimated_eps": "6.00"}
    served = [annual, STOCK[0]] if annual_first else [STOCK[0], annual]
    (row,) = es.rows_for("AAPL", es.STOCK_EARNINGS, served)
    assert row["report_type"] == "quarterly" and row["actual_eps"] == 1.64


# ── the writer: append on change ──

def test_a_first_run_appends_every_print_with_its_stamps():
    res = _capture().flush(run_id="job-1")
    assert res == {"appended": 3, "unchanged": 0}
    rows = _rows()
    assert len(rows) == 3
    assert {(r["run_id"], r["run_at"], r["status"], r["valid_from"], r["fetched_at"]) for r in rows} == \
        {("job-1", COMPLETE, "present", date(2026, 10, 8), COMPLETE)}


def test_an_unchanged_second_run_appends_nothing(monkeypatch):
    _capture().flush(run_id="job-1")
    monkeypatch.setattr(es, "_clock", lambda: COMPLETE + 600)
    assert es.Capture().flush(run_id="noop") == {"appended": 0, "unchanged": 0}
    assert _capture().flush(run_id="job-2") == {"appended": 0, "unchanged": 3}
    assert len(_rows()) == 3


def test_a_changed_actual_eps_appends_one_row(monkeypatch):
    _capture().flush(run_id="job-1")
    monkeypatch.setattr(es, "_clock", lambda: COMPLETE + 600)
    restated = [EARN[0], {**EARN[1], "actual_eps": "1.66"}]
    assert _capture(earn=restated).flush(run_id="job-2") == {"appended": 1, "unchanged": 2}
    got = [(r["run_id"], r["actual_eps"]) for r in _rows()
           if r["endpoint"] == es.EARNINGS and r["report_date"] == date(2026, 7, 30)]
    assert got == [("job-1", 1.64), ("job-2", 1.66)]


def test_three_runs_compare_against_the_newest_stored_row(monkeypatch):
    # Runs A, B, B: a print that changed once is appended once, not again on every later run (review E1-3).
    restated = [EARN[0], {**EARN[1], "actual_eps": "1.66"}]
    got = []
    for n, earn in enumerate((EARN, restated, restated)):
        monkeypatch.setattr(es, "_clock", lambda n=n: COMPLETE + 600 * n)
        got.append(_capture(earn=earn).flush(run_id=f"job-{n}")["appended"])
    assert got == [3, 1, 0]
    assert len(_rows()) == 4


def test_a_new_value_only_in_the_payload_is_a_change(monkeypatch):
    _capture().flush(run_id="job-1")
    monkeypatch.setattr(es, "_clock", lambda: COMPLETE + 600)
    moved = [{**EARN[0], "long_straddle_1d": "0.4"}, EARN[1]]
    assert _capture(earn=moved).flush(run_id="job-2")["appended"] == 1


def test_a_new_updated_at_alone_is_not_a_change(monkeypatch):
    _capture().flush(run_id="job-1")
    monkeypatch.setattr(es, "_clock", lambda: COMPLETE + 600)
    touched = [{**STOCK[0], "updated_at": "2026-10-08 09:00:00+00:00"}]
    assert _capture(stock=touched).flush(run_id="job-2") == {"appended": 0, "unchanged": 3}


def test_a_daytime_run_appends_a_change(monkeypatch):
    monkeypatch.setattr(es, "_clock", lambda: INTRADAY)
    assert _capture().flush(run_id="day")["appended"] == 3
    assert {r["valid_from"] for r in _rows()} == {date(2026, 10, 8)}


def test_a_failed_read_writes_nothing_and_closes_nothing():
    _capture().flush(run_id="job-1")
    assert _capture(earn=[], stock=[]).flush(run_id="job-2") == {"appended": 0, "unchanged": 0}
    assert len(_rows()) == 3


def test_a_store_failure_is_a_job_note_not_an_exception(monkeypatch):
    def boom(fn):
        raise RuntimeError("store locked")

    monkeypatch.setattr(es.rawstore, "atomic", boom)
    res = _capture().flush(run_id="job-1", label="Earnings")
    assert res["appended"] == 0 and res["error"] == "RuntimeError: store locked"
    assert any("Earnings prints were not stored" in n for n in ledger.take_notes())


# ── the pipeline: the workers capture, they never write ──

class _PrintsUW(_FakeUW):
    """A fake that serves both earnings reads like UWClient and counts them."""

    def __init__(self, earn=EARN, stock=STOCK, **ov):
        super().__init__(**ov)
        self.earn, self.stock, self.calls = earn, stock, []

    def earnings_rows(self, ticker):
        self.calls.append(("earnings_rows", ticker))
        return self.earn

    def stock_earnings(self, ticker):
        self.calls.append(("stock_earnings", ticker))
        return self.stock


_SCREENER = {"AAPL": {"close": "10", "call_volume": "0", "put_volume": "0", "total_open_interest": "0"}}
_CAL = {"AAPL": {"earnings_date": "2026-10-29", "full_name": "Aple", "sector": "Technology"}}


def _pipeline(uw):
    p = EarningsPipeline(["AAPL"], uw=uw, cal_meta=_CAL)
    p._screener_cache = _SCREENER
    p._dgs10_cache = {}
    return p


def test_scoring_captures_both_reads_with_no_extra_request_and_writes_nothing():
    uw = _PrintsUW()
    p = _pipeline(uw)
    assert p._score("AAPL") is not None
    assert uw.calls == [("earnings_rows", "AAPL"), ("stock_earnings", "AAPL")]
    assert _rows() == []                                     # a worker never writes; the runner flushes
    assert p.earnings_capture.flush(run_id="job-er") == {"appended": 3, "unchanged": 0}


def test_the_r2_fallback_to_the_calendar_rows_is_not_stored_as_stock_prints():
    p = _pipeline(_PrintsUW(stock=[]))
    p._score("AAPL")
    p.earnings_capture.flush(run_id="job-er")
    assert {r["endpoint"] for r in _rows()} == {es.EARNINGS}


def test_a_capture_failure_leaves_the_card_as_it_was(monkeypatch):
    uw = _PrintsUW()
    want = _pipeline(uw)._score("AAPL")
    p = _pipeline(uw)

    def boom(*a, **k):
        raise RuntimeError("capture broke")

    monkeypatch.setattr(p.earnings_capture, "add", boom)
    got = p._score("AAPL")
    assert got is not None and got.ticker == want.ticker
    assert (got.r2_score, got.r4_score, got.direction_score) == (want.r2_score, want.r4_score, want.direction_score)
