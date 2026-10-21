"""Tests for the index membership registry (pure functions, no network)."""
import pytest

from RevenueChaser.core import index_registry as ir
from RevenueChaser.core import rawstore


def test_index_memberships_inversion():
    indexes = {
        "sp500": {"AAPL", "MSFT"},
        "dow": {"AAPL"},
        "nasdaq": {"MSFT"},
    }
    memberships = ir._index_memberships(indexes)
    assert memberships["AAPL"] == ["S&P 500", "Dow Jones"]
    assert memberships["MSFT"] == ["S&P 500", "Nasdaq Composite"]
    assert "XXX" not in memberships


def test_index_for_with_registry():
    reg = {"AAPL": {"index": ["S&P 500"]}}
    assert ir.index_for("aapl", reg) == ["S&P 500"]
    assert ir.index_for("MSFT", reg) == []


def test_annotate_cards_sets_index():
    cards = [{"ticker": "AAPL"}, {"ticker": "MSFT"}]
    reg = {"AAPL": {"index": ["S&P 500", "Dow Jones"]}}
    ir.annotate_cards(cards, reg)
    assert cards[0]["index"] == ["S&P 500", "Dow Jones"]
    assert cards[1]["index"] == []


def test_save_and_load_index(rawstore_tmp_db):
    ir.save_index("sp500", {"AAPL", "MSFT"})
    assert ir.load_index("sp500") == {"AAPL", "MSFT"}
    assert ir.load_index("dow") is None


def test_fetch_index_uses_cache(rawstore_tmp_db):
    ir.save_index("sp500", {"AAPL"})
    # cache-first: never calls the fetcher
    called = {"n": 0}

    def fake():
        called["n"] += 1
        return {"MSFT"}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ir, "_FETCHERS", {"sp500": fake})
    try:
        assert ir.fetch_index("sp500") == {"AAPL"}
        assert called["n"] == 0
    finally:
        monkeypatch.undo()


def test_build_registry_merges_index_and_meta(rawstore_tmp_db):
    rawstore.replace_all("equity_universe", [
        {"ticker": "AAPL", "name": "Apple", "sector": "Information Technology"},
        {"ticker": "MSFT", "name": "Microsoft", "sector": "Information Technology"},
    ])
    # seed the membership cache directly (the tiny sets are below the fetch floor)
    ir.save_index("sp500", {"AAPL", "MSFT"})
    ir.save_index("dow", {"AAPL"})
    ir.save_index("nasdaq", {"MSFT"})

    reg = ir.build_registry(refresh=False)
    assert reg["AAPL"]["index"] == ["S&P 500", "Dow Jones"]
    assert reg["MSFT"]["index"] == ["S&P 500", "Nasdaq Composite"]
    assert reg["AAPL"]["name"] == "Apple"
    assert reg["AAPL"]["sector"] == "Information Technology"
    # the index_tickers VIEW backs load_registry (name/sector from universe)
    loaded = ir.load_registry()
    assert loaded["AAPL"]["index"] == ["S&P 500", "Dow Jones"]
    assert loaded["AAPL"]["name"] == "Apple"
    assert loaded["AAPL"]["sector"] == "Information Technology"


def test_build_registry_drops_tickers_that_left_every_index(rawstore_tmp_db):
    """Regression: as a JSON file the registry was rewritten wholesale, but the
    table write was a per-row upsert — so a delisted/removed ticker kept its row
    and ``index_for`` badged it forever. Membership is now the source of truth."""
    ir.save_index("sp500", {"AAPL", "OLDCO"})
    mp = pytest.MonkeyPatch()
    mp.setattr(ir, "_FETCHERS", {
        "sp500": lambda: set(), "dow": lambda: set(), "nasdaq": lambda: set()})
    try:
        first = ir.build_registry(refresh=False)
        assert {"AAPL", "OLDCO"} <= set(first)

        # OLDCO is removed from the index on the next refresh
        ir.save_index("sp500", {"AAPL"})
        second = ir.build_registry(refresh=False)
    finally:
        mp.undo()

    assert "OLDCO" not in second
    assert ir.load_registry() is not None and "OLDCO" not in ir.load_registry()
    assert not rawstore.query("SELECT 1 FROM index_tickers WHERE ticker = 'OLDCO'")
    assert ir.index_for("OLDCO") == []


def test_save_index_replaces_the_slug_without_touching_others(rawstore_tmp_db):
    ir.save_index("sp500", {"AAPL", "MSFT"})
    ir.save_index("dow", {"AAPL"})
    ir.save_index("sp500", {"NVDA"})
    assert ir.load_index("sp500") == {"NVDA"}
    assert ir.load_index("dow") == {"AAPL"}
