"""``read.missing``: the one reason text a board shows when a builder file gives it no rows (#642 F3)."""
from __future__ import annotations

from RevenueChaser.rcdata import ops, read, serving


def _serve_market():
    with serving.build("market") as b:
        b.con.execute("CREATE TABLE market_series (series_id TEXT)")
        b.swap({"market_series": 0})


def test_no_file_names_the_file():
    assert read.missing("market") == "no market.duckdb yet"
    assert read.missing("market", "market_series") == "no market.duckdb yet"


def test_file_there_but_dataset_never_built():
    _serve_market()
    assert read.missing("market") is None
    assert read.missing("market", "market_series") == "market_series has not built yet"
    ops.record_check("market_series", "failed")
    assert read.missing("market", "market_series") == "market_series has not built yet"


def test_file_there_and_built():
    _serve_market()
    ops.record_check("market_series", "unchanged")
    assert read.missing("market", "market_series") is None


def test_does_not_create_ops_store():
    _serve_market()
    existed = ops.ops_path().exists()
    read.missing("market", "market_series")
    assert ops.ops_path().exists() == existed


def test_unreadable_ops_store_is_not_a_reason(monkeypatch):
    _serve_market()

    def boom(_dataset):
        raise RuntimeError("locked")
    monkeypatch.setattr(ops, "ever_built", boom)
    assert read.missing("market", "market_series") is None
