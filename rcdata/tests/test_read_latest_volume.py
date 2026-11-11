"""``read.latest_volume``: newest close and average daily volume per ticker, the screener prefilter's pair (#642 F9)."""
from __future__ import annotations

import math

import pytest

from RevenueChaser.rcdata import read

from .test_prices_serving import AAPL, FLG, built, env  # noqa: F401 - the built prices store of the week


def _split_bars(security_id):
    return sorted((r for r in read.bars([security_id], basis="split")
                   if r["close"] is not None and not math.isnan(r["close"])), key=lambda r: r["session_date"])


def test_nothing_built_yet_is_no_rows():
    assert read.latest_volume(["AAPL"]) == {}
    assert read.latest_volume([]) == {}


@pytest.mark.usefixtures("built")
def test_price_and_average_volume_are_on_todays_share_basis():
    """FLG's 1-for-3 reverse split falls inside the window: the volume before it is divided by 3, so the average is
    today's shares, and the price is the newest close."""
    out = read.latest_volume(["flg", "AAPL", "NOPE"])
    assert set(out) == {"FLG", "AAPL"}
    for ticker, sid in (("FLG", FLG), ("AAPL", AAPL)):
        bars = _split_bars(sid)
        vols = [r["volume"] for r in bars if r["volume"] is not None]
        row = out[ticker]
        assert row["security_id"] == sid
        assert row["session_date"] == bars[-1]["session_date"]
        assert row["price"] == pytest.approx(bars[-1]["close"])
        assert row["avg_volume"] == pytest.approx(sum(vols) / len(vols))
        assert row["volume_sessions"] == len(vols)


@pytest.mark.usefixtures("built")
def test_the_window_is_the_last_n_bars():
    bars = _split_bars(FLG)
    row = read.latest_volume(["FLG"], sessions=2)["FLG"]
    last = [r["volume"] for r in bars[-2:]]
    assert row["volume_sessions"] == 2 and row["avg_volume"] == pytest.approx(sum(last) / 2)
