"""``prices/serving``: the served prices store, built from the sources' staging tables (no fetch, no network).

**Tables** (``prices.duckdb``, serving schema version 1), every one keyed on the security id (``prices.security_id``):

- ``price_bar_daily``: one bar per security and New York session, **nominal** (the prices as traded that day):
  Alpaca's daily bars, then the WRDS history where Alpaca has no bar (``wrds/prices``: CRSP where it links to the
  Compustat security, else Compustat; the long run is Phase 8), and the S&P 500 index level from the market store's
  Cboe file (``index:SPX``, the close only);
- ``corporate_action``: Alpaca's actions of the store's securities, resolved to the security that traded under the
  action's symbol nearest its date (else the spine's current ticker), and Compustat's splits and dividends of the
  securities with bars where Alpaca has no action of that kind that day, with ``applied`` (used by ``adj_factor``),
  ``not_applied_why``, the split ``ratio`` (new over old shares) and the cash ``amount`` per share; an action whose
  ex-date is after the newest session is kept, not applied, and raises no issue until it goes ex;
- ``adj_factor``: per security, the periods ``[valid_from, valid_to)`` between two ex-dates and the factors that
  turn a nominal bar of the period into today's basis: ``split_factor`` (the product of ``1 / ratio`` of every split
  and stock dividend after it) and ``tr_factor`` (that, times ``1 - amount / close before the ex-date`` of every cash
  dividend after it). A bar after the last ex-date, or of a security with no action, has both factors 1. The
  factors are applied on read (``rcdata.read.bars``); nothing stored is ever adjusted;
- ``shares_class``: shares per class and day: ``outstanding`` from the SEC covers (an unlisted class keyed
  ``class:<CIK>:<member>``, and so is an ADR issuer's count, which is of ordinary shares, not of the ADSs),
  ``float`` from Yahoo and from Unusual Whales;
- ``security_symbol``: the symbols each security traded under and when (from its bars; the spine's ticker for a Tier
  A name with none yet);
- ``market_cap_daily``: close times shares per security and session, the shares being the bar's own (a history
  source's), else a WRDS source's of that session, else the newest cover on or before the session, moved over the
  splits between the two; and the issuer's sum over its listed classes, each at its newest cap at most
  ``ISSUER_WITHIN_DAYS`` old (``issuer_market_cap``; an unlisted class has no price and is not in it);
- ``price_revision``: the values a later read replaced (Alpaca's re-read of the last sessions, the monthly WRDS
  refresh), with when and by which run;
- ``wrds_price_diff``: the WRDS difference report (``wrds_history.COMPARISONS``), one row per comparison: the closes
  of CRSP against Compustat and of each against Alpaca on the sessions both have, and each WRDS source's own day
  return against the store's (its closes, the store's factors) on consecutive sessions; a difference over 1 bp
  counts. Shown on the ``wrds/prices`` Settings card.

**Views** under the old ``prices.duckdb`` names (D4), with the old columns: ``price_bars_daily`` (split-adjusted OHLC,
``adj_close`` total-return adjusted, both to today, ``fetched_at`` the build time so the old reader applies no split
a second time), ``price_actions`` (splits and dividends, the amount in today's share basis) and
``index_levels_daily`` (``^GSPC``).

**Build issues** (``ISSUE_KINDS``) are computed here and written to ``ops.sqlite`` once the new file is live
(``after_swap``, ``ops.sync_issues``): an issue found again stays open, one no longer found is resolved.
"""
from __future__ import annotations

from datetime import date

import duckdb

from ... import ops
from .. import base
from . import KIND, stage, wrds_history

NAME = "prices/serving"
TABLES = ("price_bar_daily", "corporate_action", "adj_factor", "shares_class", "security_symbol",
          "market_cap_daily", "price_revision", "wrds_price_diff")
ALPACA_BARS, ALPACA_ACTIONS = "alpaca/bars", "alpaca/corporate_actions"
COVER, YAHOO, UW = "sec/cover_shares", "yfinance/float", "uw/total_float"
INDEX_SOURCE = "market_series/cboe"
# A day whose split-adjusted close moved by this factor or more, either way, with no split that day.
GAP_FACTOR = 1.9
# The 1-bp rule: our total-return day return against the source's own adjusted one.
TOLERANCE = 1e-4
# Alpaca's adjusted closes come rounded to the cent, so its day return can be off by half a cent on each close
# (KO in the server's sample, 29 Oct 2026: 1.1-1.3 bp at ~$70 between two days no factor separates). The rule
# allows that much where it is more than 1 bp: the widest the return can move with the two closes half a cent apart
# in opposite directions, h * (close + previous) / (previous * (previous - h)), exact, since a row can sit on it
# (ADV, 29 Oct: 20.395 and 19.865 rounded apart; the first-order bound was 0.02 % short).
# shortcut: the cent rounding is read from the sample's KO rows, Alpaca does not document it; measure it on the server
# (count of rows left, and their sizes) and narrow or widen this once a full Tier A build runs.
HALF_CENT = 0.005
FLOAT_OVER = 1.05
FLOAT_DISAGREE = 0.25
# A cover count behind a market cap and Yahoo's sharesOutstanding further apart than this, on the session of Yahoo's
# answer, is an issue (AEHL in the server's sample: a count from before its dilution, 400 times too small).
SHARES_DISAGREE = 0.25
# A dividend is applied with the close of the last bar before its ex-date, at most this many days before it.
CLOSE_BEFORE_DAYS = 7
# The issuer cap of a session counts each listed class at its newest cap at most this many days old (a thin class
# that did not trade that day still counts): the window ``rcdata.read.latest_market`` reads the newest cap in.
ISSUER_WITHIN_DAYS = 14
APPLIED = ("split", "stock_dividend", "cash_dividend")
NOT_APPLIED = ("spin_off", "unit_split", "rights_distribution")

