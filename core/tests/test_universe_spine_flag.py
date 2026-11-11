"""``core/universe.py`` reads the identity spine only (#642 PR 6 removed ``RC_UNIVERSE_SOURCE`` and the old store
path): membership, names, classification and types through ``rcdata.read`` and, with no prices store here, the market
fields (cap, price, quote) from the old ``equity_universe`` (the prices store's: ``test_universe_market_prices``).

Every test here is the **membership rule** that applies where the spine holds no ``my_universe`` list: Tiers A and B
whole (PR 2, avo 30 Oct 2026). The test spine's ``spine/security`` build writes that list, so ``drop_my_universe``
drops it. The list path is ``core/tests/test_universe_my_universe.py``."""
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


def test_the_default_is_the_spine(built, old_store):
    drop_my_universe()
    assert universe.source_state() == {"requested": "spine", "effective": "spine", "reason": None, "variable": None}
    names = universe.classification_universe(0, operating_only=True)
    assert names[:3] == ["MSFT", "TM", "WFC"] and "OLDONLY" not in names
    assert universe.classification_universe(0)[:3] == ["MSFT", "SPY", "TM"]   # PR 2: every A+B type
    assert universe.universe_source() == "spine"


def drop_my_universe() -> None:
    """Make the built spine the one this module's rule is about: no ``my_universe`` table, so the tiers decide.

    A function and not a fixture on purpose: ``spine`` is imported by other modules
    (``test_bar_scope_spine``, ``test_universe_market_prices``), and a fixture it depended on would have to be
    imported into every one of them by name.
    """
    import duckdb

    from RevenueChaser.core import spine_reads
    from RevenueChaser.rcdata import serving
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute("DROP TABLE IF EXISTS my_universe")
    finally:
        con.close()
    spine_reads.clear()


@pytest.fixture
def spine(built, old_store):
    drop_my_universe()


@pytest.mark.usefixtures("spine")
def test_the_spine_universe_is_its_listed_names_by_cap_and_a_board_may_ask_for_operating_ones():
    names = universe.classification_universe(0, operating_only=True)
    assert names[:3] == ["MSFT", "TM", "WFC"] and "OLDONLY" not in names
    assert not {"SPY", "WFC-PC"} & set(names)                               # etp and preferred, as before
    assert "VYLR" in names and names[-1] == "VYLR"                          # no cap in the old store: last
    # No market-cap floor (avo, 29 Oct 2026): the default is the whole set, VYLR's missing cap included.
    assert universe.classification_universe(operating_only=True) == names
    assert (universe.classification_universe(3e8, operating_only=True)
            == [t for t in names if CAPS.get(t, 0) >= 3e8])
    # PR 2 (avo, 30 Oct 2026): the universe itself is every A+B type, the ETF and the preferred included.
    every = universe.classification_universe(0)
    assert {"SPY", "WFC-PC"} <= set(every) and set(every) - {"SPY", "WFC-PC"} == set(names)


@pytest.mark.usefixtures("spine")
def test_rows_names_and_classification_come_from_the_spine():
    rows = {r["ticker"]: r for r in universe.classification_universe_rows(0)}
    assert rows["MSFT"] == {"ticker": "MSFT", "market_cap": 3.1e12, "sector": "Technology",
                            "industry": "Software & Services", "security_type": rows["MSFT"]["security_type"]}
    assert rows["SPY"]["security_type"] == "etp"
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
    assert c["floor_total"] == c["funds_etfs"] + c["spacs"] + c["other_securities"] + c["operating_total"]
    assert (c["funds_etfs"], c["other_securities"], c["no_market_cap"]) == (1, 1, 1)
    assert c["classification_total"] == c["floor_total"] == len(universe.classification_universe(3e8))
    assert c["operating_total"] == len(universe.classification_universe(3e8, operating_only=True))


@pytest.mark.usefixtures("spine")
def test_filter_fields_take_the_index_badges_from_the_spine_membership():
    fields = universe.filter_fields(["MSFT", "HEI", "NOPE"])
    assert fields["MSFT"]["index"] == ["S&P 500", "Nasdaq Composite"]
    assert fields["MSFT"]["sector"] == "Technology" and fields["MSFT"]["market_cap"] == 3.1e12
    assert fields["HEI"]["index"] == []                   # the MidCap 400 has no filter label yet (filter unchanged)
    assert fields["NOPE"]["sector"] == "Unclassified"


def test_without_a_spine_no_old_name_is_read_and_the_reason_is_said(old_store):
    """#642 PR 6: the old ``equity_universe`` is never a fallback universe."""
    assert universe.source_state() == {"requested": "spine", "effective": "spine", "reason": "no spine.duckdb yet",
                                       "variable": None}
    assert universe.classification_universe(0) == [] and not universe.has_universe()


def test_a_failed_spine_build_is_said_and_the_last_good_spine_still_read(built, old_store):
    from RevenueChaser.rcdata import ops
    drop_my_universe()
    ops.record_check("spine/security", "failed", note="boom")
    assert universe.source_state()["reason"] == "spine/security's last check failed"
    names = universe.classification_universe(0)
    assert "MSFT" in names and "OLDONLY" not in names
    ops.record_check("spine/security", "skipped", note="paused")          # a skip after the failure changes nothing
    assert universe.source_state()["reason"] == "spine/security's last check failed"


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
    assert "VYLR" not in universe.classification_universe(0, operating_only=True)


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
