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


def test_the_flag_without_a_spine_falls_back_to_the_store_and_says_so(monkeypatch, old_store, caplog):
    """Should-fix 7: setting the flag a step early must not empty every screener."""
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    with caplog.at_level("WARNING"):
        assert universe.universe_source() == "store"
    assert "OLDONLY" in universe.classification_universe(0)
    state = universe.source_state()
    assert state == {"requested": "spine", "effective": "store", "reason": "no spine.duckdb yet"}
    assert any("RC_UNIVERSE_SOURCE=spine" in r.getMessage() and "falls back" in r.getMessage() for r in caplog.records)


def test_the_flag_with_a_failed_spine_build_falls_back_to_the_store(monkeypatch, built, old_store):
    from RevenueChaser.rcdata import ops
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    assert universe.universe_source() == "spine"
    ops.record_check("spine/security", "failed", note="boom")
    assert universe.universe_source() == "store" and "OLDONLY" in universe.classification_universe(0)
    assert universe.source_state()["reason"] == "spine/security's last check failed"
    ops.record_check("spine/security", "skipped", note="paused")          # a skip after the failure changes nothing
    assert universe.universe_source() == "store"


def test_unset_says_the_store_was_asked_for(monkeypatch):
    monkeypatch.delenv(universe.SOURCE_ENV, raising=False)
    assert universe.source_state() == {"requested": "store", "effective": "store", "reason": None}


def test_an_unknown_value_is_the_old_store(monkeypatch):
    monkeypatch.setenv(universe.SOURCE_ENV, "Spine ")
    assert universe.source_state()["requested"] == "spine"
    monkeypatch.setenv(universe.SOURCE_ENV, "wrds")
    assert universe.source_state() == {"requested": "store", "effective": "store", "reason": None}


@pytest.mark.usefixtures("spine")
def test_an_untyped_spine_name_takes_the_old_masters_type(monkeypatch):
    """Should-fix 6: a spine name with no type (no OpenFIGI answer, no Compustat link) falls back to the identity
    master's type, so an ETF the old path excluded is not let through by the flag."""
    rawstore.write_rows("security_master", [{"ticker": "VYLR", "security_type": "etp"}])
    from RevenueChaser.rcdata import read
    real = read.identity
    monkeypatch.setattr(read, "identity", lambda tickers=None: [
        {**r, "security_type": "unknown"} if r["ticker"] == "VYLR" else r for r in real(tickers)])
    assert "VYLR" in universe.excluded_tickers()
    assert "VYLR" not in universe.classification_universe(0)


@pytest.mark.usefixtures("spine")
def test_the_masters_specific_type_wins_over_a_tpci_guess(monkeypatch):
    """Delta S4: tpci (fix 6) types NVR "common"; the identity master knows better (here: a closed-end fund), so the
    master's type that is not "common" wins over a type whose only source is tpci. OpenFIGI's type still wins."""
    from RevenueChaser.rcdata import read
    real = read.identity
    monkeypatch.setattr(read, "identity", lambda tickers=None: [
        {**r, "security_type": "common", "type_source": "compustat_tpci(0)"} if r["ticker"] in ("NVR", "FDX")
        else r for r in real(tickers)])
    rawstore.write_rows("security_master", [{"ticker": "NVR", "security_type": "cef"},
                                            {"ticker": "FDX", "security_type": "common"},
                                            {"ticker": "MSFT", "security_type": "etp"}])
    excluded = universe.excluded_tickers()
    assert "NVR" in excluded                               # the master's cef over the tpci guess
    assert "FDX" not in excluded                           # the master's common changes nothing
    assert "MSFT" not in excluded                          # OpenFIGI's type is not overruled by the master
