"""G9 (build plan PR 17): the SEC 13F board reads the ownership store, not ``raw.duckdb``.

* the totals, the quarters and the names come from ``ownership.duckdb`` and the spine through ``store_reads``;
* a quarter still filing (its deadline not passed) is shown, marked "(filing)", and left out of the snapshot, the
  streak and Accel;
* the coverage line (#77) and the source and as-of line (#118) come from the store, on the Fetch and on a Refresh;
* the Short int column is FINRA's short interest with days to cover;
* the Fetch queues ``ownership/serve`` (once it has had its first run) and calls no provider;
* no ownership file yet is the board's empty state, never an error page.

Fixture serving files written straight into a temp ``MARKET_DIR``; offline.
"""
from __future__ import annotations

import gzip
import json

import pytest

from RevenueChaser.core.reporting_common import payload_path
from RevenueChaser.module_sec_13f.tests.ownership_store import _store, market  # noqa: F401 - the fixture


def _payload():
    from RevenueChaser.app import runners
    with gzip.open(payload_path(runners.SEC_13F_LATEST)) as fh:
        return json.load(fh)


def _cells(payload, ticker):
    for row in payload["rows"]:
        if row[0] == ticker:
            return dict(zip(payload["cols"], row[3])), row
    raise AssertionError(ticker)


def test_the_totals_come_from_the_ownership_store_with_their_complete_flag(market):
    from RevenueChaser.module_sec_13f import store_reads
    _store(market)
    totals = store_reads.totals()
    assert sorted(totals) == ["AAA", "BBB"]
    assert sorted(totals["AAA"]) == ["2025-03-31", "2025-06-30", "2025-09-30"]
    cell = totals["AAA"]["2025-06-30"]
    assert cell["shares"] == 200.0 and cell["complete"] is True and cell["prev_period"] == "2025-03-31"
    assert totals["AAA"]["2025-09-30"]["complete"] is False
    assert store_reads.meta(["AAA", "ZZZ"]) == {"AAA": {"name": "Alpha Inc", "sector": "Technology"}}


def test_no_ownership_file_is_no_rows_and_the_boards_empty_state(market):
    from RevenueChaser.app import runners, jobs
    from RevenueChaser.module_sec_13f import store_reads
    assert store_reads.totals() == {} and store_reads.periods() == []
    with pytest.raises(jobs.JobMessage, match="holds no 13F quarter yet"):
        runners.run_sec_13f_latest(None)
    html = runners.SEC_13F_LATEST.read_text(encoding="utf-8")
    assert "The ownership store holds no 13F quarter yet" in html and 'id="rc-filters"' in html
    assert not payload_path(runners.SEC_13F_LATEST).exists()


def test_a_quarter_still_filing_is_marked_and_kept_out_of_the_snapshot(market):
    from RevenueChaser.app import runners
    _store(market)
    runners.run_sec_13f_latest(None)
    html = runners.SEC_13F_LATEST.read_text(encoding="utf-8")
    assert "2025 Q3 (filing)" in html
    assert 'data-col="sh_2025-06-30" onclick="sortTable(this.dataset.col,this)" class="sort-desc "' in html
    cells, row = _cells(_payload(), "AAA")
    assert cells["value"][1] == 2000.0          # Q2's value, not the filing Q3's 500
    assert cells["streak"][1] == 1              # Q1 -> Q2 up; the smaller Q3 does not break it
    assert cells["sh_2025-09-30"][1] == 50.0    # still shown
    # Board order: the newest complete quarter's shares (AAA 200 > BBB 150).
    assert [r[0] for r in _payload()["rows"]] == ["AAA", "BBB"]
    assert row[1] == "Alpha Inc" and row[2] == "Technology"


def test_the_coverage_and_source_lines_come_from_the_store_on_a_refresh_too(market):
    from RevenueChaser.app import runners
    _store(market)
    runners.run_sec_13f_latest(None)            # the Refresh path: no notice handed in
    html = runners.SEC_13F_LATEST.read_text(encoding="utf-8")
    assert "13F 2025 Q2: 99.0% of the $5.00T reported value is named a ticker; 80 CUSIPs ($50.0B) are not" in html
    assert "ownership store (ownership/serve)" in html and "newest quarter 2025 Q3, still filing until" in html


