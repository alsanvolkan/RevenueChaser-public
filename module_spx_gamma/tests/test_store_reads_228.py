"""#228: five follow-ups of the PR 3 delta review on SPX Gamma's store reads."""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.module_spx_gamma import store_reads
from RevenueChaser.rcdata import needs, ops, read

from . import rcdata_fixture as fx


def _boom(*a, **k):
    raise RuntimeError("ops.sqlite is locked")


def test_1_a_board_status_that_fails_to_read_leaves_the_readers_own_status(rcdata_market, monkeypatch):
    """Item 1: the try/except of ``_with_board_status`` (mutation MA survived without this test)."""
    own = store_reads.cot_tff(fx.LAST)
    monkeypatch.setattr(store_reads, "_board_status", _boom)
    out = store_reads.cot_tff(fx.LAST)
    assert out["stored"] is True and out["status"] == own["status"] and out["label"] == own["label"]


def test_2_a_read_failed_marker_gets_no_board_status(rcdata_market, monkeypatch):
    """Item 2: a reader that failed says so; the board's status is not stamped on its marker."""
    monkeypatch.setattr(needs, "ask", _boom)
    out = store_reads.cot_tff(fx.LAST)
    assert out["stored"] is False and "read failed" in out["label"] and out["status"] is None


@pytest.mark.parametrize("reports, day, gap", [
    ((date(2026, 10, 26),), date(2026, 10, 30), False),                       # 6 days after 20 Oct: a Monday
    ((date(2026, 10, 26), date(2026, 11, 3)), date(2026, 11, 6), False),      # 8 days after a Monday
    ((date(2026, 11, 3),), date(2026, 11, 6), True),                          # 14 days: a report missing
])
def test_3_reports_five_to_nine_days_apart_are_consecutive(rcdata_market, reports, day, gap):
    """Item 3: a holiday week dates a report on the Monday, so 6 or 8 days apart is still the previous week. The
    fixture's own reports end on Tue 20 Oct."""
    fx.serve(cot_reports=fx.COT_REPORTS + reports, series_end=day)
    cot = store_reads.cot_tff(day)["payload"]
    assert cot["report_date"] == reports[-1].isoformat() and cot["change_gap"] is gap
    assert (cot["asset_manager"]["change"] is None) is gap


def test_5_a_locked_status_read_keeps_the_stored_rows(rcdata_market, monkeypatch):
    """Item 5: a locked ``ops.sqlite`` (the read log's flush before the read, the status read after it) failed the
    whole read, so the board lost its pin flag and squeeze trigger. Now the rows come back stamped
    ``status_unknown``."""
    monkeypatch.setattr(ops.read_log, "flush", _boom)
    monkeypatch.setattr(ops.read_log, "_last", float("-inf"))        # every read is due to flush
    monkeypatch.setattr(ops, "read_latest_check", _boom)
    rows = read.market_series(["VIX"])
    assert rows and {r["status"] for r in rows} == {read.STATUS_UNKNOWN}
    for reader in (store_reads.zero_one_dte_dollars, store_reads.cot_tff):
        out = reader(fx.LAST)
        assert out["stored"] is True and out["status"] == read.STATUS_UNKNOWN, out["label"]


@pytest.mark.parametrize("days, gap", [(4, True), (5, False), (9, False), (10, True), (13, True)])
def test_3_the_consecutive_window_is_five_to_nine_days(days, gap):
    """Review N3 (#356): the limits of ``CONSECUTIVE_DAYS``."""
    assert (days not in store_reads.CONSECUTIVE_DAYS) is gap


def test_5_a_failed_read_log_flush_keeps_its_counts_for_the_next_one():
    """Review S1 (#356): the counts feed the retirement count, so a failed flush puts them back."""
    log = ops.ReadLog(process="test")
    with pytest.MonkeyPatch.context() as mp:        # its own patch: undoing it leaves the store roots alone
        mp.setattr(ops, "_add_reads", _boom)       # the write either backend makes (ops.sqlite or Redis)
        log.bump("market_series")
        with pytest.raises(RuntimeError):
            log.flush()
        assert sum(log._counts.values()) == 1
    log.flush()                                     # #359 N4: the next flush writes the kept count once
    assert log._counts == {} and ops.reads_since(ops._today(), ["market_series"]) == {"market_series": 1}
