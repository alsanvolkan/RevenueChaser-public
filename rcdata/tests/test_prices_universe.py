"""The prices store's universe (PR 382, avo 4 Nov 2026): what is stored, and the memory rule of the big tables.

Today's universe only: Tier A names of an operating type (ordinary shares, ADRs, REITs and the other operating
types), plus the spine's ETFs over ``ETF_MIN_AUM``; no SPAC, warrant, unit, preferred, closed-end fund or untyped
name, no ETN, no ETF of unknown AUM, and nothing delisted.
"""
from __future__ import annotations

import duckdb
import pytest

from RevenueChaser.rcdata import serving
from RevenueChaser.rcdata.build.prices import universe
from RevenueChaser.rcdata.build.prices import serving as prices_serving

from .prices_fixture import SPINE, write_spine


# ── The rule itself ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("security_type, kept", [
    ("common", True), ("adr", True), ("reit", True), ("mlp", True),
    ("bdc", False), ("royalty_trust", False), ("bank_nonsec", False),
    ("spac", False), ("warrant", False), ("unit", False), ("preferred", False), ("cef", False), ("etp", False),
    (None, False), ("unknown", False), ("other", False),
])
def test_only_a_stored_type_is_a_stock_of_the_universe(security_type, kept):
    """avo's approved rule of 4 Nov 2026: ordinary shares, ADRs, REITs and MLPs in. A SPAC, warrant, unit,
    preferred, closed-end fund and an unknown type are out, and so are a BDC, a royalty trust and a non-SEC bank,
    which operate a business but are not stored. An untyped name stays in Tier A and is still out of the store."""
    assert universe.in_stock({"tier": "A", "security_type": security_type}) is kept


def test_every_stored_stock_type_is_one_the_identity_master_calls_operating():
    """The two lists may differ -- the store keeps four of the seven -- but never disagree: a type the store keeps
    that the identity master does not call operating would be a typo, not a decision."""
    from RevenueChaser.core.identity.types import OPERATING_TYPES
    assert not set(universe.STOCK_TYPES) - OPERATING_TYPES


@pytest.mark.parametrize("tier, kept", [("A", True), ("D", True), ("B", False), ("C", False), (None, False)])
def test_the_stored_tiers_are_a_and_d(tier, kept):
    """avo's approved rule: Tier A (listed on Nasdaq, NYSE or Cboe) or Tier D (no exchange in the SEC file). Tier B
    is a listed excluded type and Tier C is over the counter, and neither is stored."""
    assert universe.in_stock({"tier": tier, "security_type": "common"}) is kept


@pytest.mark.parametrize("row, kept", [
    ({"aum": 100_000_000.0}, True),
    ({"aum": 2_000_000_000.0}, True),
    ({"aum": 2e9, "is_lev_inverse": True}, True),          # leveraged and inverse funds are in (avo's words)
    ({"aum": 99_999_999.0}, False),
    ({"aum": None}, False),                                 # an unknown AUM is out, not treated as zero or as large
    ({"aum": 5e9, "is_etn": True}, False),
    ({"aum": 5e9, "is_cef": True}, False),
])
def test_an_etf_is_kept_on_its_aum_with_notes_and_closed_end_funds_left_out(row, kept):
    assert universe.in_etf(row) is kept
    assert universe.ETF_MIN_AUM == 100_000_000.0


def test_the_sql_rule_is_the_twin_of_the_python_rule():
    """One rule, two readers: the fetch scope reads the Python one, the build reads the SQL one. An ETF the spine's
    ``security`` table does not hold (``BIG``) is still in the list, which is why it is a list of tickers."""
    secs = [{"ticker": "AAA", "tier": "A", "security_type": "common"},
            {"ticker": "PFD", "tier": "B", "security_type": "preferred"},
            {"ticker": "NEW", "tier": "A", "security_type": None}]
    etfs = [{"ticker": "BIG", "aum": 5e9, "is_etn": None, "is_cef": None},
            {"ticker": "TINY", "aum": 1e6, "is_etn": None, "is_cef": None},
            {"ticker": "NOTE", "aum": 5e9, "is_etn": True, "is_cef": None}]
    con = duckdb.connect()
    con.execute("CREATE TABLE stg_spine (ticker TEXT, tier TEXT, security_type TEXT)")
    for r in secs:
        con.execute("INSERT INTO stg_spine VALUES (?, ?, ?)", [r["ticker"], r["tier"], r["security_type"]])
    con.execute("CREATE TABLE stg_etf (ticker TEXT, aum DOUBLE, is_etn BOOLEAN, is_cef BOOLEAN)")
    for r in etfs:
        con.execute("INSERT INTO stg_etf VALUES (?, ?, ?, ?)",
                    [r["ticker"], r["aum"], r["is_etn"], r["is_cef"]])
    sql = {t for (t,) in con.execute(universe.TICKER_SQL).fetchall()}
    py = {r["ticker"] for r in secs if universe.in_stock(r)} | {r["ticker"] for r in etfs if universe.in_etf(r)}
    assert sql == py == {"AAA", "BIG"}


# ── The ETF list from the live spine ───────────────────────────────────────────────────────────────────────────

def _spine_with_etfs(etfs):
    write_spine()
    path = serving.live_path("spine")
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE etf (ticker TEXT, aum DOUBLE, aum_as_of DATE, is_etn BOOLEAN, is_cef BOOLEAN, "
                    "is_lev_inverse BOOLEAN, inception DATE)")
        for t, aum, etn, cef, lev in etfs:
            con.execute("INSERT INTO etf VALUES (?, ?, NULL, ?, ?, ?, NULL)", [t, aum, etn, cef, lev])
    finally:
        con.close()


