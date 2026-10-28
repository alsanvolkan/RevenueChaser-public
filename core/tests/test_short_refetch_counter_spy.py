"""The "short refetch refused" count is about the frame that can still be refused: SPY's (Swing review finding 8, PR 6).

Since C12 a scored name's bars come from D01 (``screen_common.load_bars``), which never sets
``short_refetch_refused``; only ``load_ohlc``, now the SPY fallback, does. The per-name count in ``data_quality`` was
therefore always 0, and a refused SPY frame was never counted. Now the count is the SPY baseline's flag (0 or 1),
on both boards that share the scan.
"""
from __future__ import annotations

import pytest

from RevenueChaser.core import screen_common as sc
from RevenueChaser.core.tests import screen_parity as scn
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import pipeline as sp

BOARDS = [pytest.param(sp, id="swing"), pytest.param(fp, id="fundamentals")]


@pytest.fixture
def scenario(rawstore_tmp_db, monkeypatch):
    scn.seed()
    scn.patch(monkeypatch)


@pytest.mark.parametrize("refused", [True, False])
@pytest.mark.parametrize("mod", BOARDS)
def test_a_refused_spy_frame_is_what_the_count_says(scenario, monkeypatch, mod, refused):
    real = sc.load_spy

    def spy(fallback, now=None):
        frame = real(fallback, now)
        frame.attrs["short_refetch_refused"] = refused
        return frame
    monkeypatch.setattr(sc, "load_spy", spy)
    payload = mod.run_scan()
    assert payload["candidates"]                                        # a real scan, not an empty board
    assert payload["data_quality"]["short_refetch_refused"] == int(refused)


@pytest.mark.parametrize("mod", BOARDS)
def test_a_scored_row_carries_no_per_name_refusal(mod):
    assert mod.build_payload([], run_stats={})["data_quality"]["short_refetch_refused"] == 0
