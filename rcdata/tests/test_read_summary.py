"""``rcdata.read.table_summary``: one row per serving table saying how much it holds and how new it is.

The Settings / Data screen asks for a need's state without naming any ticker. The readers a need points at all
want a key set (``bars(security_ids)``, ``estimates(tickers)``), so the envelope cannot answer that question from
them — and the API is not allowed to write SQL of its own. The one query belongs here, in the module that owns the
store's SQL.
"""
from __future__ import annotations

from datetime import date

import pytest

from RevenueChaser.rcdata import ops, read

from .test_prices_serving import AAPL, built as prices_built, env  # noqa: F401 - the built prices store
from .test_spine_security import built as spine_built, run_all  # noqa: F401 - the built spine


@pytest.mark.usefixtures("spine_built")
def test_a_built_table_reports_rows_keys_the_newest_day_and_the_status():
    s = read.table_summary("security", day_column="source_as_of", key_column="ticker")
    assert s["row_count"] == len(read.identity()) and s["row_count"] > 0
    assert s["key_count"] == len({r["ticker"] for r in read.identity()})
    assert s["newest_day"] == date(2026, 10, 27)
    assert s["status"] == ops.latest_check("spine/security")["result"]
    assert s["dataset"] == "spine/security"


@pytest.mark.usefixtures("prices_built")
def test_the_newest_day_is_the_tables_own_day_column():
    s = read.table_summary("price_bar_daily", day_column="session_date", key_column="security_id")
    assert s["newest_day"] == max(r["session_date"] for r in read.bars([AAPL]) + read.bars(["sec:none"]))
    assert s["key_count"] >= 2 and s["row_count"] >= s["key_count"]


@pytest.mark.usefixtures("prices_built")
def test_a_table_with_no_day_column_still_counts():
    s = read.table_summary("security_symbol", day_column=None, key_column="security_id")
    assert s["newest_day"] is None and s["row_count"] > 0


def test_an_unknown_table_is_refused_rather_than_queried():
    with pytest.raises(KeyError):
        read.table_summary("no_such_table")


@pytest.mark.usefixtures("spine_built")
def test_the_summary_is_counted_in_the_read_log(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(ops.read_log, "bump", seen.append)
    read.table_summary("security", day_column="source_as_of")
    assert seen == ["security"]


@pytest.mark.usefixtures("spine_built")
def test_a_sample_comes_back_with_the_summary_when_asked():
    s = read.table_summary("security", day_column="source_as_of", key_column="ticker", sample=3)
    assert len(s["sample_rows"]) == 3 and s["sample_rows"][0]["ticker"]
    assert all("source" in r for r in s["sample_rows"]), "the sample keeps the row's provenance"
    assert read.table_summary("security", day_column="source_as_of")["sample_rows"] == []


@pytest.mark.usefixtures("spine_built")
def test_the_sample_is_the_newest_rows_when_the_table_has_a_day():
    s = read.table_summary("security", day_column="source_as_of", sample=2)
    newest = read.table_summary("security", day_column="source_as_of")["newest_day"]
    assert all(r["source_as_of"] == newest for r in s["sample_rows"])
