"""rcdata test isolation: every test gets its own market roots, so ops.sqlite and the serving files never leak.

The root conftest points ``RC_MARKET_DIR`` / ``RC_MARKET_RAW_DIR`` at one scratch folder per process; that is shared
by every test the process runs, so a queue or a check row one test left would be the next test's input. rcdata reads
``config.MARKET_DIR`` at call time, so moving the two attributes is enough.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def market_roots(tmp_path, monkeypatch):
    from RevenueChaser.core import config
    ssd, hdd = tmp_path / "market", tmp_path / "market_raw"
    monkeypatch.setattr(config, "MARKET_DIR", ssd)
    monkeypatch.setattr(config, "MARKET_RAW_DIR", hdd)
    monkeypatch.setattr(config, "MARKET_RAW_COPY_DIR", None)
    monkeypatch.setenv("RC_MARKET_DIR", str(ssd))
    monkeypatch.setenv("RC_MARKET_RAW_DIR", str(hdd))
    monkeypatch.setenv("RC_MARKET_RAW_COPY_DIR", "")
    return ssd, hdd
