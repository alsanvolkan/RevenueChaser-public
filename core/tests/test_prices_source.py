"""The boards' daily bars from the builder's ``prices.duckdb`` (``core/prices.source_state``; #209, board plan PR D).

The builder serves ``price_bars_daily`` and ``price_actions`` as views under the old names with the old columns
(``rcdata/build/prices/serving.py``, the bridge D4), so the read API only has to open the other file.
"""
from __future__ import annotations

from datetime import date

import duckdb
import pytest

from RevenueChaser.core import ledger, prices, rawstore
from RevenueChaser.rcdata.tests.conftest import market_roots  # noqa: F401  (fixture)

STORE_CLOSE, LAYER_CLOSE = 10.0, 20.0
SESSION = date(2026, 11, 2)


def _store_bar():
    rawstore.write_rows("price_bars_daily", [{"ticker": "SPY", "session_date": SESSION, "open": STORE_CLOSE,
                                              "high": STORE_CLOSE, "low": STORE_CLOSE, "close": STORE_CLOSE,
                                              "volume": 1.0, "source": "uw", "fetched_at": 1.0}])


def _serving_file(built: bool = True):
    from RevenueChaser.rcdata import ops, serving
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE price_bars_daily (ticker VARCHAR, session_date DATE, open DOUBLE, high DOUBLE, "
                "low DOUBLE, close DOUBLE, volume DOUBLE, source VARCHAR, fetched_at DOUBLE)")
    con.execute("INSERT INTO price_bars_daily VALUES ('SPY', ?, ?, ?, ?, ?, 2.0, 'alpaca', 2e9)",
                [SESSION, LAYER_CLOSE, LAYER_CLOSE, LAYER_CLOSE, LAYER_CLOSE])
    con.execute("CREATE TABLE price_actions (ticker VARCHAR, ex_date DATE, action VARCHAR, value DOUBLE, "
                "session_date DATE, source VARCHAR, fetched_at DOUBLE, run_id VARCHAR, run_at DOUBLE)")
    con.close()
    if built:
        ops.record_check(prices.SERVING_DATASET, "changed")


@pytest.fixture
def asked(monkeypatch, market_roots):  # noqa: F811
    monkeypatch.delenv(prices.SOURCE_ENV, raising=False)
    prices._FALLBACK_LOGGED.clear()
    _store_bar()


def _close():
    return prices.daily_bars(["SPY"])["close"].tolist()


def test_the_boards_read_the_builders_bars(asked):
    _serving_file()
    assert prices.source_state()["effective"] == prices.LAYER
    assert _close() == [LAYER_CLOSE]
    assert ledger.d01_last_bars(["SPY"])["SPY"] == (SESSION.isoformat(), 2e9)


def test_without_a_served_file_the_old_store_is_read(asked):
    state = prices.source_state()
    assert state["effective"] == prices.STORE and state["reason"] == "no prices.duckdb yet"
    assert _close() == [STORE_CLOSE]


def test_a_file_never_built_is_not_read(asked):
    _serving_file(built=False)
    assert prices.source_state()["reason"] == f"{prices.SERVING_DATASET} has not built yet"
    assert _close() == [STORE_CLOSE]


def test_the_escape_hatch_reads_the_old_store(asked, monkeypatch):
    _serving_file()
    monkeypatch.setenv(prices.SOURCE_ENV, "store")
    assert prices.source_state()["effective"] == prices.STORE
    assert _close() == [STORE_CLOSE]
    assert ledger.d01_last_bars(["SPY"])["SPY"][1] == 1.0
