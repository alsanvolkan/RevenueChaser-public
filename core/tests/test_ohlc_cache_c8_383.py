"""C8 (#383 R3; card D option 2): one writer of ``swing_ohlc``, in core, with the F2b new-listing rule.

Before, two writers wrote the cache: Swing's ``indicators._write_ohlc`` (the per-ticker refetch, now only the SPY
fallback) and Co-movement's bulk ``fetch_bulk``. Only Swing's path had #261's guard (a short refetch never replaces
a longer stored history), and the guard took any refetch that shares no session with the stored bars for another
listing under a reused symbol. The F2b review found the gap: one bar right after the stored last bar shares no
session either, so a 500-bar history was replaced by 1 bar (card F2b, avo "30 gün şartı", 2026-10-15 20:13
Amsterdam). Now ``module_swing_potential/ohlc_cache.py`` is the one writer (Co-movement's row format, moved), both
paths write through
it, and a short series that shares no session is a new listing only when it starts more than 30 days after the
stored last bar. No visible change otherwise: same rows, same ``ts`` format, same replace-per-ticker write.
Offline: a temp store.
"""
from __future__ import annotations

import re
import time
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from RevenueChaser.core import rawstore
from RevenueChaser.module_comovement import ohlc as comov_ohlc
from RevenueChaser.module_swing_potential import indicators, ohlc_cache

ROOT = Path(__file__).resolve().parents[2]


def _frame(start: str, periods: int) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=periods)
    c = pd.Series(np.arange(periods, dtype=float) + 100.0, index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": 1.0e6})


def _store(ticker: str, frame: pd.DataFrame, cached_at: float = 1.0) -> None:
    rawstore.write_rows("swing_ohlc", ohlc_cache.rows_for(ticker, frame, cached_at))


def _stored_days(ticker: str) -> list[str]:
    return [r["d"] for r in rawstore.query(
        "SELECT substr(ts, 1, 10) AS d FROM swing_ohlc WHERE ticker = ? ORDER BY ts", [ticker])]


STORED = _frame("2024-01-01", 500)                      # ends 2025-11-28 (a Friday)
LAST = STORED.index[-1]


# ── one writer ────────────────────────────────────────────────────────────────

def test_no_other_production_code_writes_swing_ohlc():
    pattern = re.compile(r"""(write_rows|replace_all|append_rows)\(\s*["']swing_ohlc["']|INTO\s+swing_ohlc""")
    writers = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or rel.startswith(("scripts/", "module_testing/", ".")) \
                or rel == "module_swing_potential/ohlc_cache.py":
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            writers.append(rel)
    assert writers == []


def test_rows_keep_co_movements_format():
    idx = pd.DatetimeIndex(["2026-09-14", "2026-09-15"]).tz_localize("America/New_York")
    df = pd.DataFrame({"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0], "Close": [np.nan, 2.0],
                       "Volume": [10.0, 20.0]}, index=idx)
    rows = ohlc_cache.rows_for("AAA", df, 5.0)
    assert rows == [{"ticker": "AAA", "ts": "2026-09-15T00:00:00", "open": 2.0, "high": 2.0, "low": 2.0,
                     "close": 2.0, "volume": 20.0, "cached_at": 5.0}]      # a NaN close is no bar


# ── the rule (#261 + F2b) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("fresh,written", [
    pytest.param(_frame("2025-12-01", 1), False, id="one-bar-the-next-session"),          # the F2b gap
    pytest.param(_frame("2025-12-24", 30), False, id="starts-26-days-after"),
    pytest.param(_frame("2025-12-29", 30), True, id="starts-31-days-after-a-new-listing"),
    pytest.param(_frame(str(LAST.date()), 30), False, id="one-shared-session-261"),
    pytest.param(_frame("2023-06-01", 30), False, id="no-shared-session-but-starts-before"),
    pytest.param(_frame("2025-12-01", 260), True, id="252-or-more-bars"),
    pytest.param(_frame("2027-01-04", 500), True, id="as-long-as-the-stored-series"),
])
def test_a_short_series_replaces_the_stored_one_only_as_a_new_listing(rawstore_tmp_db, fresh, written):
    _store("AAA", STORED)
    outcome = ohlc_cache.write(ohlc_cache.rows_for("AAA", fresh, 2.0))
    assert outcome == {"AAA": "written" if written else "refused"}
    days = _stored_days("AAA")
    assert len(days) == (len(fresh) if written else len(STORED))


