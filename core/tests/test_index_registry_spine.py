"""``core/index_registry.py`` reads the identity spine (build plan PR 6): the badges, the filter's index list and
``load_index`` come from ``rcdata.read`` while the spine is the universe source; ``RC_UNIVERSE_SOURCE`` set to
anything else reads the old store's ``index_constituents``, which ``core/index_lists.py`` writes."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from RevenueChaser.core import index_lists as il
from RevenueChaser.core import index_registry as ir
from RevenueChaser.core import universe
from RevenueChaser.rcdata import read
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names


@pytest.fixture
def old_lists(rawstore_tmp_db):
    """The old store's lists, deliberately unlike the spine's."""
    il.save_index("sp500", {"OLDCO", "MSFT"})
    il.save_index("dow", {"OLDCO"})
    il.save_index("nasdaq", {"OLDCO"})


@pytest.fixture
def spine(monkeypatch, built, old_lists):
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    assert universe.universe_source() == "spine"


@pytest.mark.usefixtures("spine")
def test_load_index_is_the_spines_current_membership():
    sp500 = ir.load_index("sp500")
    assert sp500 == {r["ticker"] for r in read.membership("sp500")} and "MSFT" in sp500
    assert "OLDCO" not in sp500
    assert ir.load_index("sp400") >= {"HEI"}                           # read, though it has no badge label
    assert ir.load_index("ndx") == {"MSFT"}


@pytest.mark.usefixtures("spine")
def test_nasdaq_is_the_spines_listed_names_on_nasdaq_and_dow_has_no_source():
    assert ir.load_index("nasdaq") == {r["ticker"] for r in read.universe(tiers=("A", "B"))
                                       if r["exchange"] == "Nasdaq"}
    assert "MSFT" in ir.load_index("nasdaq") and "HEI" not in ir.load_index("nasdaq")
    assert ir.load_index("dow") is None                                # the old store's Dow list is not read


@pytest.mark.usefixtures("spine")
def test_badges_come_from_the_spine():
    reg = ir.load_registry()
    assert reg["MSFT"]["index"] == ["S&P 500", "Nasdaq Composite"]
    assert reg["MSFT"]["name"] == "MICROSOFT CORP"
    assert "OLDCO" not in reg and "HEI" not in reg                     # HEI: the MidCap 400 has no label
    cards = ir.annotate_cards([{"ticker": "msft"}, {"ticker": "OLDCO"}])
    assert [c["index"] for c in cards] == [["S&P 500", "Nasdaq Composite"], []]


@pytest.mark.usefixtures("spine")
def test_the_filter_offers_only_the_indexes_the_spine_carries():
    assert ir.filter_labels() == ["S&P 500", "Nasdaq Composite"]       # no Dow Jones, no SmallCap 600 members


@pytest.mark.usefixtures("spine")
def test_the_weekly_step_still_reads_its_own_store():
    """The old lists keep their own reader: the first-fill gate and the cache check must not see the spine."""
    assert il.stored_index("sp500") == {"OLDCO", "MSFT"}
    assert il.stored_slugs() == {"sp500", "dow", "nasdaq"}


def test_the_old_value_reads_the_old_store(monkeypatch, built, old_lists):
    monkeypatch.setenv(universe.SOURCE_ENV, "store")
    assert universe.universe_source() == "store"
    assert ir.load_index("sp500") == {"OLDCO", "MSFT"}
    assert ir.load_index("dow") == {"OLDCO"}
    assert ir.load_registry()["OLDCO"]["index"] == ["S&P 500", "Dow Jones", "Nasdaq Composite"]
    assert ir.filter_labels() == ["S&P 500", "Dow Jones", "Nasdaq Composite"]


def test_the_registry_does_not_import_the_old_store():
    tree = ast.parse(Path(ir.__file__).read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert "rawstore" not in imported and not hasattr(ir, "rawstore")
