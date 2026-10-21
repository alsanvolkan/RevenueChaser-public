"""K8a (#383 R3 slice 1): ``daily_bars(start=)`` reads a window by SQL, not the whole series.

- The SQL reads each ticker's bars from ``start`` on plus its last bar before ``start`` (the previous close a
  dividend inside the window needs), and every action; the full series is no longer loaded.
- A windowed read equals the full read sliced, on both bases, for every start (splits and dividends on both sides).
- ``skipped_events`` counts the events that touch the window: an amount ex on or before ``start`` adjusts no bar
  in it, so it is left out.
Offline, temp store.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from RevenueChaser.core import prices, rawstore
from RevenueChaser.core.tests.test_prices import bar_row
from RevenueChaser.core.tests.test_prices_vector_383 import spy_bar_reads


def _ts(d: date) -> float:
    return datetime(d.year, d.month, d.day, 21, 35, tzinfo=UTC).timestamp()


def _act(t: str, d: date, action: str, value: float, fetched: date | None = None) -> dict:
    f = _ts(fetched or d)
    return {"ticker": t, "ex_date": d, "action": action, "value": value, "session_date": d,
            "source": "yfinance", "fetched_at": f, "run_id": "r", "run_at": f}


def _days(n: int, first: date = date(2026, 1, 5)) -> list[date]:
    out, d = [], first
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def series(rawstore_tmp_db):
    """Two tickers, 60 sessions; a 2:1 split, dividends before, on and after the starts tested, a gap in TTT."""
    days = _days(60)
    early = _ts(days[0])
    bars = []
    for i, d in enumerate(days):
        close = 100.0 + i if i < 30 else (100.0 + i) / 2       # the split takes effect on days[30]
        bars.append(bar_row("TTT", d, close=close, fetched_at=early if i < 30 else _ts(d)))
        if i not in (40, 41):                                   # UUU misses two sessions
            bars.append(bar_row("UUU", d, close=20.0 + 0.1 * i, fetched_at=_ts(d)))
    rawstore.write_rows("price_bars_daily", bars)
    rawstore.write_rows("price_actions", [
        _act("TTT", days[30], "split", 2.0),
        _act("TTT", days[10], "dividend", 1.0),
        _act("TTT", days[25], "dividend", 0.5, fetched=days[20]),      # fetched before the split: divided by it
        _act("TTT", days[45], "dividend", 0.25),
        _act("UUU", days[20], "dividend", 0.2),
        _act("UUU", days[42], "dividend", 0.3),                        # ex right after UUU's gap
        _act("UUU", days[55], "capital_gain", 0.1)])
    return days


def _frame(df: pd.DataFrame) -> list[tuple]:
    return [(r.ticker, r.session_date.date(), round(r.open, 9), round(r.close, 9), round(r.volume, 6))
            for r in df.itertuples()]


@pytest.mark.parametrize("basis", ["nominal", "adjusted"])
def test_a_windowed_read_equals_the_full_read_sliced(series, basis):
    full = prices.daily_bars(["TTT", "UUU"], basis=basis)
    for i in (0, 1, 10, 11, 25, 29, 30, 31, 41, 42, 43, 45, 59):
        start = series[i]
        got = prices.daily_bars(["TTT", "UUU"], basis=basis, start=start)
        want = full[full["session_date"] >= pd.Timestamp(start)].reset_index(drop=True)
        assert _frame(got) == _frame(want), (basis, start)
        end = series[min(i + 7, 59)]
        got_end = prices.daily_bars(["TTT", "UUU"], basis=basis, start=start, end=end)
        assert _frame(got_end) == [r for r in _frame(want) if r[1] <= end], (basis, start, end)


def test_the_sql_reads_the_window_and_one_bar_before_it(series, monkeypatch):
    seen = spy_bar_reads(monkeypatch)       # the bars come back as columns since #383 K8a speed
    start = series[50]
    prices.daily_bars(["TTT"], basis="adjusted", start=start)
    dates = sorted(d for _, d in seen)
    assert dates == series[49:], "the read loads the window plus the one previous bar, not the full series"


def test_a_ticker_with_no_bar_before_start_reads_from_start(series, monkeypatch):
    seen = spy_bar_reads(monkeypatch)
    got = prices.daily_bars(["TTT"], basis="adjusted", start=series[0] - timedelta(days=30))
    assert len(got) == 60 and len(seen) == 60


def test_a_dividend_whose_previous_close_is_before_start_still_adjusts_the_window(series):
    """UUU's dividend ex days[42] sits right after its gap (no bar on 40, 41): C_prev is days[39]'s close.

    A window from days[39] holds the bar it adjusts; a window from days[40] holds none it adjusts, but the event is
    still found (not skipped) because the read carries the bar before ``start``.
    """
    full = prices.daily_bars(["UUU"], basis="adjusted")
    w39 = prices.daily_bars(["UUU"], basis="adjusted", start=series[39])
    assert w39.iloc[0]["close"] == pytest.approx(full[full["session_date"] == pd.Timestamp(series[39])].iloc[0]["close"])
    assert w39.iloc[0]["close"] != pytest.approx(20.0 + 0.1 * 39)          # the 0.3 and 0.1 amounts applied
    w40 = prices.daily_bars(["UUU"], basis="adjusted", start=series[40])
    assert w40.attrs["skipped_events"] == 0


def test_skipped_events_count_only_the_events_that_touch_the_window(rawstore_tmp_db):
    days = _days(6)
    rawstore.write_rows("price_bars_daily", [bar_row("T", d, close=10.0 + i, fetched_at=_ts(d))
                                             for i, d in enumerate(days)])
    # ex on the first session: no previous close, skipped in a full read; it adjusts no bar from days[2] on.
    rawstore.write_rows("price_actions", [_act("T", days[0], "dividend", 1.0)])
    assert prices.daily_bars(["T"], basis="adjusted").attrs["skipped_events"] == 1
    assert prices.daily_bars(["T"], basis="adjusted", start=days[2]).attrs["skipped_events"] == 0
