"""``yfinance/fx`` (#535): daily exchange rates for the fundamentals layer's non-USD filers, one Yahoo history
request per currency (``<CUR>USD=X``, the yfinance library faked at its ``Ticker``, never Yahoo); every raw day typed
into one Parquet file and staged as ``stg_yf_fx``."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from RevenueChaser.rcdata import ops
from RevenueChaser.rcdata.build.fundamentals import common, fx_rates

from . import fund_fixture as fx


def _history(days: dict[str, float]) -> pd.DataFrame:
    """A ``Ticker.history`` frame: a tz-aware day index and a ``Close`` column."""
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="Europe/London") for d in days])
    return pd.DataFrame({"Close": list(days.values())}, index=idx)


@pytest.fixture
def yf(monkeypatch):
    import yfinance
    asked: list[tuple[str, object]] = []
    answers: dict[str, object] = {}

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, start=None, period=None, auto_adjust=False, **_):
            asked.append((self.symbol, start or period))
            answer = answers.get(self.symbol, _history({"2026-11-04": 0.5, "2026-11-05": 0.51}))
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    monkeypatch.setattr(fx_rates, "CURRENCIES", ("ARS", "CAD"))
    fx.spine()
    return asked, answers


def _parquet() -> list[tuple]:
    import duckdb
    path = common.parquet_dir(fx_rates.NAME) / "part-0.parquet"
    return duckdb.sql(f"SELECT currency, day, usd_per_unit FROM read_parquet('{path}') ORDER BY 1, 2").fetchall()


def test_one_request_per_currency_whole_history_first_then_from_the_newest_day(yf):
    asked, answers = yf
    answers["ARSUSD=X"] = _history({"2001-07-13": 1.0, "2025-12-31": 0.000689})
    check = fx.run(fx_rates.NAME, "2026-11-06")
    assert check["result"] == "changed", check["note"]
    assert asked == [("ARSUSD=X", "max"), ("CADUSD=X", "max")]
    assert ops.calls_today("yfinance") == 2
    assert _parquet() == [("ARS", date(2001, 7, 13), 1.0), ("ARS", date(2025, 12, 31), 0.000689),
                          ("CAD", date(2026, 11, 4), 0.5), ("CAD", date(2026, 11, 5), 0.51)]
    assert "2 of 2 currencies" in check["note"]

    answers["CADUSD=X"] = _history({"2026-11-05": 0.52, "2026-11-06": 0.53})
    fx.run(fx_rates.NAME, "2026-11-07")
    # From a week before the newest stored day, so a late correction by Yahoo is read again.
    assert asked[2:] == [("ARSUSD=X", "2025-12-24"), ("CADUSD=X", "2026-10-29")]
    assert _parquet()[-3:] == [("CAD", date(2026, 11, 4), 0.5), ("CAD", date(2026, 11, 5), 0.52),  # newer answer
                               ("CAD", date(2026, 11, 6), 0.53)]


def test_a_failed_or_empty_currency_is_an_issue_and_every_failure_fails_the_run(yf):
    asked, answers = yf
    answers["ARSUSD=X"] = RuntimeError("429 Too Many Requests")
    answers["CADUSD=X"] = _history({})
    check = fx.run(fx_rates.NAME, "2026-11-06")
    assert check["result"] == "failed", check["note"]
    answers.clear()
    answers["ARSUSD=X"] = _history({})
    check = fx.run(fx_rates.NAME, "2026-11-07")
    assert check["result"] == "changed" and "1 empty" in check["note"]
    assert {c for c, _, _ in _parquet()} == {"CAD"}


def test_a_nan_close_is_left_out_never_zero(yf):
    asked, answers = yf
    answers["CADUSD=X"] = _history({"2026-11-04": float("nan"), "2026-11-05": 0.51})
    fx.run(fx_rates.NAME, "2026-11-06")
    assert [r for r in _parquet() if r[0] == "CAD"] == [("CAD", date(2026, 11, 5), 0.51)]


def test_a_second_run_on_the_same_day_keeps_the_whole_history(yf):
    # Review of PR 536: the second run asks only the last week and wrote over the same <as_of>/<CUR>.json.gz,
    # so the raw file and the Parquet lost every older day.
    asked, answers = yf
    answers["CADUSD=X"] = _history({"2001-01-02": 0.65, "2026-11-04": 0.5, "2026-11-05": 0.51})
    fx.run(fx_rates.NAME, "2026-11-06")
    answers["CADUSD=X"] = _history({"2026-11-05": 0.52})
    fx.run(fx_rates.NAME, "2026-11-06")
    assert asked[-1] == ("CADUSD=X", "2026-10-29")
    assert [r for r in _parquet() if r[0] == "CAD"] == [("CAD", date(2001, 1, 2), 0.65), ("CAD", date(2026, 11, 4), 0.5),
                                                        ("CAD", date(2026, 11, 5), 0.52)]
