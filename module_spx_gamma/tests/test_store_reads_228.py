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