ISSUE_KINDS = {
    "source_never_run": "a source this build needs has no data yet (key: the dataset)",
    "tier_a_no_bar": "a Tier A name with no bar on the newest Alpaca session (key: the security id)",
    "tier_a_no_shares": "a Tier A name with no shares, so no market cap, on the newest session (key: the security id)",
    "price_gap_no_action": ("the split-adjusted close moved by GAP_FACTOR or more with no split that day "
                            "(key: security:session)"),
    "adjustment_differs": ("our total-return day return is more than 1 bp from the source's own, or more than the "
                           "rounding of its cent-rounded closes where that is larger (key: security:session)"),
    "dividend_not_applied": "a cash dividend the factors could not use (key: the action id)",
    "action_not_applied": "a spin-off, unit split or rights distribution the factors do not apply (key: the action id)",
    "cover_class_unmapped": "a cover class whose trading symbol is no spine ticker of its CIK (key: CIK:symbol)",
    "cover_adr_ordinary": ("an ADR's issuer's cover count: ordinary shares, not ADSs, so kept under class: and no "
                           "market cap from it (key: the ADR's security id)"),
    "float_above_outstanding": "the newest float is above FLOAT_OVER times the shares outstanding (key: the security id)",
    "float_sources_disagree": "Yahoo's and UW's newest float differ by more than FLOAT_DISAGREE (key: the security id)",
    "shares_disagree_yahoo": ("the cover shares behind a market cap and Yahoo's newest sharesOutstanding differ by more "
                              "than SHARES_DISAGREE on the session of Yahoo's answer (key: the security id)"),
}

