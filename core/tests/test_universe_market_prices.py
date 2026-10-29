"""The spine universe's market fields from the prices store (PR 7, G2): each listed name's market cap and price are
its newest in ``prices.duckdb`` (``rcdata.read.latest_market``), the old ``equity_universe`` by ticker where the
prices store has none; ``quote`` stays the old table's. An operating company's cap is its issuer's (every listed
class, as Yahoo's was); a fund's is its own, since one trust's CIK files many funds."""
from __future__ import annotations

from datetime import date

import os

import duckdb
import pytest

from RevenueChaser.core import rawstore, spine_reads, universe
from RevenueChaser.core.reporting_common import universe_provenance_html
from RevenueChaser.core.tests.test_universe_spine_flag import CAPS, old_store, spine  # noqa: F401 - the fixtures
from RevenueChaser.rcdata.tests.conftest import http_mock, market_roots  # noqa: F401 - market roots per test
from RevenueChaser.rcdata.tests.test_spine_security import built  # noqa: F401 - the 10 names

NEWEST = date(2026, 10, 27)
# ticker -> (security id, newest bar session, close, own cap, issuer cap); a cap of None is a bar with no shares.
PRICES = {
    "MSFT": ("012141:01", NEWEST, 500.0, 3.7e12, 3.7e12),
    "HEI": ("005567:01", NEWEST, 200.0, 1.5e10, 2.6e10),
    "HEI-A": ("005567:02", NEWEST, 160.0, 1.1e10, 2.6e10),
    "VYLR": ("040001:01", NEWEST, 3.0, 4.0e8, 4.0e8),            # no cap in the old store: the prices store gives one
    "SPY": ("sym:SPY", NEWEST, 585.0, 6.1e11, 9.0e12),          # the trust's CIK sums other funds: its own cap counts
    "FDX": ("011234:01", NEWEST, 300.0, None, None),            # a bar but no shares yet: price new, cap old
    "ETN": ("016478:01", date(2026, 9, 1), 999.0, 9.9e11, 9.9e11),   # far behind the store's newest session: old
}


def _write_prices(prices=PRICES) -> None:
    """A live ``prices.duckdb`` holding only what the universe reads: the symbols, the bars and the caps."""
    from RevenueChaser.rcdata import serving
    from RevenueChaser.rcdata.build.prices.serving import SCHEMAS
    path = serving.live_path("prices")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        for table in ("security_symbol", "price_bar_daily", "market_cap_daily"):
            con.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in SCHEMAS[table])})")
        for ticker, (sid, day, close, cap, issuer) in prices.items():
            con.execute("INSERT INTO security_symbol (security_id, symbol, valid_from, source) VALUES (?, ?, ?, ?)",
                        [sid, ticker, date(2025, 10, 1), "alpaca/bars"])
            for d in (date(2025, 10, 1), day):
                con.execute("INSERT INTO price_bar_daily (security_id, session_date, symbol, close, source) "
                            "VALUES (?, ?, ?, ?, ?)", [sid, d, ticker, close if d == day else 1.0, "alpaca/bars"])
                if cap is not None:
                    con.execute("INSERT INTO market_cap_daily (security_id, session_date, close, market_cap, "
                                "issuer_market_cap) VALUES (?, ?, ?, ?, ?)",
                                [sid, d, close, cap if d == day else 1.0, issuer if d == day else 1.0])
    finally:
        con.close()


@pytest.fixture
def prices(spine):
    _write_prices()


