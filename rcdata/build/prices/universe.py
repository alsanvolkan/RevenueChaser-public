"""``prices/universe``: the one list of securities the prices store keeps, and the only place that rule lives.

avo, 4 Nov 2026 (PR 382): the store holds **today's universe**, not every tier and not the whole history.

- **Stocks**: a name on :data:`STOCK_TIERS` (Tier A, listed on Nasdaq, NYSE or Cboe, or Tier D, no exchange in the
  SEC file) whose type is one of :data:`STOCK_TYPES` — ordinary shares, ADRs, REITs and MLPs. A SPAC, warrant, unit,
  right, preferred or closed-end fund is out, and so are a BDC, a royalty trust, a non-SEC bank and a name with
  **no type yet**: Tier A keeps an untyped name (``spine.tier``, "an untyped name is never hidden"), this list does
  not. 5,349 on the live spine of 4 Nov 2026.
- **ETFs**: a row of the spine's ``etf`` table whose ``aum`` is at or over :data:`ETF_MIN_AUM`, with notes
  (``is_etn``) and closed-end funds (``is_cef``) left out; 2,071 on that spine. A leveraged or inverse fund is in
  (avo's words). An ETF whose AUM is unknown is out **here**: ``aum`` is NULL for a row N-CEN does not hold, and no
  comparison keeps a NULL. avo's approved rule keeps such a fund when its 20-day average daily dollar volume is at
  or over 10 million; that branch is the ``my_universe`` build's (``spine/my_universe.py``), not this module's, so
  without that table an unknown-AUM fund is not stored. That build reads the volume from Compustat's **raw**
  ``comp.secd`` Parquet (#421), not from this store: a store cut to the universe holds no bar of a fund outside it,
  so a new fund could never earn a volume here (review M4 of #382, avo's decision B of 5 Nov 2026).
- **A delisted name is never in it.** The list is read from the live spine, which holds today's listings only, so
  nothing delisted reaches the served tables at all (the reason PR 382's "CRSP for delisted names" step is gone).

**The list avo picks himself.** When the live spine holds a ``my_universe`` table (one row per ticker), **that
table is the universe** and the rule above is not applied: avo's hand-picked list, with the rule-based and
by-hand additions and removals the spine's own build resolves (``MY_UNIVERSE_TABLE``, built by
``spine/my_universe.py`` since #417). Both readers below check for it first, so nothing else in the build changes
when it arrives.

Both sides of the build read the rule from here: :func:`in_stock` and :func:`etf_tickers` for the daily fetch scope
(``prices.scope``), :data:`MEMBER` and :data:`UNIVERSE_SQL` for the serving build's ``_universe`` table. The raw
sources keep fetching whatever their own scope says; this module decides only what is **stored**. avo may replace
the rule with a list he picks himself: then this module is the one file that changes.
"""
from __future__ import annotations

#: The spine tiers a stock of the universe may be on: A (listed on Nasdaq, NYSE or Cboe) and D (no exchange in the
#: SEC file). avo's words of 4 Nov 2026, the rule he approved for ``my_universe``.
STOCK_TIERS = ("A", "D")
#: The security types the store keeps (avo, 4 Nov 2026): ordinary shares, ADRs, REITs and MLPs. A BDC, a royalty
#: trust and a non-SEC bank operate a business too, and the identity master keeps them in ``OPERATING_TYPES``, but
#: they are not stored. Every one of these is one of the identity master's operating types, which a test pins.
STOCK_TYPES = ("adr", "common", "mlp", "reit")
#: The smallest average net assets (N-CEN, ``etf.aum``) an ETF is kept for: 100 million dollars.
ETF_MIN_AUM = 100_000_000.0
#: The spine table that holds avo's own list, one row per ticker, when it exists: then it **is** the universe.
MY_UNIVERSE_TABLE = "my_universe"
#: ``_universe_ticker`` from that list, which ``_spine`` stages as ``stg_my_universe``.
STORED_TICKER_SQL = f"SELECT ticker FROM stg_{MY_UNIVERSE_TABLE} WHERE ticker IS NOT NULL"

_TYPES_SQL = ", ".join(f"'{t}'" for t in STOCK_TYPES)
_TIERS_SQL = ", ".join(f"'{t}'" for t in STOCK_TIERS)
#: The stock rule over ``stg_spine``'s own columns, the SQL twin of :func:`in_stock`.
STOCK_MEMBER = f"(tier IN ({_TIERS_SQL}) AND security_type IN ({_TYPES_SQL}))"
#: The ETF rule over ``stg_etf``'s columns (the live spine's ``etf`` table), the SQL twin of :func:`in_etf`.
ETF_MEMBER = f"(aum >= {ETF_MIN_AUM} AND NOT coalesce(is_etn, false) AND NOT coalesce(is_cef, false))"

