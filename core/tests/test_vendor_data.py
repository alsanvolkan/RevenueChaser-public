"""Vendor statements and valuation (#297, D12): the mappers and the one writer.

Offline: the payloads are hand-built in the shapes ``core/providers/yfinance.get_financials`` and UW
``/stock/{t}/financials`` return (docs/openapi.yaml "Full Financials"), and the clock is passed as ``run_at``.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import pytest

from RevenueChaser.core import rawstore, vendor_data

UTC = timezone.utc
COMPLETE = datetime(2026, 10, 8, 20, 35, tzinfo=UTC).timestamp()      # Thu 16:35 EDT: session 10-08, complete
INTRADAY = datetime(2026, 10, 8, 15, 0, tzinfo=UTC).timestamp()       # Thu 11:00 EDT: session 10-08, intraday
LATER_SAME = datetime(2026, 10, 8, 23, 0, tzinfo=UTC).timestamp()     # Thu 19:00 EDT: session 10-08 again
NEXT_DAY = datetime(2026, 10, 9, 20, 35, tzinfo=UTC).timestamp()      # Fri 16:35 EDT: session 10-09


def _yf_payload(revenue=100.0, ebit=20.0, total_assets=500.0, ocf=30.0, period_end="2025-12-31", info=None):
    return {
        "ticker": "AAA", "source": "yfinance", "info": info if info is not None else {},
        "annual": [{
            "fiscal_year": period_end[:4], "period_end": period_end,
            "revenue": revenue, "cost_of_revenue": 60.0, "gross_profit": 40.0, "ebit": ebit, "ebitda": 25.0,
            "pretax_income": None, "tax_provision": None, "net_income": 10.0, "interest_expense": 2.0,
            "diluted_shares": None, "basic_shares": None, "rd_expense": None,
            "operating_cash_flow": ocf, "capital_expenditure": 5.0, "depreciation": None,
            "total_assets": total_assets, "total_equity": None, "cash": 7.0, "receivables": 8.0,
            "inventory": 9.0, "payables": None, "current_assets": None, "current_liabilities": None,
            "total_debt": 11.0,
        }],
    }


def _uw_fin(revenue="1200", updated_at="2026-07-29T12:34:56Z", report_type="quarterly", fd="2026-06-30"):
    return {
        "income_statements": [{
            "ticker": "BBB", "fiscal_date_ending": fd, "report_type": report_type,
            "total_revenue": revenue, "gross_profit": "480", "operating_income": "300", "ebitda": "350",
            "net_income": "90", "interest_expense": "12", "inserted_at": "2026-07-01T00:00:00Z",
            "updated_at": updated_at,
        }],
        "balance_sheets": [{
            "ticker": "BBB", "fiscal_date_ending": fd, "report_type": report_type,
            "total_assets": "10000", "inventory": "0", "net_receivables": "250", "deferred_revenue": "40",
            "updated_at": updated_at,
        }],
        "cash_flows": [{
            "ticker": "BBB", "fiscal_date_ending": fd, "report_type": report_type,
            "operating_cashflow": "130", "net_income": "90", "updated_at": updated_at,
        }],
        "earnings": [{"fiscal_date_ending": fd}],
    }


_INFO = {"enterpriseValue": 5.0e9, "ebitda": 4.0e8, "totalRevenue": 2.0e9, "netIncomeToCommon": 1.5e8,
         "trailingPE": 22.5, "beta": 1.1, "currency": "USD", "longBusinessSummary": "Makes widgets.",
         "marketCap": 4.5e9, "sector": "Industrials"}


def _rows(table, ticker=None):
    sql = f"SELECT * FROM {table}" + (" WHERE ticker = ?" if ticker else "") + " ORDER BY run_at, statement"
    order = sql if table == "vendor_financials" else sql.replace(", statement", "")
    return rawstore.query(order, [ticker] if ticker else None)


# ── the mappers ──────────────────────────────────────────────────────────────

def test_yfinance_statements_split_into_three_rows():
    rows = vendor_data.statement_rows_yfinance("aaa", _yf_payload())
    by = {r["statement"]: r for r in rows}
    assert sorted(by) == ["balance", "cash_flow", "income"]
    inc = by["income"]
    assert (inc["ticker"], inc["source"], inc["report_type"], inc["period_end"]) == \
        ("AAA", "yfinance", "annual", date(2025, 12, 31))
    assert inc["revenue"] == 100.0 and inc["gross_profit"] == 40.0 and inc["ebitda"] == 25.0
    assert inc["operating_income"] == 20.0          # yfinance's canonical ``ebit`` (EBIT, then Operating Income)
    assert inc["interest_expense"] == 2.0 and inc["total_assets"] is None
    assert inc["payload"]["cost_of_revenue"] == 60.0 and "total_assets" not in inc["payload"]
    bal = by["balance"]
    assert (bal["total_assets"], bal["inventory"], bal["receivables"], bal["total_debt"]) == (500.0, 9.0, 8.0, 11.0)
    assert bal["revenue"] is None and bal["payload"]["cash"] == 7.0
    assert by["cash_flow"]["operating_cash_flow"] == 30.0 and by["cash_flow"]["payload"]["capital_expenditure"] == 5.0


def test_yfinance_statement_with_no_value_and_record_without_period_end_are_skipped():
    p = _yf_payload()
    for k in ("operating_cash_flow", "capital_expenditure", "depreciation"):
        p["annual"][0][k] = None
    assert "cash_flow" not in {r["statement"] for r in vendor_data.statement_rows_yfinance("AAA", p)}
    p["annual"][0].pop("period_end")
    assert vendor_data.statement_rows_yfinance("AAA", p) == []
    assert vendor_data.statement_rows_yfinance("AAA", {"info": {"x": 1}, "annual": []}) == []
    assert vendor_data.statement_rows_yfinance("AAA", None) == []


def test_uw_statements_map_the_served_fields():
    rows = vendor_data.statement_rows_uw("bbb", _uw_fin())
    by = {r["statement"]: r for r in rows}
    assert sorted(by) == ["balance", "cash_flow", "income"]
    inc = by["income"]
    assert (inc["ticker"], inc["source"], inc["report_type"], inc["period_end"]) == \
        ("BBB", "uw", "quarterly", date(2026, 6, 30))
    assert (inc["revenue"], inc["gross_profit"], inc["operating_income"], inc["ebitda"], inc["net_income"],
            inc["interest_expense"]) == (1200.0, 480.0, 300.0, 350.0, 90.0, 12.0)
    assert inc["payload"]["total_revenue"] == "1200"               # the row as served
    bal = by["balance"]
    assert bal["inventory"] == 0.0                                 # an explicit 0 stays 0
    assert bal["receivables"] == 250.0 and bal["deferred_revenue"] == 40.0
    assert bal["rpo"] is None and bal["total_debt"] is None        # no UW field is named for them
    assert by["cash_flow"]["operating_cash_flow"] == 130.0         # UW's field is ``operating_cashflow``


def test_uw_rows_without_a_date_are_skipped_and_a_missing_report_type_is_unknown():
    fin = _uw_fin()
    fin["income_statements"][0]["fiscal_date_ending"] = ""
    fin["balance_sheets"][0].pop("report_type")
    rows = vendor_data.statement_rows_uw("BBB", fin)
    assert {r["statement"] for r in rows} == {"balance", "cash_flow"}
    assert {r["statement"]: r["report_type"] for r in rows}["balance"] == "unknown"
    assert vendor_data.statement_rows_uw("BBB", {}) == []
    assert vendor_data.statement_rows_uw("BBB", None) == []


def test_valuation_row_from_info():
    row = vendor_data.valuation_row_yfinance("aaa", _INFO)
    assert (row["ticker"], row["source"]) == ("AAA", "yfinance")
    assert (row["enterprise_value"], row["ebitda"], row["total_revenue"], row["net_income_to_common"],
            row["trailing_pe"], row["beta"], row["currency"], row["long_business_summary"]) == \
        (5.0e9, 4.0e8, 2.0e9, 1.5e8, 22.5, 1.1, "USD", "Makes widgets.")
    assert row["payload"] == _INFO
    assert vendor_data.valuation_row_yfinance("AAA", {}) is None
    assert vendor_data.valuation_row_yfinance("AAA", None) is None


def test_non_finite_numbers_are_stored_as_null():
    p = _yf_payload(revenue=float("inf"))
    p["annual"][0]["cash"] = float("nan")
    inc = {r["statement"]: r for r in vendor_data.statement_rows_yfinance("AAA", p)}
    assert inc["income"]["revenue"] is None and inc["income"]["payload"]["revenue"] is None
    assert inc["balance"]["payload"]["cash"] is None
    vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", p), run_id="r1", run_at=COMPLETE)
    assert len(_rows("vendor_financials")) == 3


# ── the writer: statements (change history, rule A) ─────────────────────────

def test_first_write_appends_every_statement_with_the_run_stamps():
    res = vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload()),
                             run_id="r1", run_at=COMPLETE)
    assert res["statements_appended"] == 3 and res["statements_unchanged"] == 0
    rows = _rows("vendor_financials")
    assert {r["status"] for r in rows} == {"present"}
    assert {r["valid_from"] for r in rows} == {date(2026, 10, 8)}
    assert {r["run_id"] for r in rows} == {"r1"} and {r["run_at"] for r in rows} == {COMPLETE}
    assert all(r["fetched_at"] for r in rows)


def test_an_unchanged_statement_appends_nothing():
    vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload()), run_id="r1",
                       run_at=COMPLETE)
    res = vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload()), run_id="r2",
                             run_at=NEXT_DAY)
    assert res["statements_appended"] == 0 and res["statements_unchanged"] == 3
    assert len(_rows("vendor_financials")) == 3


def test_a_restated_value_appends_one_row_and_keeps_the_old_one():
    vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload()), run_id="r1",
                       run_at=COMPLETE)
    res = vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload(revenue=101.0)),
                             run_id="r2", run_at=NEXT_DAY)
    assert res["statements_appended"] == 1
    inc = [r for r in _rows("vendor_financials") if r["statement"] == "income"]
    assert [(r["revenue"], r["run_id"], r["valid_from"]) for r in inc] == \
        [(100.0, "r1", date(2026, 10, 8)), (101.0, "r2", date(2026, 10, 9))]


def test_a_restated_value_seen_again_appends_nothing():
    """The comparison is against the NEWEST stored row of the key, not the first one."""
    for i, (rev, when) in enumerate([(100.0, COMPLETE), (101.0, NEXT_DAY), (101.0, NEXT_DAY + 3600)]):
        res = vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload(revenue=rev)),
                                 run_id=f"r{i}", run_at=when)
    assert res["statements_appended"] == 0
    assert [r["revenue"] for r in _rows("vendor_financials") if r["statement"] == "income"] == [100.0, 101.0]


def test_a_change_in_an_untyped_payload_field_appends_a_row():
    vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload()), run_id="r1",
                       run_at=COMPLETE)
    p = _yf_payload()
    p["annual"][0]["cost_of_revenue"] = 61.0                       # no column of its own
    res = vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", p), run_id="r2",
                             run_at=NEXT_DAY)
    assert res["statements_appended"] == 1


def test_a_new_period_is_a_new_key():
    vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload()), run_id="r1",
                       run_at=COMPLETE)
    res = vendor_data.record(
        statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload(period_end="2026-12-31")),
        run_id="r2", run_at=NEXT_DAY)
    assert res["statements_appended"] == 3


def test_uw_updated_at_alone_is_not_a_change():
    vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()), run_id="r1", run_at=COMPLETE)
    res = vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin(updated_at="2026-10-09T01:00Z")),
                             run_id="r2", run_at=NEXT_DAY)
    assert res["statements_appended"] == 0
    res = vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin(revenue="1250")),
                             run_id="r3", run_at=NEXT_DAY + 60)
    assert res["statements_appended"] == 1


def test_sources_and_report_types_are_separate_keys():
    vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()), run_id="r1", run_at=COMPLETE)
    res = vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin(report_type="annual")),
                             run_id="r2", run_at=NEXT_DAY)
    assert res["statements_appended"] == 3


def test_a_period_that_leaves_the_vendor_window_is_not_closed():
    vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()), run_id="r1", run_at=COMPLETE)
    vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin(fd="2026-09-30")), run_id="r2",
                       run_at=NEXT_DAY)
    assert {r["status"] for r in _rows("vendor_financials")} == {"present"}
    assert len(_rows("vendor_financials")) == 6


def test_an_intraday_run_may_append_a_statement_change():
    res = vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()), run_id="r1",
                             run_at=INTRADAY)
    assert res["statements_appended"] == 3
    assert {r["valid_from"] for r in _rows("vendor_financials")} == {date(2026, 10, 8)}


# ── the writer: valuation (session snapshot, rule B) ────────────────────────

def test_valuation_first_write_per_session_wins():
    one = vendor_data.valuation_row_yfinance("AAA", _INFO)
    res = vendor_data.record(valuations=[one], run_id="r1", run_at=COMPLETE)
    assert res["valuations_written"] == 1
    two = vendor_data.valuation_row_yfinance("AAA", {**_INFO, "trailingPE": 30.0})
    res = vendor_data.record(valuations=[two], run_id="r2", run_at=LATER_SAME)
    assert res["valuations_written"] == 0 and res["valuations_kept"] == 1
    rows = _rows("vendor_valuation")
    assert [(r["session_date"], r["trailing_pe"], r["run_id"]) for r in rows] == [(date(2026, 10, 8), 22.5, "r1")]
    stored = rawstore.get("vendor_valuation", ("AAA", "yfinance", date(2026, 10, 8)))
    assert stored["payload"] == _INFO                               # zlib-compressed JSON, read back by get()


def test_valuation_next_session_is_a_new_row():
    vendor_data.record(valuations=[vendor_data.valuation_row_yfinance("AAA", _INFO)], run_id="r1", run_at=COMPLETE)
    res = vendor_data.record(valuations=[vendor_data.valuation_row_yfinance("AAA", _INFO)], run_id="r2",
                             run_at=NEXT_DAY)
    assert res["valuations_written"] == 1
    assert [r["session_date"] for r in _rows("vendor_valuation")] == [date(2026, 10, 8), date(2026, 10, 9)]


def test_an_intraday_run_writes_no_valuation():
    res = vendor_data.record(valuations=[vendor_data.valuation_row_yfinance("AAA", _INFO)], run_id="r1",
                             run_at=INTRADAY)
    assert res["valuations_written"] == 0 and res["valuations_skipped"] == 1
    assert res["valuation_skip_reason"] == "intraday"
    assert _rows("vendor_valuation") == []


def test_a_run_outside_the_calendar_writes_no_valuation_but_keeps_statements():
    when = datetime(2029, 1, 2, 23, 0, tzinfo=UTC).timestamp()
    res = vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()),
                             valuations=[vendor_data.valuation_row_yfinance("AAA", _INFO)], run_id="r1", run_at=when)
    assert res["valuations_written"] == 0 and res["valuation_skip_reason"] == "no session"
    assert res["statements_appended"] == 3
    assert {r["valid_from"] for r in _rows("vendor_financials")} == {None}


def test_record_with_nothing_writes_nothing():
    assert vendor_data.record(run_id="r1", run_at=COMPLETE)["statements_appended"] == 0
    assert _rows("vendor_financials") == [] and _rows("vendor_valuation") == []


def test_a_key_twice_in_one_body_keeps_the_first_answer_on_every_run():
    # Two rows with one key (here: a missing report_type on both, so 'unknown') and different values. The
    # first answer wins inside a batch, and a later run with the same body appends nothing (review F1).
    fin = _uw_fin(report_type=None)
    second = dict(fin["income_statements"][0], total_revenue="999")
    fin["income_statements"].append(second)
    rows = vendor_data.statement_rows_uw("BBB", fin)
    appended = [vendor_data.record(statements=rows, run_id=f"r{i}", run_at=t)["statements_appended"]
                for i, t in enumerate((COMPLETE, LATER_SAME, NEXT_DAY, NEXT_DAY + 3600))]
    assert appended == [3, 0, 0, 0]
    inc = rawstore.query("SELECT revenue FROM vendor_financials WHERE statement = 'income'")
    assert inc == [{"revenue": 1200.0}]


# ── the capture a job fills from its workers ────────────────────────────────

def test_capture_collects_from_threads_and_flushes_once():
    cap = vendor_data.Capture(run_at=COMPLETE)

    def work(i):
        t = f"T{i}"
        cap.add_yfinance(t, _yf_payload(info=_INFO))
        cap.add_uw(t, _uw_fin())

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    res = cap.flush(run_id="job1")
    assert res["statements_appended"] == 8 * 6 and res["valuations_written"] == 8
    assert {r["run_id"] for r in _rows("vendor_financials")} == {"job1"}
    assert cap.flush(run_id="job1") == vendor_data.EMPTY_RESULT     # flushed items are gone, nothing re-sent


def test_capture_takes_info_only_payloads_as_valuation_only():
    cap = vendor_data.Capture(run_at=COMPLETE)
    cap.add_yfinance("AAA", {"ticker": "AAA", "info": _INFO, "annual": [], "info_only": True})
    cap.add_valuation("PEER", _INFO)
    res = cap.flush(run_id="j")
    assert res["statements_appended"] == 0 and res["valuations_written"] == 2


def test_capture_ignores_a_stale_or_empty_payload():
    cap = vendor_data.Capture(run_at=COMPLETE)
    cap.add_yfinance("AAA", {**_yf_payload(info=_INFO), "_stale": True})
    cap.add_yfinance("BBB", {})
    cap.add_yfinance("CCC", None)
    cap.add_uw("DDD", None)
    assert cap.flush(run_id="j") == vendor_data.EMPTY_RESULT


def test_flush_failure_is_a_job_note_not_an_exception(monkeypatch):
    from RevenueChaser.core import ledger
    ledger.take_notes()
    cap = vendor_data.Capture(run_at=COMPLETE)
    cap.add_uw("BBB", _uw_fin())

    def boom(*a, **k):
        raise rawstore.RawStoreError("disk full")

    monkeypatch.setattr(vendor_data, "record", boom)
    res = cap.flush(run_id="j", label="Earnings")
    assert res["error"] == "RawStoreError: disk full"
    notes = ledger.take_notes()
    assert any("Earnings vendor statements were not stored" in n and "disk full" in n for n in notes)


def test_every_row_of_a_run_carries_the_capture_run_at_not_the_flush_time(monkeypatch):
    # AG flushes once per batch; every batch of one run keeps the run's time (review N2).
    monkeypatch.setattr(vendor_data, "_clock", lambda: COMPLETE)
    cap = vendor_data.Capture()
    cap.add_uw("BBB", _uw_fin())
    monkeypatch.setattr(vendor_data, "_clock", lambda: LATER_SAME)
    cap.flush(run_id="job1")
    cap.add_uw("CCC", _uw_fin())
    cap.flush(run_id="job1")
    assert {r["run_at"] for r in _rows("vendor_financials")} == {COMPLETE}


def test_a_store_that_keeps_failing_gives_one_job_note(monkeypatch):
    # AG flushes once per batch: a store that is down for the whole run notes it once (review N3).
    from RevenueChaser.core import ledger
    ledger.take_notes()
    cap = vendor_data.Capture(run_at=COMPLETE)

    def boom(*a, **k):
        raise rawstore.RawStoreError("disk full")

    monkeypatch.setattr(vendor_data, "record", boom)
    for t in ("BBB", "CCC", "DDD"):
        cap.add_uw(t, _uw_fin())
        assert cap.flush(run_id="j", label="Asymmetric Growth")["error"] == "RawStoreError: disk full"
    notes = [n for n in ledger.take_notes() if "vendor statements were not stored" in n]
    assert len(notes) == 1


# ── Data Health summary ─────────────────────────────────────────────────────

def test_health_summary_on_an_empty_store():
    h = vendor_data.health_summary(now=datetime.fromtimestamp(NEXT_DAY, tz=UTC))
    assert h["valuation_session"] is None and h["valuation_tickers"] == 0
    assert h["statement_rows"] == 0 and h["last_change_appended"] == 0 and h["last_change_session"] is None
    assert h["latest_complete"] == "2026-10-09"


def test_health_summary_counts_the_last_session_the_last_run_and_fill():
    vendor_data.record(statements=vendor_data.statement_rows_yfinance("AAA", _yf_payload()) +
                       vendor_data.statement_rows_yfinance("AAA", _yf_payload(revenue=None, period_end="2024-12-31")),
                       valuations=[vendor_data.valuation_row_yfinance("AAA", _INFO),
                                   vendor_data.valuation_row_yfinance("BBB", _INFO)], run_id="r1", run_at=COMPLETE)
    vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()) +
                       vendor_data.statement_rows_yfinance("AAA", _yf_payload(revenue=1.0)),
                       valuations=[vendor_data.valuation_row_yfinance("AAA", _INFO)], run_id="r2", run_at=NEXT_DAY)
    h = vendor_data.health_summary(now=datetime.fromtimestamp(NEXT_DAY, tz=UTC))
    assert h["valuation_session"] == "2026-10-09" and h["valuation_tickers"] == 1
    assert h["statement_rows"] == 10 and h["last_change_run_id"] == "r2" and h["last_change_appended"] == 4
    assert h["last_change_session"] == "2026-10-09"
    # fill % per field per source, on the newest period: yfinance revenue 1/1 (the 2024 row without one is older)
    assert h["fill"]["yfinance"]["revenue"] == 100.0 and h["fill"]["uw"]["revenue"] == 100.0
    assert h["fill"]["uw"]["rpo"] == 0.0
    assert h["newest_fetched_at"] is not None


@pytest.mark.parametrize("bad", [{"annual": "x"}, {"annual": [None, 3]}])
def test_malformed_yfinance_payloads_map_to_nothing(bad):
    assert vendor_data.statement_rows_yfinance("AAA", bad) == []


def test_health_summary_names_the_run_that_changed_something_not_a_quiet_one():
    # Most nights append nothing; the summary then still names the newest run that did, with its session,
    # instead of calling that older count "the last run" (review F2).
    vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()), run_id="r1", run_at=COMPLETE)
    quiet = vendor_data.record(statements=vendor_data.statement_rows_uw("BBB", _uw_fin()), run_id="r2",
                               run_at=NEXT_DAY)
    assert quiet["statements_appended"] == 0
    h = vendor_data.health_summary(now=datetime.fromtimestamp(NEXT_DAY, tz=UTC))
    assert (h["last_change_run_id"], h["last_change_appended"], h["last_change_session"]) == ("r1", 3, "2026-10-08")