def test_the_coverage_line_gives_the_share_outside_etfs_too():
    """avo, 29 Oct 2026: the bar is set on the value outside ETFs, which the store names none of; the line keeps the
    all-value share and adds that one. A file built before the column says nothing more."""
    from datetime import date

    from RevenueChaser.module_sec_13f import store_reads
    row = {"period": date(2026, 6, 30), "complete": True, "value_total": 75.69e12, "value_mapped": 65.47e12,
           "coverage": 0.8651, "unmapped_cusips": 16_248, "value_etf": 9.78e12, "coverage_ex_etf": 0.9935}
    note = store_reads.coverage_note([row])
    assert note.startswith("13F 2026 Q2: 86.5% of the $75.69T reported value is named a ticker; 16,248 CUSIPs")
    assert note.endswith("Outside the $9.78T held in ETFs, which have no ticker here: 99.4%.")
    del row["value_etf"], row["coverage_ex_etf"]
    assert "ETF" not in store_reads.coverage_note([row])


def test_the_short_column_is_finras_newest_short_interest_with_days_to_cover(market):
    from RevenueChaser.app import runners
    _store(market)
    runners.run_sec_13f_latest(None)
    cells, _ = _cells(_payload(), "AAA")
    assert cells["short"][0] == "2.50M (2.2d)" and cells["short"][1] == 2_500_000
    assert _cells(_payload(), "BBB")[0]["short"][1] is None
    assert "FINRA consolidated short interest" in runners.SEC_13F_LATEST.read_text(encoding="utf-8")


def test_exits_that_may_not_be_sales_are_named_on_the_new_exit_cell(market):
    from RevenueChaser.app import runners
    _store(market)
    runners.run_sec_13f_latest(None)
    cells, _ = _cells(_payload(), "AAA")
    assert cells["new_exit"][3] == ("2 exit(s) by filers that left holdings out in confidence; "
                                    "1 exit(s) by filers that filed no report this quarter")


def test_the_fetch_queues_the_derive_and_calls_no_provider(market, monkeypatch):
    from RevenueChaser.app import runners
    from RevenueChaser.module_sec_13f import engine
    from RevenueChaser.rcdata import ops

    def boom(*a, **k):
        raise AssertionError("the 13F Fetch must not reach the SEC or the old store")
    monkeypatch.setattr(engine, "download_missing_zips", boom)
    monkeypatch.setattr(engine, "ingest_zips", boom)
    _store(market)
    # Before ownership/serve's first run nothing is queued: a Fetch is never the store's first write.
    runners.build_sec13f(None)
    assert ops.queue_view()["active"] == []
    assert "ownership/serve not queued: it has not had its first run" in runners.SEC_13F_LATEST.read_text("utf-8")
    monkeypatch.setattr(ops, "ever_built", lambda name: name == "ownership/serve")
    runners.build_sec13f(None)
    runners.build_sec13f(None)
    active = ops.queue_view()["active"]
    assert [(r["dataset"], r["requested_by"]) for r in active] == [("ownership/serve", "sec_13f")]
    assert "Builder request: ownership/serve already queued." in runners.SEC_13F_LATEST.read_text("utf-8")


def test_the_chart_route_reads_the_store(market):
    from RevenueChaser.app.dashboard import app
    _store(market)
    with app.test_client() as c:
        got = c.get("/api/sec_13f/aaa").get_json()
        assert got["ok"] and got["ticker"] == "AAA"
        assert c.get("/api/sec_13f/ZZZ").status_code == 404


def test_the_history_records_the_newest_complete_quarter_and_finras_short(market):
    from RevenueChaser.module_sec_13f import history, store_reads
    _store(market)
    totals = store_reads.totals()
    quarter, rows = history.metric_rows(totals, {}, store_reads.short_interest(list(totals)), {})
    assert quarter == "2025-06-30"              # Q3 is still filing
    aaa = next(r for r in rows if r["ticker"] == "AAA")
    assert aaa["shares"] == 200.0 and aaa["short_interest"] == 2_500_000 and aaa["days_to_cover"] == 2.25
    assert "short_pct" not in aaa and history.SELECTION_VERSION == "6"
