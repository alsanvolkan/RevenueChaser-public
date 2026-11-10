"""``base.spill_peak``: the per-dataset spill measure ``build_kind_many`` logs (#654 review, deployed with #650)."""
from __future__ import annotations

import time

from RevenueChaser.rcdata.build import base


def test_a_spill_freed_before_the_step_ends_still_counts_as_its_peak(tmp_path):
    with base.spill_peak(tmp_path, every=0.01) as peak:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "block.tmp").write_bytes(b"x" * 4096)
        for _ in range(500):                          # up to ~5 s for the sampler to see it
            if peak[0] >= 4096:
                break
            time.sleep(0.01)
        (tmp_path / "sub" / "block.tmp").unlink()
    assert peak[0] >= 4096


def test_what_is_left_at_the_end_is_counted_and_a_missing_folder_is_zero(tmp_path):
    with base.spill_peak(tmp_path / "spill", every=60) as peak:
        (tmp_path / "spill").mkdir()
        (tmp_path / "spill" / "a.tmp").write_bytes(b"x" * 100)
    assert peak[0] == 100
    with base.spill_peak(tmp_path / "never", every=60) as peak:
        pass
    assert peak[0] == 0