def test_the_gap_is_counted_in_calendar_days_from_the_stored_last_bar():
    stored = ["2025-11-13", "2025-11-14"]
    assert ohlc_cache.short_series_refused(stored, ["2025-12-14"]) is True      # 30 days: not more than 30
    assert ohlc_cache.short_series_refused(stored, ["2025-12-15"]) is False     # 31 days: a new listing


@pytest.mark.parametrize("bars", [100, 101], ids=["the-same-sessions-again", "one-session-more"])
def test_a_young_listings_refetch_is_written(rawstore_tmp_db, bars):
    """A listing with fewer than 252 stored bars still grows: a refetch with at least as many bars replaces it."""
    _store("YNG", _frame("2026-01-05", 100))
    assert ohlc_cache.write(ohlc_cache.rows_for("YNG", _frame("2026-01-05", bars), 2.0)) == {"YNG": "written"}
    assert len(_stored_days("YNG")) == bars


def test_nothing_stored_is_always_written(rawstore_tmp_db):
    assert ohlc_cache.write(ohlc_cache.rows_for("NEW", _frame("2026-01-05", 3), 2.0)) == {"NEW": "written"}
    assert len(_stored_days("NEW")) == 3


def test_each_ticker_of_a_batch_is_decided_on_its_own(rawstore_tmp_db):
    _store("AAA", STORED)
    one = _frame("2025-12-01", 1)
    rows = ohlc_cache.rows_for("AAA", one, 2.0) + ohlc_cache.rows_for("BBB", one, 2.0)
    assert ohlc_cache.write(rows) == {"AAA": "refused", "BBB": "written"}
    assert len(_stored_days("AAA")) == 500 and len(_stored_days("BBB")) == 1


# ── both paths write through it ───────────────────────────────────────────────

def test_swing_serves_the_stored_history_when_one_bar_arrives_the_next_session(rawstore_tmp_db, monkeypatch):
    _store("SPY", STORED, cached_at=time.time() - 3 * 86400)

    class _T:
        def __init__(self, t):
            pass

        def history(self, *a, **k):
            return _frame("2025-12-01", 1)
    monkeypatch.setattr("yfinance.Ticker", _T)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    df = indicators.load_ohlc("SPY")
    assert len(df) == 500 and df.attrs.get("stale") is True and df.attrs.get("short_refetch_refused") is True
    assert len(_stored_days("SPY")) == 500


def test_co_movements_fetch_no_longer_writes_the_cache(rawstore_tmp_db, monkeypatch):
    """#379 D01 switch: Co-movement reads D01, so its Fetch downloads nothing and never calls the one writer; its
    result keeps the keys it had before C8 (card D)."""
    _store("AAA", STORED, cached_at=time.time() - 30 * 86400)

    def refuse(*a, **k):
        raise AssertionError("Co-movement must not write swing_ohlc")

    monkeypatch.setattr(ohlc_cache, "write", refuse)
    out = comov_ohlc.fetch_bulk(["AAA", "BBB", "CCC"])
    assert (out["fetched"], out["failed"]) == (0, 0)
    assert set(out) == {"requested", "cached", "pending", "fetched", "failed", "no_fetch"}
    assert len(_stored_days("AAA")) == 500
    assert not hasattr(comov_ohlc, "rows_from_download") and not hasattr(comov_ohlc, "_yf_download")
