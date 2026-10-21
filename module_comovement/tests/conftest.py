"""Co-movement test fixtures."""
from __future__ import annotations

import pytest

from RevenueChaser.module_comovement import config


@pytest.fixture(autouse=True)
def _engine_epoch_floor(monkeypatch):
    """Pin ``ENGINE_EPOCH`` to the past (#95) so a run stamped at the real wall
    clock is always serveable here; these tests must not flap with the time of
    day a suite happens to run at.

    A test that checks the epoch cutoff itself re-patches ``ENGINE_EPOCH`` to a
    later value after this fixture has run; monkeypatch reverts both in order.
    """
    monkeypatch.setattr(config, "ENGINE_EPOCH", "2000-01-01T00:00:00+00:00")