#: ``_universe_ticker``: the tickers of the universe, the two rules in one list. An ETF the spine's ``security``
#: table does not hold (an Alpha Vantage or N-CEN fund) is in it all the same, which is why the list is by ticker
#: and the ids are resolved from it below. The named groups the daily scope asks beside the universe
#: (:func:`kept_beside`, the benchmarks, the rotation ETFs and the stock watch list) are added to the same table,
#: because a board reads their bars from the store and the rule must not drop them.
TICKER_SQL = f"""
    SELECT ticker FROM stg_spine WHERE {STOCK_MEMBER} AND ticker IS NOT NULL
    UNION
    SELECT ticker FROM stg_etf WHERE {ETF_MEMBER} AND ticker IS NOT NULL"""

# ``_universe``: the security ids the served tables keep. The ids of the spine's own rows, and three kinds no spine
# ``security`` row carries:
#   - every Compustat class of a member's ``gvkey`` (Berkshire's A shares under ``072726:01``), so the issuer market
#     cap still counts each listed class (``serving.MARKET_CAP_SQL``'s ``sec``, the class the spine lacks);
#   - the id an Alpaca bar of a member ticker arrived under, for an ETF the ``security`` table does not hold
#     (``sym:<ticker>``);
#   - a class the SEC covers name under a member's CIK that the spine itself does not hold
#     (``prices.cover_classes`` asks it). A row the spine **does** hold stays excluded: a preferred of a member
#     issuer is out whatever its cover says;
#   - the Compustat series of a universe ETF the spine cannot route to Compustat (no ``security`` row, or one with no
#     ``gvkey``): Alpaca serves no bar, so without this the fund had none at all (review M3 of #382). One series per
#     ticker, the ``pick = 1`` row of :data:`ETF_TIC_SERIES_SQL`'s ``_etf_tic_series``, which ``serving._universe``
#     builds first.
UNIVERSE_SQL = """
    WITH m AS (SELECT security_id, nullif(cik, '') AS cik, nullif(gvkey, '') AS gvkey FROM stg_spine
               WHERE ticker IN (SELECT ticker FROM _universe_ticker))
    SELECT security_id FROM m WHERE security_id IS NOT NULL
    UNION
    SELECT DISTINCT d.gvkey || ':' || d.iid FROM stg_wrds_secd d
    WHERE d.gvkey IN (SELECT gvkey FROM m WHERE gvkey IS NOT NULL)
    UNION
    SELECT DISTINCT b.security_id FROM stg_alpaca_bars b
    WHERE b.ticker IN (SELECT ticker FROM _universe_ticker)
       OR (b.cik IN (SELECT cik FROM m WHERE cik IS NOT NULL)
           AND NOT EXISTS (SELECT 1 FROM stg_spine s WHERE s.ticker = b.ticker
                           AND s.cik IS NOT DISTINCT FROM b.cik))
    UNION
    SELECT gid FROM _etf_tic_series WHERE pick = 1"""


def norm(column: str) -> str:
    """A ticker as the spine writes one: upper case, ``-`` for ``.`` (Compustat's ``tic`` writes ``BRK.B``)."""
    return f"replace(upper({column}), '.', '-')"


#: ``_universe_etf``: the funds of the universe, ``ticker`` as ``_universe_ticker`` holds it and ``tic`` in
#: :func:`norm`'s form. A fund is a ticker of the spine's ``etf`` table, or one the ``my_universe`` list holds as
#: ``kind = 'etf'`` (a fund avo added by hand that the ``etf`` table lacks; review D5 of #420). Both sides compared
#: in :func:`norm`'s form (NIT 2 of #420).
UNIVERSE_ETF_SQL = f"""
    SELECT DISTINCT t.ticker, {norm('t.ticker')} AS tic FROM _universe_ticker t
    WHERE {norm('t.ticker')} IN (SELECT {norm('ticker')} FROM stg_etf WHERE ticker IS NOT NULL)
       OR {norm('t.ticker')} IN (SELECT {norm('ticker')} FROM stg_{MY_UNIVERSE_TABLE} WHERE kind = 'etf')"""