@pytest.mark.usefixtures("prices")
def test_the_caps_come_from_the_prices_store_and_the_old_table_where_it_has_none():
    rows = {r["ticker"]: r["market_cap"] for r in universe.classification_universe_rows(0)}
    assert rows["MSFT"] == 3.7e12                                  # the prices store's newest
    assert rows["HEI"] == rows["HEI-A"] == 2.6e10                   # the issuer's two listed classes
    assert rows["VYLR"] == 4.0e8 and "VYLR" in universe.classification_universe()     # now above the floor
    assert rows["TM"] == CAPS["TM"]                                 # not in the prices store: the old table's
    assert rows["FDX"] == CAPS["FDX"]                               # a bar with no shares: the old table's cap
    assert rows["ETN"] == CAPS["ETN"]                               # behind the store's newest session: the old one
    assert universe.classification_universe(0)[:3] == ["MSFT", "TM", "WFC"]


@pytest.mark.usefixtures("prices")
def test_the_price_is_the_newest_close_and_the_quote_stays_the_old_tables():
    rawstore.write_rows("equity_universe", [{"ticker": "MSFT", "market_cap": CAPS["MSFT"], "price": 10.0,
                                             "name": "old MSFT", "quote": {"fiftyTwoWeekHigh": 555.0}}])
    cached = {r["ticker"]: r for r in universe.load_cached_universe()}
    assert cached["MSFT"]["price"] == 500.0 and cached["MSFT"]["quote"] == {"fiftyTwoWeekHigh": 555.0}
    assert cached["FDX"]["price"] == 300.0                          # the bar's close, though its cap is the old one
    assert cached["ETN"]["price"] == 10.0                           # too far behind: the old price
    assert cached["SPY"]["market_cap"] == 6.1e11                    # a fund: its own cap, not its trust's sum


@pytest.mark.usefixtures("prices")
def test_the_funnel_counts_the_prices_stores_caps():
    c = universe.universe_counts(3e8)
    assert c["no_market_cap"] == 0                                  # VYLR, the one name with no cap, has one now
    assert c["scanned_total"] == c["below_floor"] + c["no_market_cap"] + c["floor_total"]
    assert c["classification_total"] == len(universe.classification_universe(3e8))


@pytest.mark.usefixtures("spine")
def test_without_a_prices_store_the_old_table_is_read_as_before():
    rows = {r["ticker"]: r["market_cap"] for r in universe.classification_universe_rows(0)}
    assert rows["MSFT"] == CAPS["MSFT"] and rows["VYLR"] is None


def test_a_cap_from_the_prices_store_alone_is_a_loaded_cap(monkeypatch, built):
    """The header's "market caps: none loaded yet" (PR 6, Opus review N5) counts the prices store's caps: on the host
    the old table stays empty, so after the first prices build the caps are there although that table has none."""
    monkeypatch.setenv(universe.SOURCE_ENV, "spine")
    assert not universe.has_market_caps()
    assert "market caps: none loaded yet" in universe_provenance_html("2026-10-28T00:00:00Z", "Swing Potential")
    _write_prices()
    assert universe.has_market_caps()
    assert "none loaded" not in universe_provenance_html("2026-10-28T00:00:00Z", "Swing Potential")


@pytest.mark.usefixtures("prices")
def test_a_render_reads_the_prices_store_once_per_file(monkeypatch):
    """As the spine's answers (``spine_reads``, Opus review S6): one prices read per render, kept until the file
    changes."""
    from RevenueChaser.rcdata import serving
    spine_reads.clear()
    seen = []
    real = serving.open_read
    monkeypatch.setattr(serving, "open_read", lambda kind: seen.append(kind) or real(kind))

    def render():
        universe.universe_counts(3e8)
        universe.classification_universe(3e8)
        return universe.classification_universe_rows(0)
    first = render()
    spine = seen.count("spine")
    assert seen.count("prices") == 1
    assert render() == first and seen.count("prices") == 1
    path = serving.live_path("prices")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))       # what a swap looks like to stat
    render()
    assert seen.count("prices") == 2 and seen.count("spine") == spine      # the spine's answers are still kept
    # The Opus review's N3: latest_market finds the tickers through the spine, so a new spine file reads it again.
    path = serving.live_path("spine")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    render()
    assert seen.count("prices") == 3
