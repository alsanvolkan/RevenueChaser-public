"""``core/universe.py`` behind ``RC_UNIVERSE_SOURCE`` (build plan PR 5): the default reads ``raw.duckdb`` as before;
``spine`` reads membership, names, classification and types through ``rcdata.read`` and only the market fields
(cap, price, quote) from the old ``equity_universe``, until PR 6 brings market data to the spine."""
from __future__ import annotations

import pytest

from RevenueChaser.core import rawstore, universe
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built, run_all  # noqa: F401 - the 10 names

CAPS = {"MSFT": 3.1e12, "ETN": 1.3e11, "HEI": 3.0e10, "HEI-A": 2.0e10, "NVR": 2.5e10, "TM": 2.4e11,
        "CNA": 1.2e10, "FDX": 6.5e10, "WFC": 2.3e11, "SPY": 6.0e11, "WFC-PC": 1.0e9}


@pytest.fixture
def old_store():
    """The old store's universe: market fields for the spine's names, and one name only the old store has."""
    rows = [{"ticker": t, "market_cap": c, "price": 10.0, "name": f"old {t}", "sector": "Old Sector",
             "industry": "Old Industry"} for t, c in CAPS.items()]
    rows.append({"ticker": "OLDONLY", "market_cap": 5e11, "price": 1.0, "name": "Only old", "sector": "Energy"})
    rawstore.write_rows("equity_universe", rows)
    rawstore.write_rows("security_master", [{"ticker": "SPY", "security_type": "etp"},
                                            {"ticker": "OLDONLY", "security_type": "common"}])


def test_the_default_is_the_old_store(monkeypatch, built, old_store):
    monkeypatch.delenv(universe.SOURCE_ENV, raising=False)
    assert universe.universe_source() == "store"
    names = universe.classification_universe(0)
    assert names[0] == "MSFT" and "OLDONLY" in names and "SPY" not in names
    assert universe.spine_gics(["MSFT"])["MSFT"].sector == "Unclassified"     # "Old Sector" is no Morningstar name


@pytest.fixture
def spine(monkeypatch, built, old_store):
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    assert universe.universe_source() == "spine"


@pytest.mark.usefixtures("spine")
def test_the_spine_universe_is_its_listed_names_less_the_excluded_types_by_cap():
    names = universe.classification_universe(0)
    assert names[:3] == ["MSFT", "TM", "WFC"] and "OLDONLY" not in names
    assert not {"SPY", "WFC-PC"} & set(names)                               # etp and preferred, as before
    assert "VYLR" in names and names[-1] == "VYLR"                          # no cap in the old store: last
    assert universe.classification_universe() == [t for t in names if CAPS.get(t, 0) >= 3e8]


@pytest.mark.usefixtures("spine")
def test_rows_names_and_classification_come_from_the_spine():
    rows = {r["ticker"]: r for r in universe.classification_universe_rows(0)}
    assert rows["MSFT"] == {"ticker": "MSFT", "market_cap": 3.1e12, "sector": "Technology",
                            "industry": "Software & Services"}
    gics = universe.spine_gics(["TM", "nvr"])
    assert (gics["TM"].sector, gics["NVR"].industry) == ("Consumer Cyclical", "Consumer Durables & Apparel")
    assert universe.excluded_tickers() == {"SPY", "WFC-PC"}
    cached = {r["ticker"]: r for r in universe.load_cached_universe()}
    assert cached["MSFT"]["name"] == "MICROSOFT CORP" and cached["MSFT"]["price"] == 10.0


@pytest.mark.usefixtures("spine")
def test_the_funnel_counts_add_up_on_the_spine():
    c = universe.universe_counts(3e8)
    assert c["scanned_total"] == 12                      # the 14 SEC rows less the OTC and the no-exchange name
    assert c["scanned_total"] == c["below_floor"] + c["no_market_cap"] + c["floor_total"]
    assert c["floor_total"] == c["funds_etfs"] + c["spacs"] + c["other_securities"] + c["classification_total"]
    assert (c["funds_etfs"], c["other_securities"], c["no_market_cap"]) == (1, 1, 1)
    assert c["classification_total"] == len(universe.classification_universe(3e8))


@pytest.mark.usefixtures("spine")
def test_filter_fields_take_the_index_badges_from_the_spine_membership():
    fields = universe.filter_fields(["MSFT", "HEI", "NOPE"])
    assert fields["MSFT"]["index"] == ["S&P 500", "Nasdaq Composite"]
    assert fields["MSFT"]["sector"] == "Technology" and fields["MSFT"]["market_cap"] == 3.1e12
    assert fields["HEI"]["index"] == []                   # the MidCap 400 has no filter label yet (filter unchanged)
    assert fields["NOPE"]["sector"] == "Unclassified"


def test_no_spine_file_yet_is_an_empty_universe_not_an_error(monkeypatch, old_store):
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    assert universe.classification_universe(0) == [] and universe.universe_counts()["scanned_total"] == 0


def test_an_unknown_value_is_the_old_store(monkeypatch):
    monkeypatch.setenv(universe.SOURCE_ENV, "Spine ")
    assert universe.universe_source() == "spine"
    monkeypatch.setenv(universe.SOURCE_ENV, "wrds")
    assert universe.universe_source() == "store"