#: How recent (calendar days before Compustat's newest priced day) a series' newest day must be for its ticker to
#: name a universe ETF (``_etf_tic_series``): two weeks covers a holiday week and a late Compustat row.
ETF_TIC_RECENT_DAYS = 14
#: ``_etf_tic_series``: every Compustat series whose ticker names a universe fund the spine cannot route to Compustat
#: (no ``security`` row, or one with no ``gvkey``), ranked per ticker. A series is named by the ticker it carries on
#: its own newest priced day, and only when that day is within :data:`ETF_TIC_RECENT_DAYS` of Compustat's newest one
#: up to the build's date (read once): a series that carried the ticker long ago is a reused ticker's, not this
#: fund's. A series the spine holds under another ticker is out. **One series per ticker is stored** (``pick = 1``):
#: the one priced most recently, then the larger dollar volume on that day, then the lower id. More than one is the
#: ``universe_etf_ticker_shared`` issue: two stored would give the fund two rows per session in ``price_bars_daily``,
#: each its own key, and no check would see it (review D1 of #420).
ETF_TIC_SERIES_SQL = f"""
    WITH newest AS (SELECT max(datadate) AS day FROM stg_wrds_secd WHERE prccd > 0
                    AND datadate <= coalesce(getvariable('rc_build_date'), DATE '9999-12-31')),
    routeless AS (SELECT u.tic FROM _universe_etf u WHERE NOT EXISTS (
                      SELECT 1 FROM stg_spine s WHERE {norm('s.ticker')} = u.tic AND nullif(s.gvkey, '') IS NOT NULL)),
    series AS (SELECT gvkey || ':' || iid AS gid, {norm('tic')} AS tic, datadate, prccd * cshtrd AS dollars
               FROM stg_wrds_secd, newest
               WHERE prccd > 0 AND datadate <= newest.day AND datadate > newest.day - INTERVAL {ETF_TIC_RECENT_DAYS} DAY
               QUALIFY row_number() OVER (PARTITION BY gvkey, iid ORDER BY datadate DESC) = 1)
    SELECT s.tic, s.gid, s.datadate, s.dollars,
           row_number() OVER (PARTITION BY s.tic ORDER BY s.datadate DESC, s.dollars DESC NULLS LAST, s.gid) AS pick
    FROM series s
    WHERE s.tic IN (SELECT tic FROM routeless)
      AND s.gid NOT IN (SELECT security_id FROM stg_spine WHERE security_id IS NOT NULL)"""
#: ``security_id IN`` the universe, the filter every stored source goes through.
IN_UNIVERSE = "{sid} IN (SELECT security_id FROM _universe)"


def in_stock(row: dict) -> bool:
    """One spine ``security`` row is a stock of the universe."""
    return row.get("tier") in STOCK_TIERS and row.get("security_type") in STOCK_TYPES


def in_etf(row: dict) -> bool:
    """One spine ``etf`` row is an ETF of the universe (an unknown ``aum`` is not)."""
    aum = row.get("aum")
    return aum is not None and aum >= ETF_MIN_AUM and not row.get("is_etn") and not row.get("is_cef")


def etf_tickers() -> set[str]:
    """The tickers of the universe's ETFs, from the live spine's ``etf`` table; empty when it has none (no spine
    yet, or a spine older than schema 4, which has no such table)."""
    from ... import serving
    try:
        con = serving.open_read("spine")
    except serving.ServingFileMissing:
        return set()
    try:
        if not con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'etf'").fetchone()[0]:
            return set()
        return {t for (t,) in con.execute(f"SELECT ticker FROM etf WHERE {ETF_MEMBER} AND ticker IS NOT NULL"
                                          ).fetchall()}
    finally:
        con.close()


def kept_beside(watched: set[str] | None = None) -> set[str]:
    """The tickers the store keeps **beside** the universe: the three benchmarks and the rotation ETFs
    (``prices.BENCHMARKS``, ``prices.ROTATION``) and the stock watch list, the same named groups the daily scope
    asks for (``prices.scope``'s ``anchors`` and ``watchlist``). A board reads these from the store -- the S&P
    benchmark of a chart, the sector ETFs of the rotation boards -- so the universe rule must not drop one, whatever
    its AUM or type says. The other listed classes of a universe issuer (``prices.cover_classes``) are kept by
    ``UNIVERSE_SQL``'s CIK rule instead, because they are asked under ids of their own."""
    from . import BENCHMARKS, ROTATION
    if watched is None:
        from ... import read
        watched = read.watched("stock")
    return {str(t).strip().upper() for t in (*BENCHMARKS, *ROTATION, *watched) if str(t).strip()}


def stored_tickers() -> set[str] | None:
    """avo's own list from the live spine's ``my_universe`` table, or ``None`` when the spine has no such table
    (then the rule above decides). A table that exists and is empty is an empty universe, not a missing one.

    ``read.my_universe_tickers`` is the one reader of that distinction, shared with the boards' universe and the
    fundamentals build; this adds only the spelling the staging SQL compares against."""
    from ... import read
    tickers = read.my_universe_tickers()
    return None if tickers is None else {str(t).strip().upper() for t in tickers}


def members(rows: list[dict], etfs: set[str] | None = None) -> set[str]:
    """The tickers of the universe: avo's stored list when the spine has one, else the rule (the Tier A operating
    names of ``rows`` and the ETFs of ``etf_tickers``). ``rows`` are the spine's ``security`` rows."""
    stored = stored_tickers()
    if stored is not None:
        return stored
    funds = etf_tickers() if etfs is None else etfs
    return {r["ticker"] for r in rows if in_stock(r)} | set(funds)