PRICE_BAR = [("security_id", "TEXT"), ("session_date", "DATE"), ("symbol", "TEXT"), ("cik", "TEXT"),
             ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"), ("volume", "DOUBLE"),
             ("vwap", "DOUBLE"), ("trade_count", "BIGINT"), ("shares_outstanding", "DOUBLE"), ("source", "TEXT"),
             ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
CORPORATE_ACTION = [("security_id", "TEXT"), ("ex_date", "DATE"), ("action", "TEXT"), ("action_type", "TEXT"),
                    ("ratio", "DOUBLE"), ("amount", "DOUBLE"), ("currency", "TEXT"), ("special", "BOOLEAN"),
                    ("applied", "BOOLEAN"), ("not_applied_why", "TEXT"), ("symbol", "TEXT"), ("new_symbol", "TEXT"),
                    ("record_date", "DATE"), ("payable_date", "DATE"), ("process_date", "DATE"), ("source", "TEXT"),
                    ("source_id", "TEXT"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"), ("run_id", "TEXT")]
ADJ_FACTOR = [("security_id", "TEXT"), ("valid_from", "DATE"), ("valid_to", "DATE"), ("split_factor", "DOUBLE"),
              ("tr_factor", "DOUBLE"), ("events", "INTEGER")]
SHARES_CLASS = [("security_id", "TEXT"), ("measure", "TEXT"), ("as_of", "DATE"), ("value", "DOUBLE"),
                ("symbol", "TEXT"), ("cik", "TEXT"), ("class_member", "TEXT"), ("source", "TEXT"),
                ("source_ref", "TEXT"), ("filed", "DATE"), ("source_as_of", "DATE"), ("fetched_at", "DOUBLE"),
                ("run_id", "TEXT")]
SECURITY_SYMBOL = [("security_id", "TEXT"), ("symbol", "TEXT"), ("cik", "TEXT"), ("valid_from", "DATE"),
                   ("valid_to", "DATE"), ("source", "TEXT")]
MARKET_CAP = [("security_id", "TEXT"), ("session_date", "DATE"), ("cik", "TEXT"), ("close", "DOUBLE"),
              ("shares", "DOUBLE"), ("shares_source", "TEXT"), ("shares_as_of", "DATE"), ("market_cap", "DOUBLE"),
              ("issuer_market_cap", "DOUBLE")]
PRICE_REVISION = [("security_id", "TEXT"), ("session_date", "DATE"), ("symbol", "TEXT"), ("open", "DOUBLE"),
                  ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"), ("volume", "DOUBLE"), ("source", "TEXT"),
                  ("fetched_at", "DOUBLE"), ("run_id", "TEXT"), ("revised_at", "DOUBLE"), ("revised_by", "TEXT")]
WRDS_PRICE_DIFF = [("comparison", "TEXT"), ("as_of", "DATE"), ("securities", "INTEGER"), ("sessions", "INTEGER"),
                   ("close_differs", "INTEGER"), ("max_close_diff", "DOUBLE"), ("return_differs", "INTEGER"),
                   ("max_return_diff", "DOUBLE"), ("first_session", "DATE"), ("last_session", "DATE")]
SCHEMAS = {"price_bar_daily": PRICE_BAR, "corporate_action": CORPORATE_ACTION, "adj_factor": ADJ_FACTOR,
           "shares_class": SHARES_CLASS, "security_symbol": SECURITY_SYMBOL, "market_cap_daily": MARKET_CAP,
           "price_revision": PRICE_REVISION, "wrds_price_diff": WRDS_PRICE_DIFF}
KEYS = {"price_bar_daily": ("security_id", "session_date"), "market_cap_daily": ("security_id", "session_date")}

# The security id of a spine row, the SQL twin of ``prices.security_id``.
SPINE_ID = ("CASE WHEN nullif(gvkey, '') IS NOT NULL AND nullif(iid, '') IS NOT NULL THEN gvkey || ':' || iid "
            "WHEN nullif(composite_figi, '') IS NOT NULL THEN 'figi:' || composite_figi "
            "WHEN nullif(cik, '') IS NOT NULL THEN 'cik:' || cik || ':' || upper(ticker) "
            "ELSE 'sym:' || upper(ticker) END")
# The factor period a day falls in: ``<a>.valid_from <= <day> < <a>.valid_to``, open at both ends.
IN_PERIOD = ("{a}.security_id = {sid} AND {day} >= coalesce({a}.valid_from, DATE '0001-01-01') "
             "AND ({a}.valid_to IS NULL OR {day} < {a}.valid_to)")


def period(a: str, sid: str, day: str) -> str:
    return IN_PERIOD.format(a=a, sid=sid, day=day)


def _create(con: duckdb.DuckDBPyConnection, table: str, select: str) -> int:
    schema = SCHEMAS[table]
    cols = ", ".join(f'"{c}" {t}' for c, t in schema)
    key = KEYS.get(table)
    con.execute(f"CREATE TABLE {table} ({cols}{f', PRIMARY KEY ({chr(44).join(key)})' if key else ''})")
    names = ", ".join(f'"{c}"' for c, _ in schema)
    con.execute(f"INSERT INTO {table} ({names}) SELECT {names} FROM ({select})")
    return stage.count(con, table)


def _spine(con: duckdb.DuckDBPyConnection) -> int:
    """``stg_spine``: the live spine's ``security`` rows with their security id (empty before a first spine)."""
    from ... import serving
    con.execute("CREATE OR REPLACE TEMP TABLE stg_spine (ticker TEXT, cik TEXT, tier TEXT, security_type TEXT, "
                "gvkey TEXT, iid TEXT, composite_figi TEXT, security_id TEXT)")
    live = serving.live_path("spine")
    if not live.exists():
        return 0
    con.execute(f"ATTACH {base.sql_path(live)} AS live_spine (READ_ONLY)")
    try:
        con.execute(f"INSERT INTO stg_spine SELECT ticker, cik, tier, security_type, gvkey, iid, composite_figi, "
                    f"{SPINE_ID} FROM live_spine.security")
    finally:
        con.execute("DETACH live_spine")
    return stage.count(con, "stg_spine")


def _index_levels(con: duckdb.DuckDBPyConnection) -> str:
    """The S&P 500 index level from the market store's Cboe Parquet, as bars (``index:SPX``)."""
    parts = sorted((base.parquet_root() / "cboe" / "index").glob("year=*/part-0.parquet"))
    empty = "SELECT " + ", ".join(f"CAST(NULL AS {t}) AS {c}" for c, t in PRICE_BAR) + " WHERE false"
    if not parts:
        return empty
    files = ", ".join(base.sql_path(p) for p in parts)
    return ("SELECT 'index:SPX' AS security_id, session_date, 'SPX' AS symbol, CAST(NULL AS TEXT) AS cik, open, high, "
            "low, coalesce(close, value) AS close, CAST(NULL AS DOUBLE) AS volume, CAST(NULL AS DOUBLE) AS vwap, "
            "CAST(NULL AS BIGINT) AS trade_count, CAST(NULL AS DOUBLE) AS shares_outstanding, "
            f"'{INDEX_SOURCE}' AS source, source_as_of, fetched_at, run_id FROM read_parquet([{files}], "
            "union_by_name = true) WHERE series_id = 'SPX' AND session_date = obs_date "
            "QUALIFY row_number() OVER (PARTITION BY obs_date ORDER BY fetched_at DESC) = 1")


BUILD_SQL = """
CREATE OR REPLACE TEMP TABLE _alp AS
    SELECT coalesce(s.security_id, b.security_id) AS security_id, b.session_date, b.ticker AS symbol, b.cik,
           b.open, b.high, b.low, b.close, b.volume, b.vwap, b.trade_count,
           CAST(NULL AS DOUBLE) AS shares_outstanding, '{bars}' AS source, b.source_as_of, b.fetched_at, b.run_id,
           b.symbol AS alpaca_symbol, b.close_all, b.all_return, b.tier
    FROM stg_alpaca_bars b LEFT JOIN stg_spine s ON s.ticker = b.ticker AND s.cik IS NOT DISTINCT FROM b.cik
    QUALIFY row_number() OVER (PARTITION BY coalesce(s.security_id, b.security_id), b.session_date
                               ORDER BY b.fetched_at DESC) = 1;
CREATE OR REPLACE TEMP TABLE _symsec AS
    SELECT alpaca_symbol, security_id, min(session_date) AS lo, max(session_date) AS hi FROM _alp GROUP BY ALL;
CREATE OR REPLACE TEMP TABLE _act AS
    WITH a AS (SELECT *, coalesce(ex_date, effective_date, process_date) AS eff FROM stg_alpaca_actions
               QUALIFY row_number() OVER (PARTITION BY action_id ORDER BY fetched_at DESC) = 1),
    cand AS (
        SELECT a.action_id, m.security_id, 0 AS pri,
               CASE WHEN a.eff < m.lo THEN date_diff('day', a.eff, m.lo)
                    WHEN a.eff > m.hi THEN date_diff('day', m.hi, a.eff) ELSE 0 END AS dist
        FROM a JOIN _symsec m ON m.alpaca_symbol = upper(a.symbol)
        UNION ALL
        SELECT a.action_id, s.security_id, 1 AS pri, 0 AS dist
        FROM a JOIN stg_spine s ON s.ticker = replace(upper(a.symbol), '.', '-')
        WHERE s.security_id IN (SELECT security_id FROM _symsec)),
    best AS (SELECT action_id, security_id FROM cand
             QUALIFY row_number() OVER (PARTITION BY action_id ORDER BY pri, dist, security_id) = 1)
    SELECT best.security_id, a.*, '{actions}' AS source FROM a JOIN best USING (action_id);
CREATE OR REPLACE TEMP TABLE _hist_all AS
    SELECT * FROM ({crsp}) UNION ALL BY NAME SELECT * FROM ({comp});
"""
# The kind of an action for "the same action on the same day" (a split and a reverse split are one kind).
ACTION_KIND = ("CASE WHEN {a}.action_type IN ('forward_split', 'reverse_split') THEN 'split' "
               "WHEN {a}.action_type IN ('cash_dividend', 'capital_gains_distribution') THEN 'cash_dividend' "
               "ELSE {a}.action_type END")


class PricesServing:
    name = NAME
    kind = KIND
    providers: tuple[str, ...] = ()
    tables = TABLES
    cadence = "daily"
    backfill_needs_from = False
    starts_by_hand = True

    def default_as_of(self) -> date:
        from ....core import calendar
        return calendar.latest_complete_session()

    def fetch(self, ctx: base.RunContext, as_of: date) -> list:
        return []                                   # built from the other datasets' Parquet only

    def backfill(self, ctx: base.RunContext, start, end: date) -> None:
        return None

    def to_parquet(self, ctx, raw) -> list:
        return []

    def to_parquet_all(self, ctx, raws) -> list:
        return []

    def build(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        from . import bars as _bars  # noqa: F401 - the staging tables below are the sources'
        found: list[tuple[str, str, str]] = []
        have = {ALPACA_BARS: stage.count(con, "stg_alpaca_bars"), ALPACA_ACTIONS: stage.count(con, "stg_alpaca_actions"),
                COVER: stage.count(con, "stg_sec_cover"), YAHOO: stage.count(con, "stg_yahoo_float"),
                UW: stage.count(con, "stg_uw_float")}
        for ds, n in have.items():
            if not n:
                found.append(("source_never_run", ds, f"{ds} has no rows in this build"))
        _spine(con)
        con.execute(BUILD_SQL.format(bars=ALPACA_BARS, actions=ALPACA_ACTIONS, crsp=wrds_history.CRSP_BARS,
                                     comp=wrds_history.COMP_BARS))
        counts = {}
        cols = ", ".join(c for c, _ in PRICE_BAR)
        # Alpaca's bar wins its session; of the history bars, CRSP's before Compustat's.
        bars_sql = (f"SELECT {cols} FROM _alp UNION ALL SELECT * FROM ({_index_levels(con)}) UNION ALL "
                    f"SELECT {cols} FROM (SELECT * FROM _hist_all h WHERE NOT EXISTS (SELECT 1 FROM _alp a "
                    "WHERE a.security_id = h.security_id AND a.session_date = h.session_date) "
                    "QUALIFY row_number() OVER (PARTITION BY h.security_id, h.session_date ORDER BY h.priority) = 1)")
        counts["price_bar_daily"] = _create(con, "price_bar_daily", bars_sql)
        counts["corporate_action"] = self._actions(con, found)
        counts["adj_factor"] = _create(con, "adj_factor", ADJ_SQL)
        counts["shares_class"] = self._shares(con, found)
        counts["security_symbol"] = _create(con, "security_symbol", SYMBOL_SQL)
        counts["market_cap_daily"] = _create(con, "market_cap_daily",
                                             MARKET_CAP_SQL.format(within=ISSUER_WITHIN_DAYS))
        counts["price_revision"] = _create(con, "price_revision",
                                           f"{REVISION_SQL} UNION ALL {wrds_history.REVISIONS_SQL}")
        counts["wrds_price_diff"] = self._wrds_report(con)
        self._views(con)
        found += self._issues(con, have)
        ctx.pending[NAME] = found
        return counts

    # ── Actions ───────────────────────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _actions(con: duckdb.DuckDBPyConnection, found: list) -> int:
        # Compustat's actions of the securities with bars, unless Alpaca has one of that kind that day.
        con.execute(f"""INSERT INTO _act BY NAME SELECT w.* FROM ({wrds_history.ACTIONS_SQL}) w
            WHERE w.security_id IN (SELECT DISTINCT security_id FROM price_bar_daily)
              AND NOT EXISTS (SELECT 1 FROM _act p WHERE p.security_id = w.security_id AND p.ex_date = w.ex_date
                              AND {ACTION_KIND.format(a='p')} = {ACTION_KIND.format(a='w')})""")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _act_typed AS
            SELECT a.*, CASE WHEN action_type IN ('forward_split', 'reverse_split') THEN 'split'
                             WHEN action_type IN ('cash_dividend', 'capital_gains_distribution') THEN 'cash_dividend'
                             ELSE action_type END AS action,
                   CASE WHEN action_type IN ('forward_split', 'reverse_split') AND old_rate > 0 AND new_rate > 0
                            THEN new_rate / old_rate
                        WHEN action_type = 'stock_dividend' AND rate > 0 THEN 1 + rate END AS ratio,
                   CASE WHEN action_type IN ('cash_dividend', 'capital_gains_distribution') THEN rate END AS amount
            FROM _act a""")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _act_close AS
            SELECT a.*, p.close AS close_before, p.session_date AS close_day
            FROM _act_typed a ASOF LEFT JOIN price_bar_daily p
                ON p.security_id = a.security_id AND a.ex_date > p.session_date""")
        # An action announced for a later day (the actions window reaches ahead) waits until its ex-date has a
        # session: applied now, it would turn today's bars into a basis that does not exist yet.
        newest = "(SELECT max(session_date) FROM price_bar_daily WHERE security_id NOT LIKE 'index:%')"
        why = f"""CASE
            WHEN ex_date > {newest} THEN 'not yet ex: the ex-date is after the newest session'
            WHEN action IN ('split', 'stock_dividend') AND ex_date IS NULL THEN 'no ex-date'
            WHEN action IN ('split', 'stock_dividend') AND ratio IS NULL THEN 'no ratio'
            WHEN action = 'cash_dividend' AND ex_date IS NULL THEN 'no ex-date'
            WHEN action = 'cash_dividend' AND coalesce(upper(currency), 'USD') <> 'USD' THEN 'paid in ' || currency
            WHEN action = 'cash_dividend' AND NOT (amount > 0) THEN 'no amount'
            WHEN action = 'cash_dividend' AND (close_before IS NULL
                 OR date_diff('day', close_day, ex_date) > {CLOSE_BEFORE_DAYS}) THEN 'no close before the ex-date'
            WHEN action = 'cash_dividend' AND NOT (amount < close_before) THEN 'the amount is not below the close'
            WHEN action IN {NOT_APPLIED} THEN action || ': not applied by the factors'
            END"""
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _act_final AS
            SELECT security_id, ex_date, action, action_type, ratio, amount, currency, special,
                   CASE WHEN action IN {APPLIED} OR action IN {NOT_APPLIED} THEN ({why}) IS NULL END AS applied,
                   {why} AS not_applied_why, symbol, new_symbol, record_date, payable_date, process_date,
                   source, action_id AS source_id, source_as_of, fetched_at, run_id,
                   close_before
            FROM _act_close""")
        n = _create(con, "corporate_action", "SELECT * FROM _act_final ORDER BY security_id, ex_date, action")
        for aid, action, sid, why_ in con.execute(
                "SELECT source_id, action, security_id, not_applied_why FROM corporate_action "
                "WHERE applied = false AND NOT starts_with(not_applied_why, 'not yet ex')").fetchall():
            kind = "dividend_not_applied" if action == "cash_dividend" else "action_not_applied"
            found.append((kind, aid, f"{sid}: {why_}"))
        return n

    # ── Shares ────────────────────────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _shares(con: duckdb.DuckDBPyConnection, found: list) -> int:
        # A cover class goes to the spine row of its trading symbol, else (no symbol, the filing's one class) to the
        # CIK's one Tier A row. Not to an ADR's: the cover counts the issuer's ordinary shares and the bar is the ADS
        # (one TSM ADS is five shares), so the count stays under ``class:`` and the ADR has no cover shares.
        # shortcut: an ADR gets no market cap from the store at all; once a source of its ADS ratio (or its ADS count)
        # is read, divide the ordinary count by the ratio and map it to the ADR's row.
        con.execute("""CREATE OR REPLACE TEMP TABLE _cover AS
            WITH one AS (SELECT cik, any_value(security_id) AS security_id, any_value(security_type) AS security_type
                         FROM stg_spine WHERE tier = 'A' GROUP BY cik HAVING count(*) = 1),
                 classes AS (SELECT accession, count(*) AS n FROM stg_sec_cover GROUP BY accession),
                 m AS (SELECT c.*, s.security_id AS by_symbol,
                              coalesce(s.security_id, CASE WHEN c.symbol IS NULL AND k.n = 1 THEN o.security_id END)
                                  AS spine_id,
                              CASE WHEN s.security_id IS NOT NULL THEN s.security_type
                                   WHEN c.symbol IS NULL AND k.n = 1 THEN o.security_type END AS spine_type
                       FROM stg_sec_cover c JOIN classes k USING (accession)
                       LEFT JOIN stg_spine s ON s.cik = c.cik AND s.ticker = replace(upper(c.symbol), '.', '-')
                       LEFT JOIN one o ON o.cik = c.cik)
            SELECT *, CASE WHEN spine_id IS NOT NULL AND spine_type IS DISTINCT FROM 'adr' THEN spine_id
                           ELSE 'class:' || cik || ':' || coalesce(nullif(class_member, ''), 'common')
                      END AS security_id
            FROM m""")
        n = _create(con, "shares_class", f"""
            SELECT c.security_id, 'outstanding' AS measure, c.as_of, c.shares AS value, c.symbol, c.cik,
                   nullif(c.class_member, '') AS class_member, '{COVER}' AS source, c.accession AS source_ref,
                   c.filed, c.source_as_of, c.fetched_at, c.run_id
            FROM _cover c
            UNION ALL
            SELECT coalesce(s.security_id, y.security_id), 'float', y.source_as_of, y.float_shares, y.ticker, y.cik,
                   NULL, '{YAHOO}', NULL, NULL, y.source_as_of, y.fetched_at, y.run_id
            FROM stg_yahoo_float y LEFT JOIN stg_spine s ON s.ticker = y.ticker AND s.cik IS NOT DISTINCT FROM y.cik
            WHERE y.float_shares IS NOT NULL
            UNION ALL
            SELECT coalesce(s.security_id, u.security_id), 'float', u.source_as_of, u.total_float, u.ticker, u.cik,
                   NULL, '{UW}', NULL, NULL, u.source_as_of, u.fetched_at, u.run_id
            FROM stg_uw_float u LEFT JOIN stg_spine s ON s.ticker = u.ticker AND s.cik IS NOT DISTINCT FROM u.cik
            WHERE u.total_float IS NOT NULL""")
        for cik, symbol in con.execute(
                "SELECT DISTINCT cik, symbol FROM _cover WHERE symbol IS NOT NULL AND by_symbol IS NULL").fetchall():
            found.append(("cover_class_unmapped", f"{cik}:{symbol}", "no spine ticker of the CIK is this symbol"))
        for sid, forms, shares in con.execute(
                "SELECT spine_id, string_agg(DISTINCT form, ', '), arg_max(shares, as_of) FROM _cover "
                "WHERE spine_type = 'adr' GROUP BY spine_id ORDER BY 1").fetchall():
            found.append(("cover_adr_ordinary", sid, f"{forms} cover counts {shares:,.0f} ordinary shares, not the "
                                                     "ADSs: no market cap from it"))
        return n

    # ── The WRDS difference report ────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _wrds_report(con: duckdb.DuckDBPyConnection) -> int:
        """``wrds_price_diff``: one row per ``wrds_history.COMPARISONS`` entry (zero sessions when a side has none)."""
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _hret AS
            WITH d AS (SELECT security_id, ex_date, sum(amount) AS amount FROM corporate_action
                       WHERE applied AND action = 'cash_dividend' GROUP BY ALL),
            x AS (SELECT h.source, h.security_id, h.session_date, h.own_return,
                         h.close * coalesce(a.split_factor, 1) AS sc,
                         coalesce(d.amount, 0) * coalesce(a.split_factor, 1) AS dv
                  FROM _hist_all h LEFT JOIN adj_factor a ON {period('a', 'h.security_id', 'h.session_date')}
                  LEFT JOIN d ON d.security_id = h.security_id AND d.ex_date = h.session_date WHERE h.close > 0),
            y AS (SELECT *, lag(sc) OVER w AS prev_sc, lag(session_date) OVER w AS prev_day FROM x
                  WINDOW w AS (PARTITION BY source, security_id ORDER BY session_date))
            -- The economic day return: the close plus the dividend over the previous close, in one share basis.
            SELECT source, security_id, session_date, own_return, (sc + dv) / prev_sc - 1 AS ours FROM y
            WHERE prev_sc > 0 AND own_return IS NOT NULL AND date_diff('day', prev_day, session_date) <= 7""")
        side = {"crsp": f"SELECT security_id, session_date, close FROM _hist_all WHERE source = '{wrds_history.CRSP}'",
                "compustat": ("SELECT security_id, session_date, close FROM _hist_all "
                              f"WHERE source = '{wrds_history.COMPUSTAT}'"),
                "alpaca": "SELECT security_id, session_date, close FROM _alp"}
        pair = ("SELECT '{name}' AS comparison, count(DISTINCT x.security_id) AS securities, count(*) AS sessions, "
                f"count(*) FILTER (WHERE abs(x.close - y.close) > {TOLERANCE} * y.close) AS close_differs, "
                "max(abs(x.close - y.close) / y.close) AS max_close_diff, CAST(NULL AS INTEGER) AS return_differs, "
                "CAST(NULL AS DOUBLE) AS max_return_diff, min(x.session_date) AS first_session, "
                "max(x.session_date) AS last_session "
                "FROM ({x}) x JOIN ({y}) y USING (security_id, session_date) WHERE y.close > 0")
        own = ("SELECT '{name}', count(DISTINCT security_id), count(*), CAST(NULL AS INTEGER), CAST(NULL AS DOUBLE), "
               f"count(*) FILTER (WHERE abs(ours - own_return) > {TOLERANCE}), max(abs(ours - own_return)), "
               "min(session_date), max(session_date) FROM _hret WHERE source = '{source}'")
        parts = []
        for name in wrds_history.COMPARISONS:
            if name.endswith("_returns"):
                source = wrds_history.CRSP if name.startswith("crsp") else wrds_history.COMPUSTAT
                parts.append(own.format(name=name, source=source))
            else:
                x, y = name.split("_vs_")
                parts.append(pair.format(name=name, x=side[x], y=side[y]))
        as_of = ("(SELECT max(source_as_of) FROM (SELECT source_as_of FROM stg_wrds_secd "
                 "UNION ALL SELECT source_as_of FROM stg_wrds_dsf))")
        return _create(con, "wrds_price_diff",
                       f"SELECT comparison, {as_of} AS as_of, securities, sessions, close_differs, max_close_diff, "
                       "return_differs, max_return_diff, first_session, last_session FROM ("
                       + " UNION ALL ".join(parts) + ")")

    # ── Views ─────────────────────────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _views(con: duckdb.DuckDBPyConnection) -> None:
        built = "(SELECT max(built_at) FROM serving_schema_version)"
        con.execute("CREATE VIEW security_symbol_now AS SELECT security_id, symbol FROM security_symbol "
                    "QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY valid_to IS NULL DESC, "
                    "valid_from DESC NULLS LAST) = 1")
        con.execute(f"""CREATE VIEW price_bars_daily AS
            SELECT n.symbol AS ticker, b.session_date, b.open * coalesce(a.split_factor, 1) AS open,
                   b.high * coalesce(a.split_factor, 1) AS high, b.low * coalesce(a.split_factor, 1) AS low,
                   b.close * coalesce(a.split_factor, 1) AS close, b.close * coalesce(a.tr_factor, 1) AS adj_close,
                   b.volume / coalesce(a.split_factor, 1) AS volume, CAST(NULL AS DOUBLE) AS split_ratio,
                   'prices/adj_factor' AS adj_source, b.source, {built} AS fetched_at, b.run_id, {built} AS run_at
            FROM price_bar_daily b JOIN security_symbol_now n USING (security_id)
            LEFT JOIN adj_factor a ON {period('a', 'b.security_id', 'b.session_date')}
            WHERE b.security_id NOT LIKE 'index:%'""")
        con.execute(f"""CREATE VIEW price_actions AS
            SELECT n.symbol AS ticker, c.ex_date, CASE WHEN c.action = 'cash_dividend' THEN 'dividend'
                   ELSE 'split' END AS action,
                   CASE WHEN c.action = 'cash_dividend' THEN c.amount * coalesce(a.split_factor, 1)
                        ELSE c.ratio END AS value,
                   c.ex_date AS session_date, c.source, {built} AS fetched_at, c.run_id, {built} AS run_at
            FROM corporate_action c JOIN security_symbol_now n USING (security_id)
            LEFT JOIN adj_factor a ON {period('a', 'c.security_id', 'c.ex_date')}
            WHERE c.applied""")
        con.execute(f"""CREATE VIEW index_levels_daily AS
            SELECT '^GSPC' AS symbol, session_date, open, high, low, close, volume, source, {built} AS fetched_at,
                   run_id, {built} AS run_at
            FROM price_bar_daily WHERE security_id = 'index:SPX'""")

    # ── Issues ────────────────────────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _issues(con: duckdb.DuckDBPyConnection, have: dict[str, int]) -> list[tuple[str, str, str]]:
        found: list[tuple[str, str, str]] = []
        newest = con.execute(f"SELECT max(session_date) FROM price_bar_daily WHERE source = '{ALPACA_BARS}'"
                             ).fetchone()[0]
        if newest is not None:
            for sid, ticker in con.execute(
                    "SELECT s.security_id, s.ticker FROM stg_spine s WHERE s.tier = 'A' AND NOT EXISTS (SELECT 1 "
                    "FROM price_bar_daily b WHERE b.security_id = s.security_id AND b.session_date = ?) "
                    "ORDER BY s.security_id", [newest]).fetchall():
                found.append(("tier_a_no_bar", sid, f"{ticker}: no bar on {newest}"))
            for sid, ticker in con.execute(
                    "SELECT s.security_id, s.ticker FROM stg_spine s WHERE s.tier = 'A' AND NOT EXISTS (SELECT 1 "
                    "FROM market_cap_daily m WHERE m.security_id = s.security_id AND m.session_date = ?) "
                    "ORDER BY s.security_id", [newest]).fetchall():
                found.append(("tier_a_no_shares", sid, f"{ticker}: no shares for {newest}"))
        con.execute(f"""CREATE OR REPLACE TEMP TABLE _ret AS
            SELECT b.security_id, b.session_date, b.close * coalesce(a.split_factor, 1) AS split_close,
                   b.close * coalesce(a.tr_factor, 1) AS tr_close, b.source,
                   lag(b.session_date) OVER w AS prev_day,
                   lag(b.close * coalesce(a.split_factor, 1)) OVER w AS prev_split,
                   lag(b.close * coalesce(a.tr_factor, 1)) OVER w AS prev_tr
            FROM price_bar_daily b LEFT JOIN adj_factor a ON {period('a', 'b.security_id', 'b.session_date')}
            WHERE b.security_id NOT LIKE 'index:%' AND b.close > 0
            WINDOW w AS (PARTITION BY b.security_id ORDER BY b.session_date)""")
        for sid, day, ratio in con.execute(f"""
                SELECT r.security_id, r.session_date, r.split_close / r.prev_split FROM _ret r
                WHERE r.prev_split > 0 AND date_diff('day', r.prev_day, r.session_date) <= 7
                  AND abs(ln(r.split_close / r.prev_split)) >= ln({GAP_FACTOR})
                  AND NOT EXISTS (SELECT 1 FROM corporate_action c WHERE c.security_id = r.security_id
                                  AND c.action IN ('split', 'stock_dividend') AND c.ex_date > r.prev_day
                                  AND c.ex_date <= r.session_date)
                ORDER BY 1, 2""").fetchall():
            found.append(("price_gap_no_action", f"{sid}:{day}", f"split-adjusted close x{ratio:.3f}, no split"))
        # Alpaca's previous adjusted close is close_all / (1 + all_return). The allowance is its exact rounding
        # bound, a hair wider (1e-6 of it) for the float error of a row that sits on it; none at a previous close of
        # half a cent or less, where the bound has no meaning and the flat 1 bp stays.
        prev = "a.close_all / nullif(1 + a.all_return, 0)"
        allowance = (f"CASE WHEN {prev} > {HALF_CENT} THEN {HALF_CENT} * (a.close_all + {prev}) "
                     f"/ ({prev} * ({prev} - {HALF_CENT})) * (1 + 1e-6) ELSE 0 END")
        for sid, day, ours, theirs in con.execute(f"""
                SELECT r.security_id, r.session_date, r.tr_close / r.prev_tr - 1, a.all_return
                FROM _ret r JOIN _alp a ON a.security_id = r.security_id AND a.session_date = r.session_date
                WHERE r.source = '{ALPACA_BARS}' AND r.prev_tr > 0 AND a.all_return IS NOT NULL
                  AND date_diff('day', r.prev_day, r.session_date) <= 5
                  AND abs((r.tr_close / r.prev_tr - 1) - a.all_return) > greatest({TOLERANCE}, {allowance})
                ORDER BY 1, 2""").fetchall():
            found.append(("adjustment_differs", f"{sid}:{day}", f"ours {ours:.6f}, alpaca {theirs:.6f}"))
        newest_of = ("SELECT security_id, value FROM shares_class WHERE measure = '{m}' AND source = '{s}' "
                     "QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY as_of DESC, fetched_at DESC) = 1")
        for sid, fl, out in con.execute(f"""
                WITH f AS ({newest_of.format(m='float', s=YAHOO)} UNION ALL {newest_of.format(m='float', s=UW)}),
                     o AS (SELECT o.security_id, o.value * coalesce(a2.split_factor, 1)
                                  / coalesce(a1.split_factor, 1) AS value
                           FROM (SELECT * FROM shares_class WHERE measure = 'outstanding'
                                 QUALIFY row_number() OVER (PARTITION BY security_id ORDER BY as_of DESC) = 1) o
                           LEFT JOIN adj_factor a1 ON {period('a1', 'o.security_id', 'o.as_of')}
                           LEFT JOIN adj_factor a2 ON {period('a2', 'o.security_id', "DATE '9999-12-31'")})
                SELECT f.security_id, max(f.value), any_value(o.value) FROM f JOIN o USING (security_id)
                GROUP BY f.security_id HAVING max(f.value) > {FLOAT_OVER} * any_value(o.value) ORDER BY 1""").fetchall():
            found.append(("float_above_outstanding", sid, f"float {fl:,.0f} over outstanding {out:,.0f}"))
        for sid, y, u in con.execute(f"""
                SELECT y.security_id, y.value, u.value FROM ({newest_of.format(m='float', s=YAHOO)}) y
                JOIN ({newest_of.format(m='float', s=UW)}) u USING (security_id)
                WHERE abs(y.value - u.value) > {FLOAT_DISAGREE} * greatest(y.value, u.value) ORDER BY 1""").fetchall():
            found.append(("float_sources_disagree", sid, f"yahoo {y:,.0f}, uw {u:,.0f}"))
        # shortcut: flagged only, the cap stays; if avo wants it, withhold a cover cap beyond a factor of two from
        # Yahoo (the universe then falls back as for a name with no shares).
        for sid, cover, as_of, yahoo, day in con.execute(f"""
                WITH y AS (SELECT coalesce(s.security_id, y.security_id) AS security_id,
                                  y.shares_outstanding AS yahoo, y.source_as_of
                           FROM stg_yahoo_float y
                           LEFT JOIN stg_spine s ON s.ticker = y.ticker AND s.cik IS NOT DISTINCT FROM y.cik
                           WHERE y.shares_outstanding > 0
                           QUALIFY row_number() OVER (PARTITION BY coalesce(s.security_id, y.security_id)
                                                      ORDER BY y.source_as_of DESC, y.fetched_at DESC) = 1)
                SELECT y.security_id, m.shares, m.shares_as_of, y.yahoo, m.session_date
                FROM y ASOF JOIN market_cap_daily m
                    ON m.security_id = y.security_id AND y.source_as_of >= m.session_date
                WHERE m.shares_source = '{COVER}'
                  AND abs(m.shares - y.yahoo) > {SHARES_DISAGREE} * greatest(m.shares, y.yahoo)
                ORDER BY 1""").fetchall():
            found.append(("shares_disagree_yahoo", sid, f"cover {cover:,.0f} (as of {as_of}, moved over the splits "
                                                        f"since), yahoo {yahoo:,.0f} on {day}"))
        return found

    def check(self, ctx: base.RunContext, con: duckdb.DuckDBPyConnection) -> dict:
        cols = ["security_id", "session_date", "open", "high", "low", "close", "volume", "source"]
        added, removed = stage.compare_live(con, "price_bar_daily", cols)
        newest = con.execute("SELECT max(session_date) FROM price_bar_daily").fetchone()[0]
        n_issues = len(ctx.pending.get(NAME, []))
        ctx.notes.append(f"{n_issues} build issues found")
        return {"dataset": NAME, "result": "changed" if added or removed else "unchanged",
                "source_as_of": (newest or ctx.as_of or self.default_as_of()).isoformat(),
                "rows": stage.count(con, "price_bar_daily"), "added": added, "removed": removed, "coverage": None}

    def after_swap(self, ctx: base.RunContext) -> None:
        found = ctx.pending.get(NAME)
        if found is None:
            return
        ops.sync_issues(NAME, ISSUE_KINDS, found, run_id=ctx.run_id)


ADJ_SQL = f"""
    WITH ev AS (
        SELECT security_id, ex_date,
               product(CASE WHEN action = 'cash_dividend' THEN 1.0 ELSE 1.0 / ratio END) AS split_mult,
               product(CASE WHEN action = 'cash_dividend' THEN 1.0 - amount / close_before ELSE 1.0 / ratio END)
                   AS tr_mult,
               count(*) AS n
        FROM _act_final WHERE applied AND action IN {APPLIED} GROUP BY ALL)
    SELECT security_id, lag(ex_date) OVER (PARTITION BY security_id ORDER BY ex_date) AS valid_from,
           ex_date AS valid_to,
           product(split_mult) OVER (PARTITION BY security_id ORDER BY ex_date DESC
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS split_factor,
           product(tr_mult) OVER (PARTITION BY security_id ORDER BY ex_date DESC
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS tr_factor,
           CAST(n AS INTEGER) AS events
    FROM ev ORDER BY security_id, ex_date"""

SYMBOL_SQL = """
    WITH p AS (SELECT security_id, symbol, any_value(cik) AS cik, min(session_date) AS lo,
                      arg_max(source, session_date) AS source
               FROM price_bar_daily WHERE symbol IS NOT NULL AND security_id NOT LIKE 'index:%' GROUP BY ALL)
    SELECT security_id, symbol, cik, lo AS valid_from,
           lead(lo) OVER (PARTITION BY security_id ORDER BY lo) AS valid_to, source FROM p
    UNION ALL
    SELECT security_id, ticker, cik, NULL, NULL, 'spine/security' FROM stg_spine
    WHERE tier = 'A' AND security_id NOT IN (SELECT security_id FROM p)"""

MARKET_CAP_SQL = f"""
    WITH o AS (SELECT security_id, as_of, value, source FROM shares_class WHERE measure = 'outstanding'
               QUALIFY row_number() OVER (PARTITION BY security_id, as_of ORDER BY fetched_at DESC) = 1),
         h AS (SELECT security_id, session_date, shares_outstanding, source FROM _hist_all
               WHERE shares_outstanding > 0
               QUALIFY row_number() OVER (PARTITION BY security_id, session_date ORDER BY priority) = 1),
         b AS (SELECT b.*, coalesce(s.cik, b.cik) AS issuer, h.shares_outstanding AS hist_shares,
                      h.source AS hist_source FROM price_bar_daily b
               LEFT JOIN (SELECT DISTINCT security_id, cik FROM stg_spine) s USING (security_id)
               LEFT JOIN h ON h.security_id = b.security_id AND h.session_date = b.session_date
               WHERE b.security_id NOT LIKE 'index:%' AND b.close IS NOT NULL),
         -- The bar's own shares, else a WRDS source's of that session (an Alpaca bar Compustat also has), else the
         -- newest cover on or before the session.
         j AS (SELECT b.security_id, b.session_date, b.issuer, b.close, coalesce(b.shares_outstanding, b.hist_shares) AS own,
                      CASE WHEN b.shares_outstanding IS NOT NULL THEN b.source ELSE b.hist_source END AS bar_source,
                      o.value AS cover, o.as_of AS cover_as_of, o.source AS cover_source
               FROM b ASOF LEFT JOIN o ON o.security_id = b.security_id AND b.session_date >= o.as_of),
         s AS (SELECT j.*, CASE WHEN own IS NOT NULL THEN own
                                ELSE cover * coalesce(a1.split_factor, 1) / coalesce(a2.split_factor, 1) END AS shares
               FROM j LEFT JOIN adj_factor a1 ON {period('a1', 'j.security_id', 'j.session_date')}
               LEFT JOIN adj_factor a2 ON {period('a2', 'j.security_id', 'j.cover_as_of')}),
         c AS (SELECT security_id, session_date, issuer AS cik, close, shares,
                      CASE WHEN own IS NOT NULL THEN bar_source ELSE cover_source END AS shares_source,
                      CASE WHEN own IS NOT NULL THEN session_date ELSE cover_as_of END AS shares_as_of,
                      close * shares AS market_cap, coalesce(issuer, security_id) AS iss
               FROM s WHERE shares IS NOT NULL),
         -- Each class of the issuer at its newest cap on or before the session, if at most ISSUER_WITHIN_DAYS old:
         -- a class with no bar that day (GOOG on a day GOOGL traded) still counts.
         k AS (SELECT DISTINCT iss, security_id AS cls FROM c),
         g AS (SELECT c.security_id, c.session_date, k.cls FROM c JOIN k USING (iss)),
         p AS (SELECT g.security_id, g.session_date, x.market_cap, x.session_date AS cap_day
               FROM g ASOF LEFT JOIN c x ON x.security_id = g.cls AND g.session_date >= x.session_date),
         i AS (SELECT security_id, session_date,
                      sum(market_cap) FILTER (WHERE date_diff('day', cap_day, session_date) <= {{within}})
                          AS issuer_market_cap
               FROM p GROUP BY ALL)
    SELECT c.*, i.issuer_market_cap FROM c JOIN i USING (security_id, session_date)"""

REVISION_SQL = f"""
    SELECT coalesce(s.security_id, r.security_id) AS security_id, r.session_date, r.ticker AS symbol, r.open, r.high,
           r.low, r.close, r.volume, '{ALPACA_BARS}' AS source, r.fetched_at, r.run_id, r.revised_at, r.revised_by
    FROM stg_alpaca_bar_revisions r LEFT JOIN stg_spine s ON s.ticker = r.ticker AND s.cik IS NOT DISTINCT FROM r.cik"""


DATASET = PricesServing()