def test_a_spine_without_an_etf_table_is_no_error():
    write_spine()                                            # schema 3: no etf table at all
    assert universe.etf_tickers() == set()


def test_the_etf_list_comes_from_the_live_spines_etf_table():
    _spine_with_etfs([("SPY", 5e11, None, None, None), ("TINY", 1e6, None, None, None),
                      ("NOTE", 5e9, True, None, None), ("DHY", 5e9, None, True, None),
                      ("TQQQ", 2e10, None, None, True), ("NOAUM", None, None, None, None)])
    assert universe.etf_tickers() == {"SPY", "TQQQ"}


# ── avo's own list, when the spine holds one ───────────────────────────────────────────────────────────────────

def test_without_the_list_the_rule_decides():
    write_spine()
    assert universe.stored_tickers() is None, "no my_universe table: the rule decides"
    assert universe.members(SPINE, etfs={"SPY"}) == {"AAPL", "BRK-B", "FLG", "GAPX", "SPY"}


def test_the_stored_list_is_the_universe_and_the_rule_is_not_applied():
    """The work order of 4 Nov 2026: the spine will hold ``my_universe`` (avo's own list, 7,363 tickers), and this
    is the one place that reads it. A list that holds a name the rule would drop keeps it, and one that leaves out a
    name the rule would keep drops it."""
    write_spine()
    con = duckdb.connect(str(serving.live_path("spine")))
    try:
        con.execute(f"CREATE TABLE {universe.MY_UNIVERSE_TABLE} (ticker TEXT, kind TEXT)")
        con.execute(f"INSERT INTO {universe.MY_UNIVERSE_TABLE} VALUES ('AAPL', 'stock'), ('TQQQ', 'etf')")
    finally:
        con.close()
    assert universe.stored_tickers() == {"AAPL", "TQQQ"}
    assert universe.members(SPINE, etfs={"SPY"}) == {"AAPL", "TQQQ"}, "the list wins over the rule, both ways"


# ── The served tables keep the universe only ───────────────────────────────────────────────────────────────────

def test_the_build_filters_every_stored_source_through_one_universe_table():
    """``_universe`` is built from the spine alone, and the three sources of a stored bar (Alpaca, Compustat, CRSP)
    each go through it, so nothing outside today's universe is ever written."""
    assert universe.UNIVERSE_SQL.count("stg_spine") >= 1
    for sql in (prices_serving.BUILD_SQL, ):
        assert "_universe" in sql
    from RevenueChaser.rcdata.build.prices import wrds_history
    for name, sql in (("CRSP_BARS", wrds_history.CRSP_BARS), ("COMP_BARS", wrds_history.COMP_BARS)):
        assert universe.IN_UNIVERSE.split("{sid}")[1].strip() in sql, f"{name} does not filter on the universe"


# ── The big tables' memory rule (avo, 4 Nov 2026) ──────────────────────────────────────────────────────────────

def test_the_primary_key_stays_on_unless_the_switch_drops_it(monkeypatch):
    """avo, 4 Nov 2026: the key stays on, because the store cut to today's universe (about 7,400 names) builds an
    index that fits. ``RC_PRICES_NO_PRIMARY_KEY=1`` drops it on the two tables with a row per security and session,
    for a build that still does not fit DuckDB's 5 GB -- no code change, and the key is checked by a query then."""
    assert prices_serving.MAY_DROP_PRIMARY_KEY == ("price_bar_daily", "market_cap_daily")
    assert set(prices_serving.KEYS) == set(prices_serving.MAY_DROP_PRIMARY_KEY)
    monkeypatch.delenv(prices_serving.NO_PRIMARY_KEY_ENV, raising=False)
    assert prices_serving.no_primary_key() == ()
    for value in ("", "0", "false"):
        monkeypatch.setenv(prices_serving.NO_PRIMARY_KEY_ENV, value)
        assert prices_serving.no_primary_key() == ()
    monkeypatch.setenv(prices_serving.NO_PRIMARY_KEY_ENV, "1")
    assert prices_serving.no_primary_key() == prices_serving.MAY_DROP_PRIMARY_KEY


@pytest.mark.parametrize("dropped", [False, True])
def test_the_switch_decides_whether_duckdb_holds_the_index(monkeypatch, dropped):
    monkeypatch.setenv(prices_serving.NO_PRIMARY_KEY_ENV, "1" if dropped else "0")
    con = duckdb.connect()
    for table in prices_serving.MAY_DROP_PRIMARY_KEY:
        prices_serving._create(con, table, "SELECT " + ", ".join(
            f"CAST(NULL AS {t}) AS \"{c}\"" for c, t in prices_serving.SCHEMAS[table]) + " WHERE false")
        keys = con.execute("SELECT count(*) FROM duckdb_constraints() WHERE table_name = ? "
                           "AND constraint_type = 'PRIMARY KEY'", [table]).fetchone()[0]
        assert keys == (0 if dropped else 1), table


def test_the_key_check_finds_a_duplicate_the_dropped_primary_key_would_have_refused():
    con = duckdb.connect()
    con.execute("CREATE TABLE price_bar_daily (security_id TEXT, session_date DATE)")
    con.execute("INSERT INTO price_bar_daily VALUES ('a', '2026-01-02'), ('b', '2026-01-02')")
    assert prices_serving.duplicate_keys(con, "price_bar_daily") == []
    con.execute("INSERT INTO price_bar_daily VALUES ('a', '2026-01-02')")
    assert prices_serving.duplicate_keys(con, "price_bar_daily") == [("a:2026-01-02", 2)]
