"""K7 price + K8b chip (#383 R3; DF101) + AG S4 (#380): the data date is the board's own names' D01 date.

Before, both boards' chip was the newest bar of any ticker in ``swing_ohlc`` (``core/provenance.py`` ``_ohlc_day``),
a cache Co-movement's bulk Fetch also writes, so a board could show a date newer than any bar it scored. Since C12
both score every name from D01, so:

- the chip (``/api/data_asof``, ``provenance.data_as_of``) is the newest D01 session over the names the board last
  scored (its score table; Asymmetric Growth reads its momentum from D01 too since #380 S4, so it is the third);
- a Fetch's own payload ``data_as_of`` is the newest D01 session over the names that Fetch scored (the score table
  still holds the previous run's names while the payload is built);
- Co-movement reads D01 too since its own switch (#379), so its chip is the newest D01 session over its names.

Since PR 8 and PR 11 (G3) Co-movement and Asymmetric Growth read the prices store instead, so their chip is the newest
prices-store session over the same names (``provenance.prices_day``); Swing and Fundamentals stay on D01 until G4.
Offline: a temp store.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from RevenueChaser.core import provenance, rawstore
from RevenueChaser.core.tests import screen_parity as scn
from RevenueChaser.core.tests.d01_seed import seed_d01
from RevenueChaser.module_fundamentals import pipeline as fp
from RevenueChaser.module_swing_potential import pipeline as sp
from RevenueChaser.rcdata.tests import prices_store

D = date(2026, 10, 14)
EVENING = datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc).timestamp()
BOARDS = [pytest.param("module_swing_potential", "swing_scores", id="swing"),
          pytest.param("module_fundamentals", "fundamentals_scores", id="fundamentals"),
          pytest.param("module_asymmetric_growth", "ticker_scores", id="ag")]   # #380 S4; the prices store's since PR 11
PRICES_STORE_BOARDS = {"module_asymmetric_growth", "module_comovement"}


def _bars(module: str, ticker: str, days, fetched_at: float) -> None:
    """The board's own price bars: the prices store for the boards that read it (G3), else D01."""
    if module in PRICES_STORE_BOARDS:
        prices_store.put_bars([{"ticker": ticker, "session_date": d, "close": 1.5, "fetched_at": fetched_at}
                               for d in days])
    else:
        seed_d01(ticker, days, fetched_at)


def _days(end: date, n: int = 5) -> list[date]:
    return [d.date() for d in pd.bdate_range(end=end, periods=n)]


def _scored(table: str, *tickers: str) -> None:
    rawstore.replace_all(table, [{"ticker": t} for t in tickers])


@pytest.mark.parametrize("module,table", BOARDS)
def test_the_chip_is_the_newest_d01_bar_of_the_names_the_board_last_scored(rawstore_tmp_db, module, table):
    _bars(module, "AAA", _days(D), EVENING)
    _bars(module, "BBB", _days(date(2026, 10, 13)), EVENING)
    _scored(table, "BBB")
    assert provenance.data_as_of(module) == "2026-10-13"      # AAA's newer bar is not this board's
    _scored(table, "AAA", "BBB")
    assert provenance.data_as_of(module) == "2026-10-14"


@pytest.mark.parametrize("module,table", BOARDS)
def test_a_newer_bar_in_the_old_cache_is_not_the_chip(rawstore_tmp_db, module, table):
    _bars(module, "AAA", _days(date(2026, 10, 13)), EVENING)
    _scored(table, "AAA")
    rawstore.write_rows("swing_ohlc", [{"ticker": "AAA", "ts": "2026-10-15T00:00:00", "open": 1.0, "high": 1.0,
                                        "low": 1.0, "close": 1.0, "volume": 1.0, "cached_at": EVENING}])
    assert provenance.data_as_of(module) == "2026-10-13"


@pytest.mark.parametrize("module,table", BOARDS)
def test_no_scored_name_is_no_date(rawstore_tmp_db, module, table):
    seed_d01("AAA", _days(D), EVENING)
    assert provenance.data_as_of(module) is None


def test_co_movement_reads_the_prices_store_date_of_its_names(rawstore_tmp_db):
    """Co-movement clusters the prices store's closes (PR 8; D01 since #379 before), so its date is its names' newest
    session there, not swing_ohlc's nor D01's."""
    _bars("module_comovement", "AAA", _days(date(2026, 10, 9)), EVENING)
    _bars("module_comovement", "OUT", _days(D), EVENING)    # not in the universe: not this board's
    seed_d01("AAA", _days(D), EVENING)                      # the old D01: not read
    rawstore.write_rows("swing_ohlc", [{"ticker": "AAA", "ts": "2026-10-14T00:00:00", "open": 1.0, "high": 1.0,
                                        "low": 1.0, "close": 1.0, "volume": 1.0, "cached_at": EVENING}])
    rawstore.replace_all("equity_universe", [{"ticker": "AAA", "market_cap": 1.0e10, "sector": "Technology",
                                              "price": 10.0, "name": "AAA", "industry": "Software",
                                              "quote": {"symbol": "AAA"}}])
    assert provenance.data_as_of("module_comovement") == "2026-10-09"


def test_d01_day_is_the_newest_session_over_the_given_names(rawstore_tmp_db):
    seed_d01("AAA", _days(D), EVENING)
    seed_d01("BBB", _days(date(2026, 10, 9)), EVENING)
    assert provenance.d01_day(["bbb"]) == "2026-10-09"
    assert provenance.d01_day(["AAA", "BBB", "NONE"]) == "2026-10-14"
    assert provenance.d01_day([]) is None and provenance.d01_day(["NONE"]) is None


# ── a Fetch's own payload ─────────────────────────────────────────────────────

@pytest.fixture
def scenario(rawstore_tmp_db, monkeypatch):
    scn.seed()
    scn.patch(monkeypatch)


@pytest.mark.parametrize("mod", [sp, fp], ids=["swing", "fundamentals"])
def test_a_fetch_dates_its_payload_by_the_bars_it_scored(scenario, mod):
    assert mod.run_scan()["data_as_of"] == "2026-10-14"


@pytest.mark.parametrize("mod", [sp, fp], ids=["swing", "fundamentals"])
def test_a_fetch_on_bars_a_day_behind_says_so(scenario, mod):
    """Every scored name's D01 stops on 13 Oct (SPY is not a scored name): the payload says 13 Oct, whatever the
    score table held before this run."""
    _scored("swing_scores" if mod is sp else "fundamentals_scores", "OLDNAME")
    seed_d01("OLDNAME", _days(date(2026, 10, 15)), EVENING)
    for t, (kind, drift) in scn.BARS.items():
        if t != "SPY":
            with rawstore.connect(db=rawstore.MANIFEST["price_bars_daily"].get("db", "raw")) as conn:
                conn.execute("DELETE FROM price_bars_daily WHERE ticker = ?", [t])
            scn.seed_d01(t, scn._bars(kind, drift, scn.LONG_BARS, end=date(2026, 10, 13)), fetched_at=EVENING)
    payload = mod.run_scan()
    assert payload["candidates"] and payload["data_as_of"] == "2026-10-13"
